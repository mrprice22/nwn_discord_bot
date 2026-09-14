"""Configuration: environment, the tag -> group mapping, the player identity map.

Shipped by ``[b5-config]``. Three surfaces live here, and nothing else in the
package is allowed to invent any of them:

1. **Environment.** :class:`Settings` names every variable the bot reads and
   reads them from a :class:`~typing.Mapping` handed in by the caller. Nothing
   here imports ``dotenv`` and nothing here ever *loads* ``.env`` — the
   environment arrives from the shell or from the systemd unit's
   ``EnvironmentFile=``. No token, password, channel id or user id is written
   down in this file.
2. **The forum-tag-name -> roadmap group-id mapping** (:data:`TAG_GROUPS`),
   answered by review item ``[r2]`` on 2026-09-05 and mirrored byte-for-byte
   from ``tag-map.json`` at the repo root. Tag names are not secret. The map is
   validated against ``vocab`` from ``/api/data`` **and** against the forums'
   ``available_tags``, and drift on either side is a loud failure, never a
   guess. ``--tag-map PATH`` overrides it without a code change.
3. **The player identity map** (``players.json``, gitignored). A Discord user
   id -> roadmap player name dictionary. **Only explicit id entries resolve.**
   The seeder can propose candidates from the parentheticals already in
   ``players:``, but a candidate is inert until a human moves it: player
   identity is merit money, and a wrong match pays the wrong player.

Forum -> type is settled and one-way: ``#bugs`` => ``Defect``,
``#feature-requests`` => ``Enhancement``. **There is no ``Exploit`` tag**
(review item ``[r3]``): an exploit is reported as an ordinary bug and the admin
promotes it to ``type: Exploit`` in the editor. The bot must therefore never
write ``type`` on an *existing* idea — see :data:`CREATION_ONLY_FIELDS`, which
``nwnbot.sync.UpdateIdeaField`` and ``nwnbot.roadmap.assert_ideas_writable``
both enforce. Without that, one later sync would silently demote a promoted
exploit from 3 merit back to 1.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# The 12 roadmap group ids, verified against nwn_homers_lotr/roadmap.yaml.
GROUP_IDS: tuple[str, ...] = (
    "forge",
    "combat-classes",
    "bosses",
    "progression",
    "travel",
    "banking",
    "wiki-tools",
    "quests-areas",
    "items-gear",
    "meaningwave",
    "economy",
    "qol",
)

#: Forum tag name -> roadmap group id. The admin's own 12 tag names, supplied
#: 2026-09-05 in answer to review item ``[r2]`` and committed as
#: ``tag-map.json``; this dict is that file, folded in so the bot has a real
#: mapping without needing a file on disk. **Both forums carry the same 12
#: tags**, so one mapping serves both. Eleven lines are near-verbatim matches
#: to the group ``title``; ``Companions/Henchmen`` -> ``meaningwave``
#: ("Meaningwave Companions") was matched by elimination and is the one line
#: the admin flagged as worth a second look.
TAG_GROUPS: dict[str, str] = {
    "Forge & Crafting": "forge",
    "Combat & Classes": "combat-classes",
    "Bosses & Difficulty": "bosses",
    "Bestiary/Achievement": "progression",
    "Teleports & Travel": "travel",
    "Banking & Storage": "banking",
    "Wiki & Tools": "wiki-tools",
    "Quests & Areas": "quests-areas",
    "Items & Gear": "items-gear",
    "Companions/Henchmen": "meaningwave",
    "Economy & Merit": "economy",
    "QualityOfLife/Buffs": "qol",
}

#: The mapping's canonical on-disk home, and the ``--tag-map`` default target.
#: Kept in the repo so a drifted forum can be re-answered without a code change.
TAG_MAP_PATH = "tag-map.json"

# Merit value by roadmap item type.
MERIT_BY_TYPE: dict[str, int] = {"Defect": 1, "Enhancement": 2, "Exploit": 3}

# --------------------------------------------------------------------------
# Who gets a Discord thread — [b8-backfill], settled 2026-09-05
#
# One policy, shared by the one-off `backfill` and the live `serve` loop,
# because they run the same planner. A filter that lived only in the command
# would let `serve` try to open a thread for every open item on its first cycle,
# blow the action cap and abort every run — jamming the Discord -> roadmap
# direction too, since a cycle aborts whole.
#
#   * A **player's** item earns a thread at any open status. Someone is waiting
#     to hear back, whether the item is `wip` or still `planned`.
#   * A **staff** item earns one only once it is `soon` or beyond. The admin does
#     not need notifying about their own backlog, and 89 of the 125 open staff
#     items are `planned`/`later` — threads that would sit dead in the forum.
#
# Measured 2026-09-05 against the live roadmap: 100 of 189 open items eligible,
# 64 player-reported and 36 staff near-term.
# --------------------------------------------------------------------------

#: Roadmap `player` names that are staff rather than reporters. Nothing in
#: roadmap.yaml marks a role — `players:` is a flat list of names — so this is
#: an explicit list, not something derived. A name absent here is treated as a
#: player, which is the generous direction: the cost of being wrong is one extra
#: thread, not a missed notification. **An item with no `player` at all counts as
#: staff**, since an unattributed item is the admin's own. Review item [r16].
STAFF_PLAYERS: frozenset[str] = frozenset({"HomelessSon (Server Admin)"})

#: The statuses at which a *staff* item earns a thread — "soon or beyond".
#: This is the ordered prefix of STATUSES down to `soon`, minus the terminal
#: ones; asserted below so inserting a status upstream cannot silently change
#: the policy.
STAFF_THREAD_STATUSES: frozenset[str] = frozenset({
    "implemented", "confirmed", "manual", "design", "wip", "soon",
})

#: Seconds between thread creations during a backfill batch. Discord's forum
#: create endpoint is rate-limited and a hundred threads back to back is exactly
#: the shape that trips it; 2s is the floor the item settled on.
BACKFILL_MIN_INTERVAL = 2.0

#: How many times one thread creation is retried after a 429 before the batch
#: gives up. Waits double each time, starting from the server's own
#: `retry_after` when it sends one.
BACKFILL_MAX_RETRIES = 5

#: Forum -> item type. Settled: the *forum*, never the tag, decides the type.
BUGS_ITEM_TYPE = "Defect"
FEATURES_ITEM_TYPE = "Enhancement"

#: The only two types the bot may ever write. ``Exploit`` is deliberately not
#: here (``[r3]``): it is worth 3 merit and is an admin promotion in the editor.
BOT_WRITABLE_TYPES: frozenset[str] = frozenset({BUGS_ITEM_TYPE, FEATURES_ITEM_TYPE})

#: Fields that may be written when an idea is *created* and never updated
#: afterwards.
#:
#: ``type`` is here because of ``[r3]``: the admin promotes an exploit by hand,
#: and an update would revert it.
#:
#: ``triage`` is here because clearing it IS the approval, and the approval is
#: the admin's. The bot marks a new report as awaiting one and must never be
#: able to decide the answer -- including by accident, which is why this is a
#: construction-time refusal rather than a convention.
CREATION_ONLY_FIELDS: frozenset[str] = frozenset({"type", "triage"})

#: Who produced a ``dupe_candidates`` row, recorded on every entry.
#:
#: Not bookkeeping for its own sake: when a better model appears the question is
#: "which suggestions are worth regenerating", and that is only answerable if
#: each row says what made it. Bump the suffix when the scoring CHANGES, not
#: when the file is edited — a stale id is worse than none.
DUPE_SCORER_ID: str = "stdlib-token-v1"

#: The local LLM that judges duplicates. Unset means the feature is off and the
#: token scorer is used alone -- a supported state, not a broken one.
#: llama.cpp on this box; see windows/run-llama.ps1.
ENV_LLM_BASE_URL = "NWNBOT_LLM_BASE_URL"
#: Pinned in the environment rather than discovered, so swapping the model shows
#: up as a visible config change instead of a silent change in behaviour.
ENV_LLM_MODEL = "NWNBOT_LLM_MODEL"
ENV_LLM_API_KEY = "NWNBOT_LLM_API_KEY"
ENV_LLM_TIMEOUT = "NWNBOT_LLM_TIMEOUT"

# --------------------------------------------------------------------------
# Duplicate detection — [b9-dupes], answered by review item [r6]
#
# The bot **never writes** `dupe_of`. These values choose how loudly it
# proposes, and nothing else. A duplicate becomes real only when a DM or admin
# sets `dupe_of` in the roadmap editor.
#
#   below DUPE_LOW_THRESHOLD   silence
#   low .. high                a review-queue entry; nothing said in Discord
#   at/above DUPE_HIGH_THRESHOLD  that entry, plus a line in the reporter's
#                              thread — but only if DUPE_POST_IN_THREAD is on
#
# In every band the new idea is created normally, with no `dupe_of`, so a false
# positive can never swallow a real report.
#
# ---- These numbers are MEASURED, not proposed --------------------------------
# `python -m nwnbot dupes --calibrate --roadmap-yaml <path>` was run against the
# real roadmap (404 non-dupe ideas, and the 5 `dupe_of` rows already in it as
# ground truth). What it showed:
#
#   * The 5 known duplicate pairs score 0.10-0.51 (title_weight 0.3). Only 2 of
#     the 5 rank their true canonical first; the others land at #4, #19 and #93.
#   * Scoring every idea as if it were a fresh thread, the top-1 match is a false
#     positive by construction. That top-1 is >= 0.20 for 53% of them, >= 0.30
#     for 18%, >= 0.50 for 10%.
#
# So the honest summary is: **on this corpus a token scorer buys roughly 20%
# recall at a 10% false-positive rate.** The real duplicates here are
# paraphrases ("rest-menu teleport back to where you last ported" vs "expand
# rest-menu teleports"), and lexical overlap cannot see them. The strongest
# lexical signals are the opposite — template-titled siblings ("Prestige quest:
# Pale Master (L11+)" vs "Prestige quest: Weapon Master (L13+)") which score 0.83
# and are deliberately distinct items.
#
# [r6]'s proposed 0.55/0.85 would have found **none** of the five.
#
# Hence DUPE_POST_IN_THREAD below. See future-llm-dupe-matching.md: recovering
# the other 80% needs semantic matching, and this measurement is the evidence
# for it.
# --------------------------------------------------------------------------

#: Below this, a candidate is not worth mentioning at all. 0.50 is the knee of
#: the measured false-positive curve: ~10% of new threads file an entry.
DUPE_LOW_THRESHOLD = 0.50

#: At or above this the reporter would be told — gated by DUPE_POST_IN_THREAD.
DUPE_HIGH_THRESHOLD = 0.85

#: **Off, deliberately, on the evidence above.** A scorer that is right about
#: one duplicate in five has not earned the right to tell a player their report
#: may already be tracked; being told "this is probably a duplicate" wrongly is
#: worse than being told nothing. The review queue still gets every candidate,
#: so the admin sees them all. Turn this on when the matcher can carry it —
#: which on this corpus means semantic matching, not a bigger number.
DUPE_POST_IN_THREAD = False

#: How `nwnbot.dupes.score` splits title similarity against token overlap.
#: 0.3, measured: higher weights reward shared title boilerplate, which is what
#: the roadmap's template-titled families are made of.
DUPE_TITLE_WEIGHT = 0.3

#: `notes` is truncated to this many characters before tokenizing. Measured
#: against the real roadmap.yaml: notes are p90 1,028 chars and run to 4,347,
#: and a long note dilutes its token set until it matches every other long note.
DUPE_NOTES_MAX_CHARS = 800

#: How many candidates `rank` returns. Only the best is banded; the rest exist
#: so a review entry and `dupes --calibrate` can show near misses.
DUPE_CANDIDATE_LIMIT = 5

# Fields the bot must never write. Enforced as assertions in nwnbot.roadmap.
FORBIDDEN_STATUSES: frozenset[str] = frozenset({"awarded", "implemented", "manual"})
FORBIDDEN_FIELDS: frozenset[str] = frozenset({"merit_awarded", "notes", "impl_notes"})
FORBIDDEN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"meta", "groups", "players", "epics", "redemption", "housing"}
)

# Environment variable names, so callers do not spell them by hand.
ENV_ROADMAP_BASE_URL = "ROADMAP_BASE_URL"
ENV_ROADMAP_USER = "ROADMAP_USER"
ENV_ROADMAP_PASSWORD = "ROADMAP_PASSWORD"
ENV_DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKEN"
ENV_DISCORD_GUILD_ID = "DISCORD_GUILD_ID"
ENV_DISCORD_BOT_USER_ID = "DISCORD_BOT_USER_ID"
ENV_DISCORD_BUGS_FORUM_ID = "DISCORD_BUGS_FORUM_ID"
ENV_DISCORD_FEATURES_FORUM_ID = "DISCORD_FEATURES_FORUM_ID"
#: Cloudflare R2, where rehosted Discord screenshots live. All five or none:
#: see nwnbot.r2.from_env for why half-configured is refused rather than
#: degraded. Unset everywhere means rehosting is off and images are reported as
#: present-but-not-kept -- never written as a signed link that dies in a day.
ENV_R2_ACCOUNT_ID = "R2_ACCOUNT_ID"
ENV_R2_BUCKET = "R2_BUCKET"
ENV_R2_ACCESS_KEY_ID = "R2_ACCESS_KEY_ID"
ENV_R2_SECRET_ACCESS_KEY = "R2_SECRET_ACCESS_KEY"
#: The bucket's public custom domain. This is the only R2 value that is ever
#: written into a roadmap item, so it must be a durable credential-free URL.
ENV_R2_PUBLIC_BASE_URL = "R2_PUBLIC_BASE_URL"

ENV_NWNBOT_DB = "NWNBOT_DB"
ENV_NWNBOT_PLAYERS = "NWNBOT_PLAYERS"
ENV_NWNBOT_DRY_RUN = "NWNBOT_DRY_RUN"
ENV_NWNBOT_DUPE_LOW = "NWNBOT_DUPE_LOW"
ENV_NWNBOT_DUPE_HIGH = "NWNBOT_DUPE_HIGH"

# Fallback used when the bot runs on the same host as the editor.
LOCAL_ROADMAP_BASE_URL = "http://127.0.0.1:8765"

# Default local state database path (overridden by $NWNBOT_DB).
DEFAULT_DB_PATH = "state.db"

#: Default player identity map path. Gitignored: it maps real Discord user ids.
DEFAULT_PLAYERS_PATH = "players.json"


class ConfigError(Exception):
    """Configuration is missing, malformed or has drifted from the live systems."""


def _threshold(raw: str, name: str, default: float) -> float:
    """Parse a 0..1 threshold override. Empty means the default; junk is fatal.

    The first numeric setting in this module, and it stays loud on purpose: a
    typo that silently fell back to the default would change how the bot behaves
    with no sign that it had.
    """
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"${name} must be a number in 0..1, got {raw!r}") from None
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"${name} must be in 0..1, got {value!r}")
    return value


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Every environment variable the bot reads, in one object.

    Constructed from a mapping the caller supplies (``os.environ`` on the live
    path, a literal dict in a test), so no code path in this package has to
    reach for the process environment — or for ``.env``, which holds real
    credentials and is never read by anything in ``nwnbot/``.
    """

    roadmap_base_url: str = ""
    roadmap_user: str = ""
    roadmap_password: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_bot_user_id: str = ""
    bugs_forum_id: str = ""
    features_forum_id: str = ""
    db_path: str = DEFAULT_DB_PATH
    players_path: str = DEFAULT_PLAYERS_PATH
    dry_run: bool = True
    dupe_low: float = DUPE_LOW_THRESHOLD
    dupe_high: float = DUPE_HIGH_THRESHOLD

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        def get(name: str, default: str = "") -> str:
            return (env.get(name) or default).strip()

        return cls(
            roadmap_base_url=get(ENV_ROADMAP_BASE_URL) or LOCAL_ROADMAP_BASE_URL,
            roadmap_user=get(ENV_ROADMAP_USER),
            roadmap_password=get(ENV_ROADMAP_PASSWORD),
            discord_bot_token=get(ENV_DISCORD_BOT_TOKEN),
            discord_guild_id=get(ENV_DISCORD_GUILD_ID),
            discord_bot_user_id=get(ENV_DISCORD_BOT_USER_ID),
            bugs_forum_id=get(ENV_DISCORD_BUGS_FORUM_ID),
            features_forum_id=get(ENV_DISCORD_FEATURES_FORUM_ID),
            db_path=get(ENV_NWNBOT_DB) or DEFAULT_DB_PATH,
            players_path=get(ENV_NWNBOT_PLAYERS) or DEFAULT_PLAYERS_PATH,
            # Anything other than a deliberate "0" means plan-only.
            dry_run=get(ENV_NWNBOT_DRY_RUN, "1") != "0",
            dupe_low=_threshold(get(ENV_NWNBOT_DUPE_LOW), ENV_NWNBOT_DUPE_LOW,
                                DUPE_LOW_THRESHOLD),
            dupe_high=_threshold(get(ENV_NWNBOT_DUPE_HIGH), ENV_NWNBOT_DUPE_HIGH,
                                 DUPE_HIGH_THRESHOLD),
        )

    def __post_init__(self) -> None:
        # A band that is empty or inverted would silently change which of the
        # three outcomes every new thread gets, so it is a loud failure.
        if not 0.0 < self.dupe_low < self.dupe_high <= 1.0:
            raise ConfigError(
                f"duplicate thresholds must satisfy 0 < low < high <= 1; got "
                f"low={self.dupe_low!r} high={self.dupe_high!r} "
                f"(${ENV_NWNBOT_DUPE_LOW} / ${ENV_NWNBOT_DUPE_HIGH})")

    def channel_types(self) -> dict[str, str]:
        """Forum channel id -> item type. Ids come from the environment only."""
        types: dict[str, str] = {}
        if self.bugs_forum_id:
            types[self.bugs_forum_id] = BUGS_ITEM_TYPE
        if self.features_forum_id:
            types[self.features_forum_id] = FEATURES_ITEM_TYPE
        return types


def channel_types(env: Mapping[str, str]) -> dict[str, str]:
    """Convenience wrapper: ``Settings.from_env(env).channel_types()``."""
    return Settings.from_env(env).channel_types()


# --------------------------------------------------------------------------
# The tag -> group mapping, and its validation
# --------------------------------------------------------------------------
def load_tag_groups(path: str | Path | None = None) -> dict[str, str]:
    """The mapping, from ``path`` when given and from :data:`TAG_GROUPS` when not.

    A file may be either a bare ``{tag: group}`` object or the ``tag-map.json``
    shape with the mapping under ``tag_groups`` (which lets the file carry a
    ``_comment`` block).
    """
    if not path:
        return dict(TAG_GROUPS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: cannot read a tag map: {exc}") from exc
    mapping = raw.get("tag_groups", raw) if isinstance(raw, Mapping) else raw
    if not isinstance(mapping, Mapping):
        raise ConfigError(f"{path}: expected an object of tag name -> group id")
    return {str(k): str(v) for k, v in mapping.items()}


def group_problems(vocab_group_ids: Iterable[Any]) -> list[str]:
    """Drift between :data:`GROUP_IDS` and the editor's own ``vocab.groups``."""
    ids = tuple(str(g.get("id") if isinstance(g, Mapping) else g)
                for g in vocab_group_ids or ())
    missing = [g for g in GROUP_IDS if g not in ids]
    extra = [g for g in ids if g not in GROUP_IDS]
    if missing or extra:
        return [f"vocab drift — missing {missing or '-'}, unexpected {extra or '-'}"]
    return []


def tag_map_problems(mapping: Mapping[str, str] | None, *,
                     available_tags: Mapping[str, Sequence[str]] | None = None,
                     vocab_group_ids: Iterable[Any] | None = None) -> list[str]:
    """Every way the mapping can be wrong, as a list of sentences.

    The single implementation of these checks. ``nwnbot.cli.check_tag_map``
    turns the list into a ``doctor`` line and :func:`validate_tag_map` turns it
    into an exception; neither has a copy of the rules.

    Checked: every group id exists, every group is covered, no group is claimed
    twice, the count is exactly ``len(GROUP_IDS)``, and — when the forums'
    ``available_tags`` is supplied — every mapped tag really exists on a forum.
    """
    if mapping is None:
        return ["no mapping supplied"]
    problems: list[str] = []
    known_groups = set(GROUP_IDS)
    unknown = sorted({g for g in mapping.values() if g not in known_groups})
    if unknown:
        problems.append(f"tags mapped to group ids that do not exist: {unknown}")
    uncovered = [g for g in GROUP_IDS if g not in set(mapping.values())]
    if uncovered:
        problems.append(f"groups with no tag: {uncovered}")
    seen: dict[str, list[str]] = {}
    for tag, group in mapping.items():
        seen.setdefault(group, []).append(tag)
    doubled = {g: t for g, t in seen.items() if len(t) > 1}
    if doubled:
        problems.append(f"groups claimed by more than one tag: {doubled}")
    if len(mapping) != len(GROUP_IDS):
        problems.append(f"{len(mapping)} tag(s) mapped, expected {len(GROUP_IDS)}")
    if available_tags:
        known = {name for names in available_tags.values() for name in names}
        absent = sorted(t for t in mapping if t not in known)
        if absent:
            problems.append(f"tags not present on either forum: {absent}")
    if vocab_group_ids is not None:
        problems.extend(group_problems(vocab_group_ids))
    return problems


def validate_tag_map(mapping: Mapping[str, str] | None, *,
                     available_tags: Mapping[str, Sequence[str]] | None = None,
                     vocab_group_ids: Iterable[Any] | None = None,
                     where: str = "tag map") -> None:
    """Startup validation. Raise :class:`ConfigError` on any drift, loudly.

    Called on the live path before anything is planned, so a forum that gained,
    lost or renamed a tag stops the bot instead of quietly filing every new
    thread under the wrong group.
    """
    problems = tag_map_problems(mapping, available_tags=available_tags,
                                vocab_group_ids=vocab_group_ids)
    if problems:
        raise ConfigError(
            f"{where} has drifted from the live systems:\n  - "
            + "\n  - ".join(problems)
            + f"\nFix the forum tags or supply a corrected --tag-map "
              f"(canonical copy: {TAG_MAP_PATH}). Nothing was planned.")


# --------------------------------------------------------------------------
# The player identity map
# --------------------------------------------------------------------------
#: ``"Sync (Shync)"`` -> name ``"Sync (Shync)"``, alias ``"Shync"``. Several of
#: the 19 roster entries have no parenthetical at all, and one ("Server Admin")
#: is a role rather than a handle, so the alias is a *hint* and never a match.
_PARENTHETICAL = re.compile(r"^(?P<head>.+?)\s*\((?P<alias>[^()]+)\)\s*$")


@dataclass(frozen=True)
class PlayerCandidate:
    """One roster entry, split into what it says and what it might mean."""

    roadmap_name: str          # exactly as it appears in `players:`
    head: str                  # the part before the parenthetical
    alias: str | None = None   # the parenthetical, when there is one

    @property
    def has_alias(self) -> bool:
        return bool(self.alias)


def split_player_entry(entry: str) -> PlayerCandidate:
    """Split one ``players:`` entry. No parenthetical is the normal case."""
    name = str(entry).strip()
    match = _PARENTHETICAL.match(name)
    if not match:
        return PlayerCandidate(roadmap_name=name, head=name, alias=None)
    return PlayerCandidate(roadmap_name=name,
                           head=match.group("head").strip(),
                           alias=match.group("alias").strip() or None)


#: What the seeder writes at the top of ``players.json``. It says out loud what
#: the file can and cannot do, because the next person to open it will be
#: tempted to paste a display name into ``discord_ids``.
PLAYERS_DOC_COMMENT = [
    "Player identity map. discord_ids is the ONLY section the bot reads.",
    "A Discord user id is a 17-19 digit snowflake. Nothing in roadmap.yaml",
    "contains one, so seeding CANNOT fill discord_ids in - it can only list",
    "candidates. Fill discord_ids by hand (right-click a user in Discord with",
    "developer mode on -> Copy User ID) and move the roster name across.",
    "candidates[] holds the parentheticals already in the roadmap's players:",
    "list - they are DISPLAY NAMES or, in at least one case, a role. They are",
    "inert: the bot never matches on them. An author with no discord_ids entry",
    "is queued for review and never auto-added to players:.",
    "This file is gitignored and must never be committed.",
]


def seed_players_document(roster: Iterable[str]) -> dict[str, Any]:
    """Build a fresh ``players.json`` document from a ``players:`` roster.

    Produces an **empty** ``discord_ids`` map plus a candidate list, and that
    is the honest ceiling: the roster contains names and display names, never
    Discord user ids, so no id -> name pair can be derived from it. Seeding
    saves the typing of the right-hand side, not the identification.
    """
    candidates: list[dict[str, Any]] = []
    for entry in roster:
        cand = split_player_entry(entry)
        if not cand.roadmap_name:
            continue
        candidates.append({
            "roadmap_name": cand.roadmap_name,
            "alias": cand.alias,
            "note": ("no parenthetical in the roster — nothing to go on"
                     if not cand.has_alias else
                     "parenthetical is a display name, not a Discord user id"),
        })
    return {
        "_comment": list(PLAYERS_DOC_COMMENT),
        "discord_ids": {},
        "candidates": candidates,
    }


@dataclass(frozen=True)
class PlayerMap:
    """Discord user id -> roadmap player name, plus inert candidates.

    :meth:`resolve` looks at ``ids`` and nothing else. That is the whole safety
    property: an alias never resolves, so a coincidence of display names can
    never credit merit to the wrong player.
    """

    ids: Mapping[str, str] = field(default_factory=dict)
    candidates: tuple[PlayerCandidate, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ids",
                           {str(k): str(v) for k, v in dict(self.ids).items()})
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def resolve(self, discord_user_id: str) -> str | None:
        """The roadmap player name for this Discord user id, or ``None``.

        ``None`` means "queue this author for review", never "guess".
        """
        return self.ids.get(str(discord_user_id)) or None

    @property
    def unresolved_candidates(self) -> tuple[PlayerCandidate, ...]:
        claimed = set(self.ids.values())
        return tuple(c for c in self.candidates if c.roadmap_name not in claimed)

    @classmethod
    def from_document(cls, doc: Any) -> "PlayerMap":
        """Accept the seeded shape, or a plain ``{id: name}`` object."""
        if not isinstance(doc, Mapping):
            raise ConfigError("players.json must contain a JSON object")
        if "discord_ids" in doc or "candidates" in doc:
            ids = doc.get("discord_ids") or {}
            raw_candidates = doc.get("candidates") or []
        else:
            ids = {k: v for k, v in doc.items() if not str(k).startswith("_")}
            raw_candidates = []
        if not isinstance(ids, Mapping):
            raise ConfigError("players.json: discord_ids must be an object")
        candidates = []
        for item in raw_candidates:
            if isinstance(item, Mapping):
                name = str(item.get("roadmap_name") or "")
                if name:
                    candidates.append(split_player_entry(name))
            elif isinstance(item, str) and item:
                candidates.append(split_player_entry(item))
        return cls(ids=ids, candidates=tuple(candidates))

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PlayerMap":
        """Load the map; a missing file is an empty map, not an error."""
        target = Path(path or DEFAULT_PLAYERS_PATH)
        if not target.exists():
            return cls()
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{target}: cannot read the player map: {exc}") from exc
        return cls.from_document(doc)


def write_players_seed(path: str | Path, roster: Iterable[str], *,
                       overwrite: bool = False) -> dict[str, Any]:
    """Write a seeded ``players.json``, preserving any ids already filled in.

    Refuses to clobber a file that already carries ``discord_ids`` unless
    ``overwrite`` is set: those pairs are hand-made and cannot be regenerated.
    """
    target = Path(path)
    doc = seed_players_document(roster)
    if target.exists() and not overwrite:
        doc["discord_ids"] = dict(PlayerMap.load(target).ids)
    target.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return doc


__all__ = [
    "BOT_WRITABLE_TYPES",
    "DUPE_CANDIDATE_LIMIT",
    "DUPE_HIGH_THRESHOLD",
    "DUPE_LOW_THRESHOLD",
    "DUPE_NOTES_MAX_CHARS",
    "DUPE_POST_IN_THREAD",
    "BACKFILL_MAX_RETRIES",
    "BACKFILL_MIN_INTERVAL",
    "STAFF_PLAYERS",
    "STAFF_THREAD_STATUSES",
    "DUPE_TITLE_WEIGHT",
    "BUGS_ITEM_TYPE",
    "CREATION_ONLY_FIELDS",
    "DUPE_SCORER_ID",
    "ENV_LLM_BASE_URL",
    "ENV_LLM_MODEL",
    "ENV_LLM_API_KEY",
    "ENV_LLM_TIMEOUT",
    "ConfigError",
    "DEFAULT_DB_PATH",
    "DEFAULT_PLAYERS_PATH",
    "FEATURES_ITEM_TYPE",
    "FORBIDDEN_FIELDS",
    "FORBIDDEN_STATUSES",
    "FORBIDDEN_TOP_LEVEL_KEYS",
    "GROUP_IDS",
    "LOCAL_ROADMAP_BASE_URL",
    "MERIT_BY_TYPE",
    "PLAYERS_DOC_COMMENT",
    "PlayerCandidate",
    "PlayerMap",
    "Settings",
    "TAG_GROUPS",
    "TAG_MAP_PATH",
    "channel_types",
    "group_problems",
    "load_tag_groups",
    "seed_players_document",
    "split_player_entry",
    "tag_map_problems",
    "validate_tag_map",
    "write_players_seed",
]
