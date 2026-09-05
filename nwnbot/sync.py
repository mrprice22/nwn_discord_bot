"""Pure planners: (roadmap snapshot, forum snapshot, store view) -> action list.

Owned by ``[b6-sync]``, extended by ``[b9-dupes]``. It holds
:func:`plan_discord_to_roadmap` and :func:`plan_roadmap_to_discord`.

**These functions stay pure.** They never call Discord or the roadmap API, read
a database, look at the clock or roll a die; they return a :class:`Plan` of
actions that an executor applies. That is what makes dry-run honest and the
tests cheap, and every new behaviour is a new action type plus a table-driven
test rather than an inline side effect. :func:`simulate` applies a plan to the
same immutable inputs, which is how "feeding a planner's own output back in is
a no-op" is a *test* rather than a claim.

Notably, the Discord -> roadmap direction appends to the internal ``comments``
list and never writes ``notes``: ``notes`` is the admin's player-facing release
note and is only ever written by a human. :class:`UpdateIdeaField` refuses
``notes``/``impl_notes``/``merit_awarded`` and the three admin-only statuses by
raising :class:`~nwnbot.roadmap.ForbiddenWrite` at construction, so a forbidden
write cannot even be *planned*, let alone executed.

Loop prevention has three layers:

1. every message authored by the bot's own Discord user id is skipped;
2. a content hash per ``(idea_id, field)`` in :class:`~nwnbot.store.StoreView`
   skips a value that has already been synced;
3. a per-run action cap (:data:`DEFAULT_ACTION_CAP`, 25) marks the plan
   ``aborted`` and reports, rather than handing a runaway batch to an executor.

Configuration is an **input**, not an import. The forum-tag -> group mapping,
the channel -> type mapping and the player identity map all arrive in
:class:`PlanContext`; ``[b5-config]`` supplies them and ``nwnbot.cli`` builds
the context. Nothing here invents a tag name or hard-codes an id, and an empty
mapping produces a review item rather than a guess.

``type`` is written **once**, when an idea is created: there is no ``Exploit``
forum tag (review item ``[r3]``), so an exploit arrives as a ``Defect`` and the
admin promotes it in the editor. :data:`CREATION_ONLY_FIELDS` makes an update
to ``type`` unconstructible, so a later sync cannot demote a promoted exploit
from 3 merit back to 1.
"""

from __future__ import annotations

import copy
import dataclasses
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator, Mapping, Sequence

from nwnbot import config as cfg
from nwnbot.config import MERIT_BY_TYPE
from nwnbot.forum import ForumMessage, ForumSnapshot, ForumThread
from nwnbot.roadmap import COMMENT_MAX_LEN, ForbiddenWrite, Snapshot
from nwnbot.store import StoreView, content_hash

# Per-run cap on planned actions; exceeding it aborts the run and reports.
DEFAULT_ACTION_CAP = 25

#: The ten statuses, `bin/gen-roadmap.py:73` in nwn_homers_lotr.
STATUSES: tuple[str, ...] = (
    "awarded", "implemented", "confirmed", "manual", "design",
    "wip", "soon", "later", "planned", "unlikely",
)

#: Statuses the bot must never *write* (`roadmap.FORBIDDEN_STATUSES`).
ADMIN_ONLY_STATUSES = frozenset({"awarded", "implemented", "manual"})

#: Fields the bot must never write. ``notes`` is the admin's player-facing
#: release note; ``merit_awarded`` is the merit DB's own receipt.
ADMIN_ONLY_FIELDS = frozenset({"notes", "notes_h", "impl_notes", "impl_notes_h",
                               "merit_awarded"})

#: Fields the bot may set when it *creates* an idea and may never change
#: afterwards. ``type`` is the whole list, and the reason is review item
#: ``[r3]``: there is no ``Exploit`` forum tag, so an exploit arrives as an
#: ordinary bug (``Defect``, 1 merit) and the admin promotes it to ``Exploit``
#: (3 merit) in the editor. An update to ``type`` would silently undo that
#: promotion on the next sync. Enforced here, at construction, so it cannot be
#: planned — and again in ``nwnbot.roadmap.assert_ideas_writable`` on the wire.
CREATION_ONLY_FIELDS = frozenset(cfg.CREATION_ONLY_FIELDS)

#: Terminal states: there is no ``closed``. A thread is closed on
#: ``merit_awarded`` (the boolean, not the status) or on ``unlikely``.
TERMINAL_STATUSES = frozenset({"awarded", "unlikely"})

#: Where a brand-new idea minted from a forum thread starts. "idea captured,
#: under consideration (not committed to)" — the only status that describes a
#: report nobody has triaged yet, and not one of the admin-only three.
#: PROVISIONAL — see plan.md review item [r11]; the admin owns the triage status.
NEW_IDEA_STATUS = "planned"

#: Ids show up in URLs, `dupe_of` pickers and conflict messages; the editor
#: trims to 60 (`roadmap-editor.py:4584`, `shortenId(slugifyId(title), 60)`).
ID_MAX_LEN = 60

# --------------------------------------------------------------------------
# Player-visible strings
#
# PROVISIONAL WORDING — the admin owns every string a player can read. These
# are placeholders that invent as little as possible, marked the same way as
# `nwnbot/render.py`'s. Nothing reaches a player until `apply --yes` runs.
# --------------------------------------------------------------------------

#: Posted in a thread when the linked item's status changes.
STATUS_MESSAGE = "Roadmap status for this report is now: {status} — {label}.{link}"

#: Posted when the governing item's merit has really been paid. The thread is
#: archived *and locked* straight after.
MERIT_MESSAGE = (
    "This has shipped and {merit} merit {points} been awarded for it "
    "({type}). Thanks for the report — closing this thread.{link}"
)

#: Posted when an item is marked `unlikely`. Archived, deliberately NOT locked.
UNLIKELY_MESSAGE = (
    "This one is logged but not likely to be implemented. Archiving the thread; "
    "it stays readable and unlocked.{link}"
)

#: Opening post of a thread the bot creates from an existing roadmap item.
THREAD_BODY = "{body}{link}"

#: The internal, never-rendered `comments` entry a Discord message becomes.
#: PROVISIONAL WORDING, though the blast radius is small: only the admin ever
#: sees the `comments` list.
COMMENT_TEMPLATE = "Discord — {author}{where}:\n\n{body}"

#: Appended to a Discord-bound message as a link back to the item.
LINK_SUFFIX = "\n\n{url}"


# --------------------------------------------------------------------------
# Review kinds — every judgement call the bot refuses to make on its own
# --------------------------------------------------------------------------
REVIEW_UNKNOWN_AUTHOR = "unknown_author"
REVIEW_PLAYER_NOT_ON_ROSTER = "player_not_on_roster"
REVIEW_THREAD_RENAMED = "thread_renamed"
REVIEW_UNMAPPED_TAG = "unmapped_tag"
REVIEW_TAG_MAPPING_MISSING = "tag_mapping_missing"
REVIEW_UNKNOWN_CHANNEL = "unknown_channel"
REVIEW_NO_CHANNEL_FOR_TYPE = "no_channel_for_type"
REVIEW_UNSLUGGABLE_TITLE = "unsluggable_title"
REVIEW_BROKEN_LINK = "broken_link"
REVIEW_DUPE_CYCLE = "dupe_cycle"
REVIEW_UNKNOWN_STATUS = "unknown_status"
REVIEW_ACTION_CAP = "action_cap"


# --------------------------------------------------------------------------
# Id minting — mirrors the editor's own rules, character for character
#
# `slugifyId` / `shortenId` / `uniqueIdeaId` are JS in
# nwn_homers_lotr/bin/roadmap-editor.py:4537, 4552 and 4562, called together at
# :4584 as `shortenId(slugifyId(title), 60)` then `uniqueIdeaId(...)`. They are
# transliterated here rather than reinvented, because an id is a stable key:
# `dupe_of` points at it and `#idea-<id>` anchors are linked from other notes.
# --------------------------------------------------------------------------
_COMBINING = re.compile("[\\u0300-\\u036f]")
_APOSTROPHES = re.compile("['\\u2019]")
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_TRIM_DASHES = re.compile(r"^-+|-+$")


def slugify_id(title: str | None) -> str:
    """`slugifyId` (roadmap-editor.py:4537), transliterated from JS.

    NFD-decompose and drop combining marks ("Theoden" out of "Theoden"),
    lowercase, delete apostrophes so "boss's" is not "boss-s", turn every other
    run into a single "-", and trim leading/trailing separators.
    """
    text = unicodedata.normalize("NFD", title or "")
    text = _COMBINING.sub("", text)
    text = text.lower()
    text = _APOSTROPHES.sub("", text)
    text = _NON_SLUG.sub("-", text)
    return _TRIM_DASHES.sub("", text)


def shorten_id(slug: str, max_len: int = ID_MAX_LEN) -> str:
    """`shortenId` (roadmap-editor.py:4552): cut on a word boundary.

    Keeps the whole slug when it fits; otherwise cuts at ``max_len`` and backs
    up to the last "-" if that boundary is past the halfway mark, then strips
    trailing separators. The ``> max/2`` comparison is the editor's, kept as
    float division so the boundary case matches.
    """
    if len(slug) <= max_len:
        return slug
    cut = slug[:max_len]
    at = cut.rfind("-")
    out = cut[:at] if at > max_len / 2 else cut
    return out.rstrip("-")


def unique_idea_id(base: str, taken: Iterable[str]) -> str:
    """`uniqueIdeaId` (roadmap-editor.py:4562): append -2, -3, … until free.

    Case-insensitive, exactly as the editor is: roadmap.yaml still carries a few
    mixed-case ids (e.g. ``Commoner-troll-faction``) and a lowercase twin of one
    would be ambiguous both in ``dupe_of`` and as an anchor.
    """
    used = {str(t).lower() for t in taken if t}
    if base.lower() not in used:
        return base
    n = 2
    while f"{base}-{n}".lower() in used:
        n += 1
    return f"{base}-{n}"


def mint_idea_id(title: str, taken: Iterable[str], max_len: int = ID_MAX_LEN) -> str:
    """The editor's full autofill chain: slugify, shorten to 60, de-duplicate."""
    return unique_idea_id(shorten_id(slugify_id(title), max_len), taken)


# --------------------------------------------------------------------------
# Effects — what applying an action leaves behind in the store
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Effects:
    """State an *applied* action writes back. Never written by a planner.

    ``nwnbot.store.Store.record`` reads this duck-typed, so a new action type
    needs no change there; :func:`simulate` reads the same thing, which is why
    the dry-run and the real run can never drift.
    """

    links: Mapping[str, tuple[str, str]] = field(default_factory=dict)  # thread -> (idea, channel)
    hashes: Mapping[tuple[str, str], Any] = field(default_factory=dict)
    reviews: tuple["ReviewItem", ...] = ()


# --------------------------------------------------------------------------
# The action types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    """Base class. An action is inert data describing one write, nothing more."""

    @property
    def key(self) -> str:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def effects(self) -> Effects:
        return Effects()

    def describe(self) -> str:
        return self.key


@dataclass(frozen=True)
class CreateIdea(Action):
    """Mint a new roadmap idea from a forum thread. Always ``hidden: true``."""

    idea: Mapping[str, Any]
    thread_id: str = ""
    channel_id: str = ""
    reason: str = "new forum thread"

    def __post_init__(self) -> None:
        idea = dict(self.idea)
        for name in ADMIN_ONLY_FIELDS:
            if name in idea:
                raise ForbiddenWrite(
                    f"{idea.get('id')!r}: a planned new idea may not carry {name!r}")
        if idea.get("status") in ADMIN_ONLY_STATUSES:
            raise ForbiddenWrite(
                f"{idea.get('id')!r}: refusing to plan status {idea['status']!r}")
        if idea.get("hidden") is not True:
            raise ForbiddenWrite(
                f"{idea.get('id')!r}: a bot-created idea must be hidden: true")
        object.__setattr__(self, "idea", idea)

    @property
    def idea_id(self) -> str:
        return str(self.idea.get("id") or "")

    @property
    def key(self) -> str:
        return f"create_idea:{self.idea_id}"

    def effects(self) -> Effects:
        hashes = {(self.idea_id, f): self.idea.get(f)
                  for f in ("title", "group", "status", "type")}
        links = ({self.thread_id: (self.idea_id, self.channel_id)}
                 if self.thread_id else {})
        return Effects(links=links, hashes=hashes)

    def describe(self) -> str:
        return (f"create idea {self.idea_id!r} ({self.idea.get('group')}/"
                f"{self.idea.get('type')}) from thread {self.thread_id}")


@dataclass(frozen=True)
class AppendComment(Action):
    """Append to the idea's internal, append-only ``comments`` list.

    This is the *only* way Discord text reaches an idea. ``notes`` is never
    touched: it is the admin's player-facing release note.
    """

    idea_id: str
    text: str
    thread_id: str = ""
    message_id: str = ""
    field_name: str = ""

    def __post_init__(self) -> None:
        if not self.idea_id:
            raise ForbiddenWrite("a comment needs an idea id")
        if not (self.text or "").strip():
            raise ForbiddenWrite("refusing to plan an empty comment")
        if not self.field_name:
            object.__setattr__(self, "field_name",
                               f"comment:{self.message_id}" if self.message_id
                               else f"comment:{content_hash(self.text)[:12]}")
        if len(self.text) > COMMENT_MAX_LEN:
            object.__setattr__(self, "text", self.text[:COMMENT_MAX_LEN])

    @property
    def key(self) -> str:
        return f"comment:{self.idea_id}:{self.field_name}"

    def effects(self) -> Effects:
        return Effects(hashes={(self.idea_id, self.field_name): self.text})

    def describe(self) -> str:
        return f"comment on {self.idea_id!r} from message {self.message_id or '-'}"


@dataclass(frozen=True)
class UpdateIdeaField(Action):
    """Change one field on an existing idea. Refuses the admin's fields."""

    idea_id: str
    field_name: str
    value: Any
    previous: Any = None
    thread_id: str = ""

    def __post_init__(self) -> None:
        if not self.idea_id:
            raise ForbiddenWrite("an update needs an idea id")
        if self.field_name in ADMIN_ONLY_FIELDS:
            raise ForbiddenWrite(
                f"{self.idea_id!r}: refusing to plan a write to {self.field_name!r} — "
                f"that field is the admin's")
        if self.field_name in CREATION_ONLY_FIELDS:
            raise ForbiddenWrite(
                f"{self.idea_id!r}: refusing to plan an update to "
                f"{self.field_name!r} — it is written once, when the idea is "
                f"created ([r3]). Updating it would demote an admin-promoted "
                f"Exploit (3 merit) back to a Defect (1 merit).")
        if self.field_name == "status" and self.value in ADMIN_ONLY_STATUSES:
            raise ForbiddenWrite(
                f"{self.idea_id!r}: refusing to plan status {self.value!r} — "
                f"shipping and merit are the admin's call")

    @property
    def key(self) -> str:
        return f"update:{self.idea_id}:{self.field_name}"

    def effects(self) -> Effects:
        return Effects(hashes={(self.idea_id, self.field_name): self.value})

    def describe(self) -> str:
        return (f"set {self.field_name}={self.value!r} on {self.idea_id!r} "
                f"(was {self.previous!r})")


@dataclass(frozen=True)
class CreateThread(Action):
    """Open a forum thread for a roadmap item that has none."""

    idea_id: str
    channel_id: str
    title: str
    body: str = ""
    tag_names: tuple[str, ...] = ()
    status: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_names", tuple(self.tag_names))

    @property
    def key(self) -> str:
        return f"create_thread:{self.idea_id}"

    def effects(self) -> Effects:
        # No link yet: the thread id only exists once Discord has answered, so
        # the executor links it (and checkpoints it into the idea's `discord`
        # field) at that point. Recording the status baseline here stops the
        # very next run announcing a status nobody changed.
        return Effects(hashes={(self.idea_id, "status"): self.status})

    def describe(self) -> str:
        return (f"create thread for {self.idea_id!r} in {self.channel_id} "
                f"tagged {', '.join(self.tag_names) or '-'}")


@dataclass(frozen=True)
class PostMessage(Action):
    """Post one message into an existing thread."""

    thread_id: str
    idea_id: str
    text: str
    kind: str = "status"
    field_name: str = "status"
    value: Any = None

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ForbiddenWrite("a post needs a thread id")
        if not (self.text or "").strip():
            raise ForbiddenWrite("refusing to plan an empty post")

    @property
    def key(self) -> str:
        return f"post:{self.thread_id}:{self.kind}"

    def effects(self) -> Effects:
        if not self.idea_id:
            return Effects()
        return Effects(hashes={(self.idea_id, self.field_name): self.value})

    def describe(self) -> str:
        return f"post {self.kind} in thread {self.thread_id} for {self.idea_id!r}"


@dataclass(frozen=True)
class ArchiveThread(Action):
    """Archive a thread, and lock it only when merit has really been paid."""

    thread_id: str
    idea_id: str = ""
    locked: bool = False
    reason: str = ""

    @property
    def key(self) -> str:
        return f"archive:{self.thread_id}"

    def effects(self) -> Effects:
        if not self.idea_id:
            return Effects()
        return Effects(hashes={(self.idea_id, "archived"):
                               "locked" if self.locked else "archived"})

    def describe(self) -> str:
        return (f"archive thread {self.thread_id}"
                f"{' and lock' if self.locked else ' (unlocked)'} — {self.reason}")


@dataclass(frozen=True)
class RecordBaseline(Action):
    """Record a field's current value in the store without telling anyone.

    The first time the bot sees a thread it did not open — one linked by hand,
    or adopted from before it ran — there is no hash for ``status``, so every
    field looks "changed". Announcing a status nobody changed is exactly the
    noise the loop-prevention layers exist to avoid, so the first sighting
    plans a baseline instead of a post. It writes nothing to Discord or the
    roadmap and does not count against the action cap.
    """

    idea_id: str
    field_name: str
    value: Any
    reason: str = "first sighting"

    @property
    def key(self) -> str:
        return f"baseline:{self.idea_id}:{self.field_name}"

    def effects(self) -> Effects:
        return Effects(hashes={(self.idea_id, self.field_name): self.value})

    def describe(self) -> str:
        return (f"record baseline {self.field_name}={self.value!r} on "
                f"{self.idea_id!r} ({self.reason}) — nothing is posted")


@dataclass(frozen=True)
class ReviewItem(Action):
    """A question for the human. The bot plans one instead of guessing."""

    kind: str
    subject: str = ""
    detail: str = ""
    thread_id: str = ""
    idea_id: str = ""
    review_key: str = ""

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("a review item needs a kind")
        if not self.review_key:
            object.__setattr__(self, "review_key", f"{self.kind}:{self.subject}")

    @property
    def key(self) -> str:
        return self.review_key

    def effects(self) -> Effects:
        return Effects(reviews=(self,))

    def describe(self) -> str:
        return f"review [{self.kind}] {self.subject}: {self.detail}"


#: Actions that only touch local state: neither Discord nor the roadmap sees
#: them, so they are not "writes" and never count against the action cap.
STATE_ONLY = (ReviewItem, RecordBaseline)


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Plan(Sequence):
    """An ordered list of actions, plus whether it may be executed at all.

    Compares equal to a plain list of the same actions, so a test can assert
    ``plan == []``. When ``aborted`` is true the cap was exceeded: the actions
    are still here to be *read*, and no executor may run them.
    """

    actions: tuple[Action, ...] = ()
    cap: int = DEFAULT_ACTION_CAP
    aborted: bool = False
    direction: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))

    def __iter__(self) -> Iterator[Action]:
        return iter(self.actions)

    def __len__(self) -> int:
        return len(self.actions)

    def __getitem__(self, index):  # type: ignore[override]
        return self.actions[index]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Plan):
            return (self.actions == other.actions and self.aborted == other.aborted
                    and self.direction == other.direction)
        if isinstance(other, (list, tuple)):
            return list(self.actions) == list(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.actions, self.aborted, self.direction))

    @property
    def executable(self) -> bool:
        """False when the cap tripped: report the plan, do not run it."""
        return not self.aborted

    @property
    def reviews(self) -> tuple[ReviewItem, ...]:
        return tuple(a for a in self.actions if isinstance(a, ReviewItem))

    @property
    def baselines(self) -> tuple["RecordBaseline", ...]:
        return tuple(a for a in self.actions if isinstance(a, RecordBaseline))

    @property
    def writes(self) -> tuple[Action, ...]:
        """Actions that actually touch Discord or the roadmap."""
        return tuple(a for a in self.actions if not isinstance(a, STATE_ONLY))

    def describe(self) -> list[str]:
        return [a.describe() for a in self.actions]


# --------------------------------------------------------------------------
# The configuration the planners are handed
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanContext:
    """Everything the planners need to know that is not in a snapshot.

    All of it is an **input**: ``[b5-config]`` owns the values and
    ``nwnbot.cli`` assembles them. Nothing here has a default that invents one —
    an empty mapping produces a review item, never a guess, and ``players`` is
    an id -> name map so an unrecognised author is queued rather than matched.
    """

    tag_groups: Mapping[str, str] = field(default_factory=dict)      # tag name -> group id
    channel_types: Mapping[str, str] = field(default_factory=dict)   # channel id -> item type
    players: Mapping[str, str] = field(default_factory=dict)         # discord id -> player
    bot_user_id: str = ""
    action_cap: int = DEFAULT_ACTION_CAP
    editor_url: str = ""
    thread_url_template: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_groups", dict(self.tag_groups))
        object.__setattr__(self, "channel_types", dict(self.channel_types))
        object.__setattr__(self, "players", dict(self.players))

    def group_for_tags(self, tag_names: Iterable[str]) -> str | None:
        for name in tag_names:
            group = self.tag_groups.get(name)
            if group:
                return group
        return None

    def tags_for_group(self, group: str) -> tuple[str, ...]:
        return tuple(name for name, gid in self.tag_groups.items() if gid == group)

    def channel_for_type(self, item_type: str) -> str | None:
        for channel_id, kind in self.channel_types.items():
            if kind == item_type:
                return channel_id
        return None

    def idea_url(self, idea_id: str) -> str:
        return f"{self.editor_url.rstrip('/')}#idea-{idea_id}" if self.editor_url else ""

    def thread_url(self, thread_id: str) -> str:
        if not self.thread_url_template:
            return ""
        return self.thread_url_template.format(thread_id=thread_id)

    def _link(self, idea_id: str) -> str:
        url = self.idea_url(idea_id)
        return LINK_SUFFIX.format(url=url) if url else ""


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------
def _as_view(store: Any) -> StoreView:
    """Accept a ``Store``, a ``StoreView`` or ``None``; always read-only here."""
    if store is None:
        return StoreView.empty()
    if isinstance(store, StoreView):
        return store
    view = getattr(store, "view", None)
    if callable(view):
        return view()
    raise TypeError(f"expected a StoreView or a Store, got {type(store).__name__}")


def _is_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes")


def resolve_canonical(by_id: Mapping[str, Mapping[str, Any]],
                      idea_id: str) -> tuple[str | None, bool]:
    """Follow ``dupe_of`` to the item whose merit governs the thread.

    Returns ``(canonical_id, cycle)``. A cycle is a **review item**, never an
    exception: the roadmap is a file a human edits, and a bot that crashes on a
    typo is worse than one that asks. A ``dupe_of`` pointing at an id that does
    not exist stops at the last real item.
    """
    seen: list[str] = []
    current = idea_id
    while True:
        if current in seen:
            return None, True
        seen.append(current)
        idea = by_id.get(current)
        if idea is None:
            return (current if len(seen) == 1 else seen[-2]), False
        nxt = idea.get("dupe_of")
        if not nxt or nxt == current:
            return current, False
        if nxt not in by_id:
            return current, False
        current = str(nxt)


def _status_label(status: str) -> str:
    """A plain-language label. Deliberately terse; wording is review-owned."""
    return {
        "confirmed": "in progress",
        "manual": "needs manual finishing",
        "design": "needs design input",
        "wip": "up next",
        "soon": "soon",
        "later": "later",
        "planned": "under consideration",
        "unlikely": "not likely to be implemented",
        "implemented": "shipped, in testing",
        "awarded": "shipped, merit awarded",
    }.get(status, status)


def _linked_idea_id(thread: ForumThread, view: StoreView,
                    roadmap: Snapshot) -> str | None:
    """The store link first, then the idea's own ``discord`` field."""
    linked = view.idea_for_thread(thread.id)
    if linked:
        return linked
    for idea in roadmap.ideas:
        discord = idea.get("discord")
        if isinstance(discord, Mapping) and str(discord.get("thread_id") or "") == thread.id:
            return str(idea.get("id") or "") or None
    return None


def _thread_id_for_idea(idea: Mapping[str, Any], view: StoreView) -> str | None:
    discord = idea.get("discord")
    if isinstance(discord, Mapping) and discord.get("thread_id"):
        return str(discord["thread_id"])
    return view.thread_for_idea(str(idea.get("id") or ""))


def _cap(actions: list[Action], ctx: PlanContext, direction: str) -> Plan:
    """Loop-prevention layer three: report a runaway batch, never execute it."""
    cap = ctx.action_cap if ctx.action_cap is not None else DEFAULT_ACTION_CAP
    writes = [a for a in actions if not isinstance(a, STATE_ONLY)]
    if cap >= 0 and len(writes) > cap:
        reason = (f"{len(writes)} actions planned, over the per-run cap of {cap}; "
                  f"aborting and reporting instead of executing a runaway batch")
        actions = actions + [ReviewItem(kind=REVIEW_ACTION_CAP, subject=direction,
                                        detail=reason)]
        return Plan(tuple(actions), cap=cap, aborted=True, direction=direction,
                    reason=reason)
    return Plan(tuple(actions), cap=cap, aborted=False, direction=direction)


# --------------------------------------------------------------------------
# Discord -> roadmap
# --------------------------------------------------------------------------
def plan_discord_to_roadmap(roadmap: Snapshot, forum: ForumSnapshot,
                            store: Any = None,
                            context: PlanContext | None = None) -> Plan:
    """Plan what the forums imply for the roadmap. Pure: nothing executes.

    Branches, in order:

    * a thread with no idea  -> create one (``hidden: true``, id per the
      editor's own slug rules, group from the tag, type from the forum, player
      from the identity map, ``discord: {...}``), then carry the opening post
      over as a ``comment``;
    * new replies            -> append a ``comment`` — **never** an edit to
      ``notes``;
    * tag changed            -> update ``group``;
    * thread renamed         -> queue for review, because the id is an anchor
      target for cross-links; the idea is *not* renamed.

    Anything the bot cannot decide (an unrecognised author, an unmapped tag, a
    channel with no type) becomes a :class:`ReviewItem` and blocks only the
    thread it concerns.
    """
    ctx = context or PlanContext()
    view = _as_view(store)
    by_id = roadmap.by_id
    actions: list[Action] = []
    minted: list[str] = []

    for thread in forum.threads:
        idea_id = _linked_idea_id(thread, view, roadmap)

        if idea_id is not None and idea_id not in by_id:
            _review(actions, view, REVIEW_BROKEN_LINK, thread.id,
                    f"thread {thread.id} is linked to idea {idea_id!r}, which is not "
                    f"in the roadmap any more", thread_id=thread.id, idea_id=idea_id)
            continue

        if idea_id is None:
            # ---------------------------------------------------------------
            # [b9-dupes] HOOK SEAM. Duplicate detection runs *here*, before the
            # create-idea branch below: score this thread against every
            # non-`dupe_of` idea and, above the high threshold, hand
            # `_plan_new_idea` a `dupe_of` (resolved transitively via
            # `resolve_canonical`, never pointing at another dupe row) plus an
            # `AppendComment` on the canonical item. `[b9]` is unpickable until
            # its thresholds are answered in `[r6]`, so nothing scores here yet
            # and `_plan_new_idea` takes `dupe_of=None`. The store already
            # carries the admin decisions b9 needs (`DECISION_DUPE_REMOVED`), so
            # slotting it in adds a call, not a rewrite.
            # ---------------------------------------------------------------
            _plan_new_idea(actions, thread, roadmap, view, ctx, minted)
            continue

        idea = by_id[idea_id]
        _plan_thread_replies(actions, thread, idea_id, view, ctx, forum.bot_user_id)
        _plan_tag_change(actions, thread, idea, view, ctx)
        _plan_rename(actions, thread, idea, view)

    return _cap(actions, ctx, "discord->roadmap")


def _review(actions: list[Action], view: StoreView, kind: str, subject: str,
            detail: str, **extra: Any) -> None:
    """Queue a question once. A review already in the store is not re-raised."""
    item = ReviewItem(kind=kind, subject=subject, detail=detail, **extra)
    if view.has_review(item.review_key):
        return
    if any(isinstance(a, ReviewItem) and a.review_key == item.review_key
           for a in actions):
        return
    actions.append(item)


def _plan_new_idea(actions: list[Action], thread: ForumThread, roadmap: Snapshot,
                   view: StoreView, ctx: PlanContext, minted: list[str],
                   dupe_of: str | None = None) -> None:
    """The create-idea branch. Every unknown is a review item, never a guess."""
    if thread.archived or thread.locked:
        # A thread that was already closed before the bot ever saw it is
        # history, not an inbox. Backfilling it is the admin's call.
        return

    player = ctx.players.get(thread.author_id)
    if not player:
        # NEVER add a name to the roadmap's `players:` list.
        _review(actions, view, REVIEW_UNKNOWN_AUTHOR, thread.author_id,
                f"Discord author of thread {thread.id} ({thread.title!r}) is not in "
                f"the identity map; the idea was not created and no name was added "
                f"to players:", thread_id=thread.id)
        return
    if player not in roadmap.known_players:
        _review(actions, view, REVIEW_PLAYER_NOT_ON_ROSTER, player,
                f"identity map points thread {thread.id} at player {player!r}, who is "
                f"not on the roadmap roster", thread_id=thread.id)
        return

    if not ctx.tag_groups:
        _review(actions, view, REVIEW_TAG_MAPPING_MISSING, "tag_groups",
                "no forum-tag -> group mapping was supplied (config.TAG_GROUPS or "
                "--tag-map), so no idea can be filed")
        return
    group = ctx.group_for_tags(thread.tag_names)
    if not group:
        _review(actions, view, REVIEW_UNMAPPED_TAG, thread.id,
                f"thread {thread.id} carries tags {list(thread.tag_names)} and none "
                f"maps to a roadmap group", thread_id=thread.id)
        return

    item_type = ctx.channel_types.get(thread.channel_id)
    if not item_type:
        _review(actions, view, REVIEW_UNKNOWN_CHANNEL, thread.channel_id,
                f"forum channel {thread.channel_id} has no item type "
                f"(bugs => Defect, feature requests => Enhancement)",
                thread_id=thread.id)
        return

    taken = list(roadmap.by_id) + minted
    idea_id = mint_idea_id(thread.title, taken)
    if not idea_id:
        _review(actions, view, REVIEW_UNSLUGGABLE_TITLE, thread.id,
                f"thread title {thread.title!r} slugifies to nothing", thread_id=thread.id)
        return
    minted.append(idea_id)

    idea: dict[str, Any] = {
        "id": idea_id,
        "title": thread.title,
        "group": group,
        "status": NEW_IDEA_STATUS,
        # Hidden until a human has looked at it: a bot-filed idea must never
        # appear on the published roadmap unreviewed.
        "hidden": True,
        "type": item_type,
        "player": player,
        "discord": {
            "thread_id": thread.id,
            "channel_id": thread.channel_id,
            "url": thread.url or ctx.thread_url(thread.id),
        },
    }
    if dupe_of:  # [b9-dupes] only; b6 never sets this.
        idea["dupe_of"] = dupe_of
    actions.append(CreateIdea(idea=idea, thread_id=thread.id,
                              channel_id=thread.channel_id))

    starter = thread.starter
    if starter is not None and starter.content.strip():
        actions.append(_comment_action(idea_id, thread, starter, ctx))


def _comment_action(idea_id: str, thread: ForumThread, message: ForumMessage,
                    ctx: PlanContext) -> AppendComment:
    url = thread.url or ctx.thread_url(thread.id)
    where = f" in {thread.title}" + (f" ({url})" if url else "")
    text = COMMENT_TEMPLATE.format(
        author=message.author_name or message.author_id,
        where=where, body=message.content.strip())
    return AppendComment(idea_id=idea_id, text=text, thread_id=thread.id,
                         message_id=message.id)


def _plan_thread_replies(actions: list[Action], thread: ForumThread, idea_id: str,
                         view: StoreView, ctx: PlanContext, bot_user_id: str) -> None:
    """New replies become ``comments``. Never ``notes``, which is the admin's."""
    for message in thread.all_messages:
        # Layer one: anything the bot itself said is not news.
        if bot_user_id and message.author_id == bot_user_id:
            continue
        if not message.content.strip():
            continue
        field_name = f"comment:{message.id}"
        # Layer two: already carried over at this exact text.
        if view.unchanged(idea_id, field_name, _comment_action(idea_id, thread,
                                                               message, ctx).text):
            continue
        if view.seen(idea_id, field_name) and not message.edited:
            continue
        actions.append(_comment_action(idea_id, thread, message, ctx))


def _plan_tag_change(actions: list[Action], thread: ForumThread,
                     idea: Mapping[str, Any], view: StoreView,
                     ctx: PlanContext) -> None:
    """Tag changed => update ``group``. No mapping => nothing, not a guess."""
    if not ctx.tag_groups or not thread.tag_names:
        return
    idea_id = str(idea.get("id"))
    group = ctx.group_for_tags(thread.tag_names)
    if not group:
        _review(actions, view, REVIEW_UNMAPPED_TAG, thread.id,
                f"thread {thread.id} carries tags {list(thread.tag_names)} and none "
                f"maps to a roadmap group", thread_id=thread.id, idea_id=idea_id)
        return
    if group == idea.get("group"):
        return
    if view.unchanged(idea_id, "group", group):
        return  # already planned/applied at this value; the roadmap will catch up
    actions.append(UpdateIdeaField(idea_id=idea_id, field_name="group", value=group,
                                   previous=idea.get("group"), thread_id=thread.id))


def _plan_rename(actions: list[Action], thread: ForumThread,
                 idea: Mapping[str, Any], view: StoreView) -> None:
    """A renamed thread is a **review item**: the id is an anchor target.

    ``#idea-<id>`` anchors are linked from other items' notes and ``dupe_of``
    points at the id, so the bot never renames the idea to chase a thread title.
    """
    idea_id = str(idea.get("id"))
    title = (thread.title or "").strip()
    if title == str(idea.get("title") or "").strip():
        return
    key = f"{REVIEW_THREAD_RENAMED}:{thread.id}:{content_hash(title)[:12]}"
    _review(actions, view, REVIEW_THREAD_RENAMED, thread.id,
            f"thread {thread.id} is now titled {title!r} but idea {idea_id!r} is "
            f"{idea.get('title')!r}; the id is an anchor target, so the idea was "
            f"not renamed", thread_id=thread.id, idea_id=idea_id, review_key=key)


# --------------------------------------------------------------------------
# Roadmap -> Discord
# --------------------------------------------------------------------------
def plan_roadmap_to_discord(roadmap: Snapshot, forum: ForumSnapshot,
                            store: Any = None,
                            context: PlanContext | None = None) -> Plan:
    """Plan what the roadmap implies for the forums. Pure: nothing executes.

    Branches, in order:

    * an open, non-hidden, non-``dupe_of`` idea with no thread -> create one,
      tagged from ``group``;
    * ``merit_awarded: true`` on the **governing** item (``dupe_of`` resolved
      transitively) -> post the merit award, then archive **and lock**;
    * ``status: unlikely`` -> post and archive **without** locking;
    * any other status change -> post.

    The bot only ever *reads* ``merit_awarded``; it is never planned as a write
    (:class:`UpdateIdeaField` refuses it outright).
    """
    ctx = context or PlanContext()
    view = _as_view(store)
    by_id = roadmap.by_id
    threads = forum.by_id
    actions: list[Action] = []

    for idea in roadmap.ideas:
        idea_id = str(idea.get("id") or "")
        if not idea_id:
            continue
        thread_id = _thread_id_for_idea(idea, view)
        thread = threads.get(thread_id) if thread_id else None

        if thread is None:
            if thread_id:
                continue  # linked, but not in this snapshot: nothing to say
            _plan_new_thread(actions, idea, view, ctx)
            continue

        canonical, cycle = resolve_canonical(by_id, idea_id)
        if cycle:
            _review(actions, view, REVIEW_DUPE_CYCLE, idea_id,
                    f"dupe_of from {idea_id!r} runs in a circle; the thread's merit "
                    f"cannot be resolved", idea_id=idea_id, thread_id=thread.id)
            continue
        governing = by_id.get(canonical or idea_id, idea)

        # merit_awarded is the close signal — the boolean, not the status.
        if _is_true(governing.get("merit_awarded")):
            _plan_merit_close(actions, idea_id, thread, governing, view, ctx)
            continue

        if idea.get("status") == "unlikely":
            _plan_unlikely(actions, idea_id, thread, view, ctx)
            continue

        _plan_status_post(actions, idea_id, idea, thread, view, ctx)

    return _cap(actions, ctx, "roadmap->discord")


def _plan_new_thread(actions: list[Action], idea: Mapping[str, Any],
                     view: StoreView, ctx: PlanContext) -> None:
    """Open, non-hidden, non-``dupe_of`` items only — the backfill rule."""
    idea_id = str(idea.get("id"))
    if _is_true(idea.get("hidden")) or idea.get("dupe_of"):
        return
    if _is_true(idea.get("merit_awarded")):
        return
    status = str(idea.get("status") or "")
    if status in TERMINAL_STATUSES:
        return
    if status not in STATUSES:
        _review(actions, view, REVIEW_UNKNOWN_STATUS, idea_id,
                f"idea {idea_id!r} has status {status!r}, which is not one of the ten",
                idea_id=idea_id)
        return

    item_type = str(idea.get("type") or "")
    channel_id = ctx.channel_for_type(item_type)
    if not channel_id:
        _review(actions, view, REVIEW_NO_CHANNEL_FOR_TYPE, item_type or idea_id,
                f"no forum channel is mapped to type {item_type!r}, so no thread can "
                f"be opened for {idea_id!r}", idea_id=idea_id)
        return

    if not ctx.tag_groups:
        _review(actions, view, REVIEW_TAG_MAPPING_MISSING, "tag_groups",
                "no forum-tag -> group mapping was supplied (config.TAG_GROUPS or "
                "--tag-map), so no thread can be tagged")
        return
    tags = ctx.tags_for_group(str(idea.get("group") or ""))
    if not tags:
        _review(actions, view, REVIEW_UNMAPPED_TAG, idea_id,
                f"group {idea.get('group')!r} has no forum tag mapped to it",
                idea_id=idea_id)
        return

    from nwnbot.render import html_to_md, truncate_for_discord

    body = html_to_md(idea.get("notes")) if idea.get("notes") else ""
    body = truncate_for_discord(THREAD_BODY.format(body=body, link=ctx._link(idea_id)),
                                editor_url=ctx.idea_url(idea_id))
    actions.append(CreateThread(idea_id=idea_id, channel_id=channel_id,
                                title=str(idea.get("title") or idea_id), body=body,
                                tag_names=tags, status=status))


def _plan_merit_close(actions: list[Action], idea_id: str, thread: ForumThread,
                      governing: Mapping[str, Any], view: StoreView,
                      ctx: PlanContext) -> None:
    """``merit_awarded: true`` => post the award, then archive **and lock**."""
    canonical_id = str(governing.get("id") or idea_id)
    item_type = str(governing.get("type") or "")
    merit = MERIT_BY_TYPE.get(item_type, 0)
    if not view.unchanged(idea_id, "merit_awarded", canonical_id):
        text = MERIT_MESSAGE.format(
            merit=merit, points="point has" if merit == 1 else "points have",
            type=item_type or "item", link=ctx._link(canonical_id))
        actions.append(PostMessage(thread_id=thread.id, idea_id=idea_id, text=text,
                                   kind="merit", field_name="merit_awarded",
                                   value=canonical_id))
    if not (thread.archived and thread.locked):
        actions.append(ArchiveThread(thread_id=thread.id, idea_id=idea_id, locked=True,
                                     reason=f"merit awarded on {canonical_id}"))


def _plan_unlikely(actions: list[Action], idea_id: str, thread: ForumThread,
                   view: StoreView, ctx: PlanContext) -> None:
    """``unlikely`` => post and archive, deliberately **without** locking."""
    if not view.unchanged(idea_id, "status", "unlikely"):
        actions.append(PostMessage(
            thread_id=thread.id, idea_id=idea_id,
            text=UNLIKELY_MESSAGE.format(link=ctx._link(idea_id)),
            kind="unlikely", field_name="status", value="unlikely"))
    if not thread.archived:
        actions.append(ArchiveThread(thread_id=thread.id, idea_id=idea_id, locked=False,
                                     reason="status: unlikely"))


def _plan_status_post(actions: list[Action], idea_id: str, idea: Mapping[str, Any],
                      thread: ForumThread, view: StoreView, ctx: PlanContext) -> None:
    status = str(idea.get("status") or "")
    if status not in STATUSES:
        _review(actions, view, REVIEW_UNKNOWN_STATUS, idea_id,
                f"idea {idea_id!r} has status {status!r}, which is not one of the ten",
                idea_id=idea_id, thread_id=thread.id)
        return
    if view.unchanged(idea_id, "status", status):
        return
    if not view.seen(idea_id, "status"):
        # First sighting of a thread the bot did not open: adopt it quietly.
        actions.append(RecordBaseline(idea_id=idea_id, field_name="status",
                                      value=status))
        return
    if thread.archived:
        return  # never post into an archived thread; reopening is the admin's call
    actions.append(PostMessage(
        thread_id=thread.id, idea_id=idea_id,
        text=STATUS_MESSAGE.format(status=status, label=_status_label(status),
                                   link=ctx._link(idea_id)),
        kind="status", field_name="status", value=status))


# --------------------------------------------------------------------------
# Dry-run simulation — how "no-op on the second pass" becomes a test
# --------------------------------------------------------------------------
def simulate(plan: Plan, roadmap: Snapshot, forum: ForumSnapshot,
             view: StoreView | None = None,
             context: PlanContext | None = None
             ) -> tuple[Snapshot, ForumSnapshot, StoreView]:
    """Apply a plan to its own inputs, purely, and hand back the new state.

    Nothing here talks to Discord or the roadmap: this is what the world *would*
    look like, used by the round-trip tests and available to ``plan`` for a
    multi-pass dry run. Applying a plan and re-planning must produce nothing.
    """
    ctx = context or PlanContext()
    view = StoreView.empty() if view is None else view
    ideas = copy.deepcopy(list(roadmap.ideas))
    by_id = {str(i.get("id")): i for i in ideas if i.get("id")}
    threads = {t.id: t for t in forum.threads}
    links: dict[str, str] = {}
    hashes: dict[tuple[str, str], str] = {}
    reviews: list[str] = []

    for action in plan.actions:
        if isinstance(action, CreateIdea):
            new = copy.deepcopy(dict(action.idea))
            ideas.append(new)
            by_id[action.idea_id] = new
        elif isinstance(action, AppendComment):
            idea = by_id.get(action.idea_id)
            if idea is not None:
                idea.setdefault("comments", []).append(
                    {"author": ctx.bot_user_id or "nwnbot", "date": "", "text": action.text})
        elif isinstance(action, UpdateIdeaField):
            idea = by_id.get(action.idea_id)
            if idea is not None:
                idea[action.field_name] = action.value
        elif isinstance(action, CreateThread):
            thread_id = f"sim-{action.idea_id}"
            starter = ForumMessage(id=f"{thread_id}-0",
                                   author_id=ctx.bot_user_id or "nwnbot",
                                   content=action.body, is_starter=True)
            threads[thread_id] = ForumThread(
                id=thread_id, channel_id=action.channel_id, title=action.title,
                author_id=ctx.bot_user_id or "nwnbot", tag_names=action.tag_names,
                starter=starter)
            links[thread_id] = action.idea_id
            idea = by_id.get(action.idea_id)
            if idea is not None:
                idea["discord"] = {"thread_id": thread_id,
                                   "channel_id": action.channel_id, "url": ""}
        elif isinstance(action, PostMessage):
            thread = threads.get(action.thread_id)
            if thread is not None:
                message = ForumMessage(id=f"{thread.id}-{len(thread.messages) + 1}",
                                       author_id=ctx.bot_user_id or "nwnbot",
                                       content=action.text)
                threads[thread.id] = replace(thread,
                                             messages=thread.messages + (message,))
        elif isinstance(action, ArchiveThread):
            thread = threads.get(action.thread_id)
            if thread is not None:
                threads[thread.id] = replace(thread, archived=True,
                                             locked=thread.locked or action.locked)

        effects = action.effects()
        for thread_id, (idea_id, _channel) in effects.links.items():
            links[thread_id] = idea_id
        for key, value in effects.hashes.items():
            hashes[key] = content_hash(value)
        for item in effects.reviews:
            reviews.append(item.review_key)

    new_roadmap = dataclasses.replace(roadmap, ideas=ideas)
    new_forum = forum.with_threads(threads.values())
    new_view = view.evolve(links=links, hashes=hashes, reviewed=reviews)
    return new_roadmap, new_forum, new_view


__all__ = [
    "ADMIN_ONLY_FIELDS",
    "CREATION_ONLY_FIELDS",
    "ADMIN_ONLY_STATUSES",
    "Action",
    "AppendComment",
    "ArchiveThread",
    "COMMENT_TEMPLATE",
    "CreateIdea",
    "CreateThread",
    "DEFAULT_ACTION_CAP",
    "Effects",
    "ID_MAX_LEN",
    "MERIT_MESSAGE",
    "NEW_IDEA_STATUS",
    "Plan",
    "PlanContext",
    "PostMessage",
    "RecordBaseline",
    "REVIEW_ACTION_CAP",
    "REVIEW_BROKEN_LINK",
    "REVIEW_DUPE_CYCLE",
    "REVIEW_NO_CHANNEL_FOR_TYPE",
    "REVIEW_PLAYER_NOT_ON_ROSTER",
    "REVIEW_TAG_MAPPING_MISSING",
    "REVIEW_THREAD_RENAMED",
    "REVIEW_UNKNOWN_AUTHOR",
    "REVIEW_UNKNOWN_CHANNEL",
    "REVIEW_UNKNOWN_STATUS",
    "REVIEW_UNMAPPED_TAG",
    "REVIEW_UNSLUGGABLE_TITLE",
    "ReviewItem",
    "STATE_ONLY",
    "STATUSES",
    "STATUS_MESSAGE",
    "TERMINAL_STATUSES",
    "UNLIKELY_MESSAGE",
    "UpdateIdeaField",
    "mint_idea_id",
    "plan_discord_to_roadmap",
    "plan_roadmap_to_discord",
    "resolve_canonical",
    "shorten_id",
    "simulate",
    "slugify_id",
    "unique_idea_id",
]
