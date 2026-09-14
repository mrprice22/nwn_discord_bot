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
from nwnbot import dupes
from nwnbot.config import MERIT_BY_TYPE
from nwnbot.forum import ForumMessage, ForumSnapshot, ForumThread
from nwnbot.render import md_to_html
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

#: Fields the bot must never write.
#:
#: ``impl_notes``/``impl_notes_h`` are the developer/implementation notes: the
#: admin's own working record of HOW something was built, which nothing in a
#: Discord thread can inform. ``merit_awarded`` is the merit DB's own receipt.
#:
#: ``notes`` was on this list until 2026-09-14 and is deliberately NOT any
#: more. It is the reporter-facing description -- the field the admin fills by
#: hand-copying the Discord thread into it, which is the very job this bot
#: exists to take over -- and it is the only field whose HTML renders images.
#: Blocking it was the wrong reading of "the admin's field".
ADMIN_ONLY_FIELDS = frozenset({"impl_notes", "impl_notes_h", "merit_awarded"})

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
#: Settled by review item [r11].2 — approved. A new triage status would have
#: been a schema change, and so a reopening of [r1].
NEW_IDEA_STATUS = "planned"

#: Phrases that turn a mention into an attribution. Matched immediately either
#: side of the mention, because both orders occur in practice: "requested by
#: @X" and "@X suggested". Deliberately a short, literal list -- a looser
#: pattern would catch "thanks @X" and "@X can you confirm", which are not
#: attributions at all.
ON_BEHALF_BEFORE = ("requested by", "suggested by", "reported by",
                    "on behalf of", "asked for by", "raised by", "for")
ON_BEHALF_AFTER = ("suggested", "requested", "reported", "asked for",
                   "raised this", "wants", "would like")

#: The mention itself, as Discord stores it. Matched on the RAW form: the
#: planner sees content after nwnbot.bot.resolve_mentions has rewritten it for
#: humans, so this also has to cope with the resolved `@Name` shape — which it
#: does by reading the id, and falling back to no attribution when there is no
#: id left to read. An unattributed idea is the safe outcome.
MENTION_RE = re.compile(r"<@!?(\d+)>")

#: Discord's hard limit on a forum thread name. A roadmap title can be a whole
#: sentence -- 41 of 423 are over this -- and the API rejects the create
#: outright with "In name: Must be between 1 and 100 in length", so the thread
#: is simply never made. Cut on a word boundary and mark it, because a title
#: that stops mid-word reads like the report was damaged.
DISCORD_THREAD_TITLE_MAX = 100

#: Ids show up in URLs, `dupe_of` pickers and conflict messages; the editor
#: trims to 60 (`roadmap-editor.py:4584`, `shortenId(slugifyId(title), 60)`).
ID_MAX_LEN = 60

# --------------------------------------------------------------------------
# Player-visible strings
#
# Settled by review item [r11].1. The admin owns every string a player can
# read; these are the approved wordings, not placeholders. Changing one is a
# review decision, not a refactor. Nothing reaches a player until `apply --yes`
# runs with NWNBOT_DRY_RUN=0.
# --------------------------------------------------------------------------

#: Posted in a thread when the linked item's status changes. The raw status id
#: trails the label because it is the word the editor and the roadmap page use,
#: so a player who goes looking finds the same term.
STATUS_MESSAGE = "Roadmap update — this is now **{label}** ({status}).{link}"

#: Posted when the governing item's merit has really been paid. The thread is
#: archived *and locked* straight after.
MERIT_MESSAGE = (
    "This has shipped, and {merit} merit {points} been awarded for it "
    "({type}). Thanks for the report — closing this thread.{link}"
)

#: Posted when an item is marked `unlikely`. Archived, deliberately NOT locked
#: — and the wording says so, because an unlocked archive is easy to miss.
UNLIKELY_MESSAGE = (
    "Logged, but not likely to be implemented. Archiving the thread — it stays "
    "readable and unlocked, so add to it if you disagree.{link}"
)

#: Leads the opening post of a thread the bot opens from an existing roadmap
#: item. [b8-backfill] opens one per open item, so this line gets read ~100
#: times in a row by the same people: it has to carry information rather than
#: explain the mechanism, which stops being news after the second thread.
#: Attribution is the part that is actually useful and differs every time — it
#: tells the reporter their report was kept, and everyone else whose it was.
THREAD_HEADER = "Reported by: {player} on {date}."

#: When the roadmap has no `player`. Rare — 178 of 179 open items have one —
#: but "Reported by: ." would read worse than saying so.
UNKNOWN_PLAYER = "an unmatched reporter"

#: `date` is only filled in for shipped items, so roughly one open item in
#: seven has none. Saying so is honest; inventing one, or dropping the clause,
#: would both imply the date is known.
UNKNOWN_DATE = "Unknown date"

#: Opening post of a thread the bot creates from an existing roadmap item.
#: ``body`` already carries THREAD_HEADER — see :func:`_plan_new_thread`, which
#: joins them so an item with empty ``notes`` does not leave a blank gap.
THREAD_BODY = "{body}{link}"

#: The internal, never-rendered `comments` entry a Discord message becomes.
#: Settled by [r11].1 alongside the rest, though the blast radius is small:
#: only the admin ever sees the `comments` list.
COMMENT_TEMPLATE = "Discord — {author}{where}:\n\n{body}"

#: Images carried over from a Discord message, appended to the internal comment.
#: Only ever REHOSTED urls: the signed Discord link expires inside a day, and
#: writing one would look correct in review and 404 later (see
#: nwnbot/attachments.py). Internal-only text — the `comments` list is never
#: rendered publicly.
ATTACHMENT_BLOCK = "\n\nImages:\n{lines}"
ATTACHMENT_LINE = "- {url}"
#: An image that could not be rehosted. Named rather than skipped silently, so
#: the admin knows the report had a screenshot and that it is now only in
#: Discord — and deliberately WITHOUT the signed link, which would be dead by
#: the time anyone clicked it.
ATTACHMENT_LOST = "- ({filename} — not rehosted; see the thread, the Discord link expires)"

#: Appended to a Discord-bound message as a link back to the item.
LINK_SUFFIX = "\n\n{url}"

#: PROVISIONAL WORDING — review item [r14].
#: Posted once in a *new* thread whose report scored above DUPE_HIGH_THRESHOLD
#: against an existing item. It is a **question, not a verdict**: the thread's
#: own idea has already been created and the reporter keeps their credit. Only
#: a DM or admin setting `dupe_of` in the editor makes a duplicate real.
DUPE_HINT_MESSAGE = (
    "This looks like it may already be tracked as **{title}** — an admin will "
    "check. Either way your report is logged and stays yours.{link}"
)

#: PROVISIONAL WORDING — review item [r14].
#: Posted once after a human has confirmed the duplicate by setting `dupe_of`.
#: Says plainly that the thread stays open and the credit stays with the
#: reporter, because "duplicate" reads like "dismissed" everywhere else.
DUPE_CONFIRMED_MESSAGE = (
    "Tracked as the same issue as **{title}**, which is where updates will "
    "appear — you are credited as a requester there. Merit for the fix goes to "
    "the original report, but you can still earn merit by helping test it when "
    "it ships.{link}"
)

#: Posted once when the admin approves a report and it joins the published
#: roadmap. The reporter has heard nothing since they filed it, so this is the
#: first news they get: it happened, and updates will follow here.
APPROVED_MESSAGE = "Added to the roadmap: **{title}** — {outlook}{link}"

#: What each lane means for the reporter, in plain language.
#:
#: Deliberately NOT the board's lane name. "now **later**" read as an adverb --
#: "now, later" -- and even fixed up to name the lane properly it still asked
#: the reporter to learn the board's vocabulary in order to find out the one
#: thing they want to know: when. So the lane name is gone and each sentence
#: just answers that, in the admin's own words.
#:
#: Four of the five are the same sentence with the timing swapped, which is the
#: point: a reporter who has filed twice can tell two outcomes apart at a
#: glance, without reading carefully.
APPROVED_OUTLOOK = {
    "confirmed": "this is being worked on now.",
    "wip": "this is scheduled to be done next.",
    "soon": "this is scheduled to be done soon.",
    "later": "this is scheduled to be done later.",
    "planned": "this is under consideration.",
}

#: For a lane with no entry above. Says the true thing and promises nothing --
#: in particular it does not invent a timing the admin has not committed to.
APPROVED_OUTLOOK_DEFAULT = "you will get an update here as it moves."

#: The internal note left on the *canonical* item when a duplicate is confirmed,
#: so the extra demand shows up where the admin actually works. Never rendered.
DUPE_CANONICAL_COMMENT = (
    "Also reported by {player} in Discord{where}. Tracked as duplicate "
    "{idea_id}."
)


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
#: The opening post names someone else as the person who actually asked for
#: this. Filed as a question, never applied: `player` decides who is paid merit
#: when the item ships, and two of the five real mentions in this roadmap were
#: NOT attribution -- one a question addressed to a player, one a note that
#: somebody else had also hit the bug. Reading "the first mention wins" would
#: have paid the wrong person twice.
REVIEW_ON_BEHALF = "on_behalf_of"
#: A new thread scored inside the duplicate band. Always a proposal: the idea
#: was created normally and no `dupe_of` was written. Resolving the entry
#: without setting `dupe_of` is how you say "no" — a resolved review is never
#: re-raised, because `Store.view()` loads every status, not just the open ones.
REVIEW_POSSIBLE_DUPE = "possible_dupe"
#: A `dupe_of` the bot had already announced to the reporter has been removed.
#: The player was told something that is no longer true; the bot does not post
#: a retraction on its own, it asks.
REVIEW_DUPE_UNLINKED = "dupe_unlinked"
#: A new report that closely resembles work already shipped. NOT a duplicate
#: proposal: the admin does not reopen an awarded item, so this is filed as
#: its own story and the resemblance is reported as context — a regression or
#: a follow-up to something already delivered, which is worth seeing.
REVIEW_DUPE_ECHO = "dupe_echo"
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


def thread_title(title: str) -> str:
    """A roadmap title cut to something Discord will accept as a thread name."""
    text = " ".join((title or "").split())
    if len(text) <= DISCORD_THREAD_TITLE_MAX:
        return text
    cut = text[:DISCORD_THREAD_TITLE_MAX - 1]
    space = cut.rfind(" ")
    # Only honour a word boundary past halfway; a very long first word would
    # otherwise leave almost nothing.
    if space > DISCORD_THREAD_TITLE_MAX // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


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
                  for f in ("title", "group", "status", "type", "triage")}
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
    triage: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_names", tuple(self.tag_names))

    @property
    def key(self) -> str:
        return f"create_thread:{self.idea_id}"

    def effects(self) -> Effects:
        # No link yet: the thread id only exists once Discord has answered, so
        # the executor links it (and checkpoints it into the idea's `discord`
        # field) at that point. Recording the status baseline here stops the
        # very next run announcing a status nobody changed. `triage` is
        # baselined for exactly the same reason: the bot has now SEEN this
        # idea's approval state, so a later cycle must not read the absence of
        # a hash as "it was just approved".
        return Effects(hashes={(self.idea_id, "status"): self.status,
                               (self.idea_id, "triage"): self.triage})

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
    # [b9-dupes]. Thresholds are an input like everything else here: config.py
    # owns the values, cli.py passes them, and nothing in this module imports
    # them. Zero thresholds mean the scorer never runs at all, which is what a
    # PlanContext() built by hand in a test gets unless it asks for otherwise.
    dupe_low: float = 0.0
    dupe_high: float = 0.0
    dupe_title_weight: float = 0.6
    dupe_notes_max: int = 800
    dupe_candidates: int = 5
    # Whether the high band may speak to the player at all. Off by default and
    # off in config: measured recall does not justify telling a reporter their
    # report may be a duplicate. The review entry is filed either way.
    dupe_post_in_thread: bool = False
    #: Stamped onto every `dupe_candidates` row so a later model upgrade can
    #: tell which suggestions predate it. An input like every other setting.
    dupe_scorer: str = cfg.DUPE_SCORER_ID
    #: thread id -> a one-paragraph summary for the new idea's `notes`.
    #: Written by the model BEFORE planning, because a planner is pure and a
    #: network call inside one would end that. Missing or empty simply means
    #: the report's own words are used, which is never wrong -- only longer.
    summaries: Mapping[str, str] = field(default_factory=dict)
    # [b8-backfill]. Who earns a Discord thread. An empty `staff_players` means
    # nobody is staff, so every open item qualifies — the pre-b8 behaviour, which
    # keeps every test written before this policy planning what it always did.
    staff_players: frozenset[str] = frozenset()
    staff_thread_statuses: frozenset[str] = frozenset()
    #: Restrict the run to these idea ids. Empty means "no restriction", which
    #: is the normal case. This exists so a first live run can be one item
    #: wide: the action cap ABORTS a plan rather than trimming it, so there is
    #: otherwise no way to execute a single action out of a large plan, and
    #: "just try one and look at it" is the only safe way to approach a batch
    #: that posts to a player-facing forum with no undo.
    only: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tag_groups", dict(self.tag_groups))
        object.__setattr__(self, "channel_types", dict(self.channel_types))
        object.__setattr__(self, "players", dict(self.players))
        object.__setattr__(self, "summaries", dict(self.summaries))
        object.__setattr__(self, "staff_players", frozenset(self.staff_players))
        object.__setattr__(self, "staff_thread_statuses",
                           frozenset(self.staff_thread_statuses))
        object.__setattr__(self, "only", frozenset(self.only or ()))

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

    def earns_thread(self, idea: Mapping[str, Any]) -> bool:
        """Does this item earn a Discord thread? ``[b8-backfill]``, settled.

        Called only after the caller has established the item is open — not
        hidden, not a dupe row, merit unpaid, status not terminal. This answers
        the remaining question: *is anyone waiting to hear about it?*

        A **player's** item qualifies at any open status; someone reported it and
        is owed an answer whether it is `wip` or still `planned`. A **staff**
        item qualifies only at `soon` or beyond, because the admin does not need
        notifying about their own backlog. An item with **no player** counts as
        staff: an unattributed item is the admin's own.

        With no staff configured this returns True for everything, which is what
        a hand-built PlanContext in a test gets.
        """
        if not self.staff_players:
            return True
        player = str(idea.get("player") or "")
        if player and player not in self.staff_players:
            return True
        return str(idea.get("status") or "") in self.staff_thread_statuses

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
    if ctx.only:
        # Narrow BEFORE the cap, so `--only` makes an over-cap plan executable
        # rather than merely reporting a smaller abort. Both planners funnel
        # through here, so one filter covers both directions.
        actions = [a for a in actions if getattr(a, "idea_id", "") in ctx.only]
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
    candidates: tuple[dupes.Prepared, ...] | None = None

    for thread in forum.threads:
        idea_id = _linked_idea_id(thread, view, roadmap)

        if idea_id is not None and idea_id not in by_id:
            _review(actions, view, REVIEW_BROKEN_LINK, thread.id,
                    f"thread {thread.id} is linked to idea {idea_id!r}, which is not "
                    f"in the roadmap any more", thread_id=thread.id, idea_id=idea_id)
            continue

        if idea_id is None:
            # [b9-dupes]. Score first, create unconditionally, propose second.
            # `_plan_new_idea` is called with `dupe_of=None` on every path and
            # in every band: review item [r6] settled that the bot never writes
            # `dupe_of` at all, because a wrong merge silently steals a player's
            # merit credit. The parameter stays on the signature as the seam a
            # later policy would use, and a test asserts nothing passes it.
            if candidates is None:  # built once per run, and only if needed
                candidates = _dupe_candidates(roadmap, ctx)
            best = _best_dupe(thread, candidates, ctx)
            echo = _best_dupe(thread, candidates, ctx, shipped=True)
            new_id = _plan_new_idea(actions, thread, roadmap, view, ctx, minted,
                                    candidates=_dupe_candidate_rows(
                                        thread, candidates, ctx))
            if new_id and best is not None:
                _plan_dupe_hint(actions, thread, new_id, best, view, ctx)
            if new_id and echo is not None:
                _plan_dupe_echo(actions, thread, new_id, echo, view, ctx)
            continue

        idea = by_id[idea_id]
        _plan_thread_replies(actions, thread, idea_id, view, ctx, forum.bot_user_id)
        _plan_tag_change(actions, thread, idea, view, ctx)
        _plan_rename(actions, thread, idea, view)

    return _cap(actions, ctx, "discord->roadmap")


def unlinked_threads(roadmap: Snapshot, forum: ForumSnapshot,
                     store: Any = None) -> list[ForumThread]:
    """Threads with no roadmap idea yet — the ones a cycle would create from.

    Public and pure so the engine can ask the model about exactly these, before
    planning, without duplicating the link resolution or reaching into the
    planner. An archived thread is excluded for the same reason
    `_plan_new_idea` skips it: it is history, not an inbox.
    """
    view = _as_view(store)
    out = []
    for thread in forum.threads:
        if thread.archived or thread.locked:
            continue
        if _linked_idea_id(thread, view, roadmap) is None:
            out.append(thread)
    return out


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


def report_date(created_at: str) -> str:
    """A thread's creation time as the roadmap's ``YYYY-MM-DD``, or "".

    The roadmap stores a plain date; Discord hands over a full ISO timestamp.
    Anything unparseable yields "" rather than a guess: a wrong date on a card
    is worse than none, because nothing downstream can tell it is wrong.
    """
    text = (created_at or "").strip()
    if not text:
        return ""
    head = text[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        try:
            int(head[:4]), int(head[5:7]), int(head[8:10])
        except ValueError:
            return ""
        return head
    return ""


def credited_to(text: str, players: Mapping[str, str]) -> str:
    """The player this report was filed ON BEHALF OF, or "".

    Reads only an explicit attribution: a mention with one of a short list of
    phrases immediately before or after it. Both orders occur -- "requested by
    @X" and "@X suggested" -- and both are in this roadmap already.

    Matches the RESOLVED form (`@Sync (Shync)`) as well as the raw one
    (`<@139...>`), because nwnbot.bot.resolve_mentions rewrites the text for
    human readers before a planner ever sees it. Matching only the raw id would
    have made this silently dead on the live path while passing every test.

    A bare mention is NOT an attribution and must not be read as one. Of the
    five real mentions in this roadmap two were something else: a question
    addressed to a player ("@Balendin -- I assume this happened as you logged
    in") and a note that someone else had also hit the bug. "First mention
    wins" would have moved merit credit to the wrong person in both.
    """
    body = " ".join((text or "").split())
    if not body or "@" not in body:
        return ""

    # (start, end, player) for every mention, in either shape. Names are tried
    # longest first so "Sync (Shync)" wins over a shorter name it contains.
    found: list[tuple[int, int, str]] = []
    for match in MENTION_RE.finditer(body):
        name = players.get(match.group(1))
        if name:
            found.append((match.start(), match.end(), name))
    for name in sorted(set(players.values()), key=len, reverse=True):
        needle = "@" + name
        at = body.find(needle)
        while at != -1:
            span = (at, at + len(needle))
            if not any(a <= at < b for a, b, _ in found):
                found.append((span[0], span[1], name))
            at = body.find(needle, at + 1)
    found.sort()

    for start, end, name in found:
        before = body[:start].lower().rstrip(" :-")
        after = body[end:].lower().lstrip(" :-,")
        if any(before.endswith(cue) for cue in ON_BEHALF_BEFORE):
            return name
        if any(after.startswith(cue) for cue in ON_BEHALF_AFTER):
            return name
    return ""


def _plan_new_idea(actions: list[Action], thread: ForumThread, roadmap: Snapshot,
                   view: StoreView, ctx: PlanContext, minted: list[str],
                   dupe_of: str | None = None,
                   candidates: Sequence[Mapping[str, Any]] = ()) -> str | None:
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
    # Awaiting the admin's approval. Cleared in the editor, never here: the bot
    # can say "someone should look at this" and can never answer it.
    idea["triage"] = True
    # When it was reported. The roadmap shows `date` on the card and the
    # backfilled thread header quotes it, and neither has anything to say
    # without this -- a brand-new idea would read "Unknown date" on the very
    # day it was filed. The thread's creation time IS the report date.
    reported = report_date(thread.created_at)
    if reported:
        idea["date"] = reported
    # The description the admin would otherwise write by hand from the thread.
    # Falls back to the reporter's own words, which is the thing being
    # described: using them verbatim is never wrong, only longer.
    # Filed on someone else's behalf? Asked, never assumed: `player` decides
    # who is paid when the item ships. The idea is still created, credited to
    # the thread's author, and the question goes to the review queue.
    on_behalf = credited_to(
        thread.starter.content if thread.starter else "", ctx.players)
    if on_behalf and on_behalf != player:
        _review(actions, view, REVIEW_ON_BEHALF, thread.id,
                f"thread {thread.id} ({thread.title!r}) was opened by {player!r} "
                f"but its first post credits {on_behalf!r}. Filed as {idea_id!r} "
                f"crediting {player!r}; change `player` in the editor if the "
                f"submitter credit belongs to {on_behalf!r}",
                thread_id=thread.id, idea_id=idea_id)

    summary = (ctx.summaries.get(thread.id) or "").strip()
    if not summary and thread.starter is not None:
        summary = (thread.starter.content or "").strip()
    if summary:
        idea["notes"] = md_to_html(summary)
    if candidates:
        idea["dupe_candidates"] = [dict(c) for c in candidates]
    if dupe_of:  # [b9-dupes] only; b6 never sets this.
        idea["dupe_of"] = dupe_of
    actions.append(CreateIdea(idea=idea, thread_id=thread.id,
                              channel_id=thread.channel_id))

    starter = thread.starter
    if starter is not None and _worth_carrying(starter):
        actions.append(_comment_action(idea_id, thread, starter, ctx))

    # The id, so the caller can tell an idea was really minted. Every guard
    # above returns None instead, and [b9-dupes] leans on the difference: a
    # duplicate hint must never name an idea that was never created.
    return idea_id


# --------------------------------------------------------------------------
# [b9-dupes] — duplicate detection. Every outcome is a proposal.
#
# Review item [r6], answered 2026-09-05: the bot **never writes `dupe_of`**.
# Three bands, and the new idea is created identically in all three, so a false
# positive can never swallow a real report:
#
#   below ctx.dupe_low    silence
#   low .. high           a review-queue entry; nothing said in Discord
#   at/above ctx.dupe_high  that entry, plus one line in the reporter's thread
#
# A duplicate becomes real only when a DM or admin sets `dupe_of` in the editor.
# `_plan_confirmed_dupe` below is what the bot does *after* that happens.
# --------------------------------------------------------------------------
def _dupe_scoring_on(ctx: PlanContext) -> bool:
    """Thresholds default to zero, so a hand-built PlanContext scores nothing."""
    return ctx.dupe_low > 0.0 and ctx.dupe_high >= ctx.dupe_low


def _dupe_candidates(roadmap: Snapshot, ctx: PlanContext) -> tuple[dupes.Prepared, ...]:
    """Every non-`dupe_of` idea, notes flattened once for the whole run.

    Flattening HTML notes is the expensive half of scoring, so it happens once
    per plan rather than once per (thread, candidate) pair.
    """
    if not _dupe_scoring_on(ctx):
        return ()
    return dupes.prepare(roadmap.ideas, notes_max=ctx.dupe_notes_max)


def _best_dupe(thread: ForumThread, candidates: Sequence[dupes.Prepared],
               ctx: PlanContext, *,
               shipped: bool = False) -> dupes.Candidate | None:
    """The closest existing idea, or ``None`` when nothing clears the low band.

    ``shipped`` selects which pool to look in: ``False`` is the merge-candidate
    pool and ``True`` the echo pool. They are scored the same way and reported
    very differently — see :data:`REVIEW_DUPE_ECHO`.
    """
    candidates = tuple(c for c in candidates if c.shipped is shipped)
    if not candidates:
        return None
    ranked = dupes.rank(thread.title, thread.body, candidates,
                        tag_names=thread.tag_names,
                        title_weight=ctx.dupe_title_weight,
                        limit=ctx.dupe_candidates)
    if not ranked:
        return None
    best = ranked[0]
    return best if best.value >= ctx.dupe_low else None


def _dupe_candidate_rows(thread: ForumThread, candidates: Sequence[dupes.Prepared],
                         ctx: PlanContext) -> list[dict]:
    """The ranked suggestions to store on a newly filed idea.

    Written so the approval tab can show them without re-running a scorer in a
    browser. Advisory only: the bot never writes ``dupe_of``.

    Shipped matches are included and marked ``kind: "echo"``. They are NOT
    merge targets -- the admin does not reopen an awarded idea -- but a report
    that resembles delivered work is usually a regression in it, and that is
    worth seeing next to the report rather than discovering later.
    """
    rows: list[dict] = []
    for shipped in (False, True):
        pool = tuple(c for c in candidates if c.shipped is shipped)
        if not pool:
            continue
        for cand in dupes.rank(thread.title, thread.body, pool,
                               tag_names=thread.tag_names,
                               title_weight=ctx.dupe_title_weight,
                               limit=ctx.dupe_candidates):
            if cand.value < ctx.dupe_low:
                continue
            rows.append({"id": cand.idea_id, "title": cand.title,
                         "score": round(cand.value, 3),
                         "kind": "echo" if shipped else "candidate",
                         "by": ctx.dupe_scorer})
    return rows


def _plan_dupe_hint(actions: list[Action], thread: ForumThread, idea_id: str,
                    best: dupes.Candidate, view: StoreView, ctx: PlanContext) -> None:
    """File the proposal, and above the high band tell the reporter too.

    The review key carries both ids, so resolving it is a durable "no" for that
    exact pair: `Store.view()` loads reviews of *every* status, so `_review`
    never re-raises one that has been resolved.
    """
    key = f"{REVIEW_POSSIBLE_DUPE}:{thread.id}:{best.idea_id}"
    _review(actions, view, REVIEW_POSSIBLE_DUPE, thread.id,
            f"thread {thread.id} ({thread.title!r}) scores {best.value:.2f} against "
            f"idea {best.idea_id!r} ({best.title!r}); filed as {idea_id!r} with no "
            f"dupe_of. Set dupe_of in the editor to confirm, or resolve this entry "
            f"to reject it",
            thread_id=thread.id, idea_id=idea_id, review_key=key)

    if best.value < ctx.dupe_high or not ctx.dupe_post_in_thread:
        # The quiet band: a question for the admin, not for the player. Also
        # where every candidate lands while `dupe_post_in_thread` is off, which
        # is the shipped default — see cfg.DUPE_POST_IN_THREAD for the numbers.
        return
    text = DUPE_HINT_MESSAGE.format(title=best.title, link=ctx._link(best.idea_id))
    actions.append(PostMessage(thread_id=thread.id, idea_id=idea_id, text=text,
                               kind="dupe_hint", field_name="dupe_hint",
                               value=best.idea_id))


def _plan_dupe_echo(actions: list[Action], thread: ForumThread, idea_id: str,
                    echo: dupes.Candidate, view: StoreView, ctx: PlanContext) -> None:
    """Note that a new report resembles work already shipped.

    Deliberately NOT a duplicate proposal and never a `dupe_of`: an awarded item
    is not reopened, so this report is its own story earning its own merit. What
    the admin wants to see is that it may be a regression in, or a follow-up to,
    something already delivered. Nothing is said to the player on this path —
    "we already did that" is exactly the wrong thing to tell someone who just
    hit it again.
    """
    key = f"{REVIEW_DUPE_ECHO}:{thread.id}:{echo.idea_id}"
    _review(actions, view, REVIEW_DUPE_ECHO, thread.id,
            f"thread {thread.id} ({thread.title!r}) scores {echo.value:.2f} against "
            f"{echo.idea_id!r} ({echo.title!r}), which is already shipped. Filed as "
            f"{idea_id!r}, a new story, NOT a duplicate — it may be a regression in "
            f"that work or a follow-up to it",
            thread_id=thread.id, idea_id=idea_id, review_key=key)


def _plan_confirmed_dupe(actions: list[Action], idea: Mapping[str, Any], idea_id: str,
                         canonical: str, thread: ForumThread, roadmap: Snapshot,
                         view: StoreView, ctx: PlanContext) -> bool:
    """A human set `dupe_of`. Say so once, and note the demand on the canonical.

    Returns True when this cycle planned the announcement, so the caller can let
    the status branches run on every *other* cycle. The thread is deliberately
    neither archived nor locked: the reporter keeps their thread and their merit
    credit, and closing follows the canonical item's `merit_awarded` further down.
    """
    if view.unchanged(idea_id, "dupe_of", canonical):
        return False
    target = roadmap.by_id.get(canonical) or {}
    title = str(target.get("title") or canonical)

    text = DUPE_CONFIRMED_MESSAGE.format(title=title, link=ctx._link(canonical))
    actions.append(PostMessage(thread_id=thread.id, idea_id=idea_id, text=text,
                               kind="dupe_confirmed", field_name="dupe_of",
                               value=canonical))

    url = thread.url or ctx.thread_url(thread.id)
    where = f" ({url})" if url else ""
    actions.append(AppendComment(
        idea_id=canonical,
        text=DUPE_CANONICAL_COMMENT.format(
            player=str(idea.get("player") or "an unmatched author"),
            where=where, idea_id=idea_id),
        thread_id=thread.id, field_name=f"dupe_of:{idea_id}"))
    return True


def _comment_action(idea_id: str, thread: ForumThread, message: ForumMessage,
                    ctx: PlanContext) -> AppendComment:
    url = thread.url or ctx.thread_url(thread.id)
    where = f" in {thread.title}" + (f" ({url})" if url else "")
    text = COMMENT_TEMPLATE.format(
        author=message.author_name or message.author_id,
        where=where, body=message.content.strip())
    text += _attachment_block(message)
    return AppendComment(idea_id=idea_id, text=text, thread_id=thread.id,
                         message_id=message.id)


def _worth_carrying(message: ForumMessage) -> bool:
    """Whether a message has anything to record on the idea.

    Text OR an image. Testing ``content`` alone silently dropped every
    screenshot-only post -- Discord sends those with ``content == ""`` -- which
    is the single most common shape of a bug report: a sentence in one message
    and the evidence in the next.
    """
    if message.content.strip():
        return True
    return any(a.is_image for a in message.attachments)


def _attachment_block(message: ForumMessage) -> str:
    """The images from one message, as permanent links. "" when there are none.

    Reads ``permanent_url`` only, which is empty until the bytes have actually
    been copied somewhere that outlives the signed CDN link. Rehosting happens
    before planning precisely so this stays a pure read.
    """
    lines = []
    for item in message.attachments:
        if not item.is_image:
            continue
        if item.permanent_url:
            lines.append(ATTACHMENT_LINE.format(url=item.permanent_url))
        else:
            lines.append(ATTACHMENT_LOST.format(
                filename=item.filename or item.id))
    return ATTACHMENT_BLOCK.format(lines="\n".join(lines)) if lines else ""


def _plan_thread_replies(actions: list[Action], thread: ForumThread, idea_id: str,
                         view: StoreView, ctx: PlanContext, bot_user_id: str) -> None:
    """New replies become ``comments``. Never ``notes``, which is the admin's."""
    for message in thread.all_messages:
        # Layer one: anything the bot itself said is not news.
        if bot_user_id and message.author_id == bot_user_id:
            continue
        if not _worth_carrying(message):
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

        # [b9-dupes]. A human confirmed a duplicate in the editor: say it once,
        # note the demand on the canonical, and let the close path below take
        # over on later cycles. The thread is never archived for being a dupe.
        closing = (_is_true(governing.get("merit_awarded"))
                   or idea.get("status") == "unlikely")
        if idea.get("dupe_of") and canonical and canonical != idea_id and not closing:
            # ...but not when the thread is about to be archived anyway. The
            # real news is one message away, and "this is a duplicate" directly
            # before "this shipped, closing" is noise, not information.
            if _plan_confirmed_dupe(actions, idea, idea_id, canonical, thread,
                                    roadmap, view, ctx):
                continue
        elif not idea.get("dupe_of") and view.seen(idea_id, "dupe_of"):
            # Announced, then un-linked by hand. The reporter has been told
            # something that is no longer true. The bot does not decide to
            # retract — the content hash already stops it re-announcing, so all
            # that is left is to tell the admin a player is holding stale news.
            _review(actions, view, REVIEW_DUPE_UNLINKED, idea_id,
                    f"idea {idea_id!r} was announced in thread {thread.id} as a "
                    f"duplicate and its dupe_of has since been removed; the "
                    f"reporter has not been told otherwise",
                    idea_id=idea_id, thread_id=thread.id)

        # merit_awarded is the close signal — the boolean, not the status.
        if _is_true(governing.get("merit_awarded")):
            _plan_merit_close(actions, idea_id, thread, governing, view, ctx)
            continue

        if idea.get("status") == "unlikely":
            _plan_unlikely(actions, idea_id, thread, view, ctx)
            continue

        # Approval comes before the status branch: an approved report is news
        # even when its status has not moved, and `planned` -> `planned` is
        # exactly what approval usually looks like.
        if _plan_approved_post(actions, idea_id, idea, thread, view, ctx):
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
    # Nobody is waiting on this one yet. Silent, not a review item: it is the
    # normal state of most of the backlog, and it changes on its own the moment
    # the item is promoted. See PlanContext.earns_thread.
    if not ctx.earns_thread(idea):
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
    # The header leads, so it survives the 4000-char cut by construction, and an
    # item with no `notes` gets the header alone rather than a leading blank.
    header = THREAD_HEADER.format(
        player=str(idea.get("player") or "").strip() or UNKNOWN_PLAYER,
        date=str(idea.get("date") or "").strip() or UNKNOWN_DATE)
    body = "\n\n".join(part for part in (header, body.strip()) if part)
    body = truncate_for_discord(THREAD_BODY.format(body=body, link=ctx._link(idea_id)),
                                editor_url=ctx.idea_url(idea_id))
    actions.append(CreateThread(idea_id=idea_id, channel_id=channel_id,
                                title=thread_title(str(idea.get("title") or idea_id)),
                                body=body,
                                tag_names=tags, status=status,
                                triage=_is_true(idea.get("triage"))))


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
        if not thread.archived:
            # Posting REOPENS an archived thread in Discord. The admin closes a
            # thread when they move an idea between #bugs and #feature-requests
            # rather than deleting it, so a closed thread is a deliberate state
            # and often the OLD half of a pair. Announcing merit into one would
            # resurrect it in front of players, next to the live thread that
            # should have received the news.
            actions.append(PostMessage(thread_id=thread.id, idea_id=idea_id,
                                       text=text, kind="merit",
                                       field_name="merit_awarded",
                                       value=canonical_id))
    if not thread.archived:
        actions.append(ArchiveThread(thread_id=thread.id, idea_id=idea_id, locked=True,
                                     reason=f"merit awarded on {canonical_id}"))
    # An already-archived thread is left exactly as it is, even when it is not
    # locked. Discord refuses to modify an archived thread -- "50083: Thread is
    # archived" -- so adding the lock would mean unarchiving it first, which
    # bumps it back to the top of the forum in front of players. A missing lock
    # on a closed thread is not worth reopening it for.


def _plan_unlikely(actions: list[Action], idea_id: str, thread: ForumThread,
                   view: StoreView, ctx: PlanContext) -> None:
    """``unlikely`` => post and archive, deliberately **without** locking."""
    # Same reopening hazard as the merit path: never post into a closed thread.
    if not view.unchanged(idea_id, "status", "unlikely") and not thread.archived:
        actions.append(PostMessage(
            thread_id=thread.id, idea_id=idea_id,
            text=UNLIKELY_MESSAGE.format(link=ctx._link(idea_id)),
            kind="unlikely", field_name="status", value="unlikely"))
    if not thread.archived:
        actions.append(ArchiveThread(thread_id=thread.id, idea_id=idea_id, locked=False,
                                     reason="status: unlikely"))


def _plan_approved_post(actions: list[Action], idea_id: str, idea: Mapping[str, Any],
                        thread: ForumThread, view: StoreView,
                        ctx: PlanContext) -> bool:
    """Say so, once, when a pending report is approved onto the roadmap.

    Returns True when this cycle planned something, so the caller lets the
    status branch run on every other cycle.

    Absence of `triage` is ambiguous on its own -- the editor drops a false
    boolean, so "approved" and "never in the queue" look identical in the YAML.
    The store is what separates them: an idea whose pending state was recorded
    as True and is now absent has been approved; an idea never recorded at all
    never entered the queue, and gets neither a message nor a stored row.

    That second case is load-bearing. Without it the first run after this ships
    would find no `triage` hash for any of the ~420 existing ideas, read every
    one as freshly approved, and post into every thread at once.
    """
    pending = _is_true(idea.get("triage"))
    if view.unchanged(idea_id, "triage", pending):
        return False
    if not view.seen(idea_id, "triage"):
        if not pending:
            # Never been in the queue: an idea that predates it, or one the
            # admin wrote by hand. Not approved -- simply never pending. Say
            # nothing and store nothing, so this does not stamp a baseline row
            # onto all four hundred existing ideas the first time it runs.
            return False
        actions.append(RecordBaseline(idea_id=idea_id, field_name="triage",
                                      value=pending))
        return True
    if pending:
        # Went back INTO the queue. The admin's business; the reporter does not
        # need to hear that their approved idea was un-approved.
        actions.append(RecordBaseline(idea_id=idea_id, field_name="triage",
                                      value=pending, reason="returned to triage"))
        return True
    if thread.archived:
        return False
    status = str(idea.get("status") or "")
    actions.append(PostMessage(
        thread_id=thread.id, idea_id=idea_id,
        text=APPROVED_MESSAGE.format(
            title=str(idea.get("title") or idea_id),
            outlook=APPROVED_OUTLOOK.get(status, APPROVED_OUTLOOK_DEFAULT),
            link=ctx._link(idea_id)),
        kind="approved", field_name="triage", value=pending))
    return True


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
    "APPROVED_MESSAGE",
    "APPROVED_OUTLOOK",
    "CREATION_ONLY_FIELDS",
    "ADMIN_ONLY_STATUSES",
    "Action",
    "AppendComment",
    "ArchiveThread",
    "ATTACHMENT_BLOCK",
    "ATTACHMENT_LINE",
    "ATTACHMENT_LOST",
    "COMMENT_TEMPLATE",
    "DUPE_CANONICAL_COMMENT",
    "DUPE_CONFIRMED_MESSAGE",
    "DUPE_HINT_MESSAGE",
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
    "REVIEW_ON_BEHALF",
    "credited_to",
    "report_date",
    "REVIEW_DUPE_ECHO",
    "REVIEW_DUPE_UNLINKED",
    "REVIEW_NO_CHANNEL_FOR_TYPE",
    "REVIEW_PLAYER_NOT_ON_ROSTER",
    "REVIEW_POSSIBLE_DUPE",
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
    "THREAD_BODY",
    "THREAD_HEADER",
    "UNKNOWN_DATE",
    "UNKNOWN_PLAYER",
    "UNLIKELY_MESSAGE",
    "UpdateIdeaField",
    "mint_idea_id",
    "plan_discord_to_roadmap",
    "plan_roadmap_to_discord",
    "resolve_canonical",
    "shorten_id",
    "simulate",
    "unlinked_threads",
    "slugify_id",
    "thread_title",
    "DISCORD_THREAD_TITLE_MAX",
    "unique_idea_id",
]
