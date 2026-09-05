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

#: Forum -> item type. Settled: the *forum*, never the tag, decides the type.
BUGS_ITEM_TYPE = "Defect"
FEATURES_ITEM_TYPE = "Enhancement"

#: The only two types the bot may ever write. ``Exploit`` is deliberately not
#: here (``[r3]``): it is worth 3 merit and is an admin promotion in the editor.
BOT_WRITABLE_TYPES: frozenset[str] = frozenset({BUGS_ITEM_TYPE, FEATURES_ITEM_TYPE})

#: Fields that may be written when an idea is *created* and never updated
#: afterwards. ``type`` is the whole list and the reason is ``[r3]``: the admin
#: promotes an exploit by hand, and an update would revert it.
CREATION_ONLY_FIELDS: frozenset[str] = frozenset({"type"})

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
ENV_NWNBOT_DB = "NWNBOT_DB"
ENV_NWNBOT_PLAYERS = "NWNBOT_PLAYERS"
ENV_NWNBOT_DRY_RUN = "NWNBOT_DRY_RUN"

# Fallback used when the bot runs on the same host as the editor.
LOCAL_ROADMAP_BASE_URL = "http://127.0.0.1:8765"

# Default local state database path (overridden by $NWNBOT_DB).
DEFAULT_DB_PATH = "state.db"

#: Default player identity map path. Gitignored: it maps real Discord user ids.
DEFAULT_PLAYERS_PATH = "players.json"


class ConfigError(Exception):
    """Configuration is missing, malformed or has drifted from the live systems."""


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
        )

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
    "BUGS_ITEM_TYPE",
    "CREATION_ONLY_FIELDS",
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
