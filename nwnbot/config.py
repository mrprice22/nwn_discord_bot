"""Configuration: environment, tag->group mapping, player identity map.

Filled in by ``[b5-config]``. Will hold:

- env loading (``ROADMAP_BASE_URL``, ``ROADMAP_USER``, ``ROADMAP_PASSWORD``,
  ``DISCORD_BOT_TOKEN``, ``DISCORD_GUILD_ID``, ``DISCORD_BUGS_FORUM_ID``,
  ``DISCORD_FEATURES_FORUM_ID``, ``NWNBOT_DB``, ``NWNBOT_DRY_RUN``);
- the literal forum-tag-name -> roadmap group-id dict, validated at startup
  against ``vocab`` from ``/api/data`` and the forum's ``available_tags``;
- the player identity map (``players.json``: discord user id -> roadmap player
  name). An unmatched author is queued for review, never auto-added.

Secrets are only ever read from the environment; nothing is hard-coded here.
The tag names and channel ids are still open review items (``r2``, ``r3``), so
the mapping itself is deliberately not written yet.
"""

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

# Merit value by roadmap item type.
MERIT_BY_TYPE: dict[str, int] = {"Defect": 1, "Enhancement": 2, "Exploit": 3}

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
ENV_DISCORD_BUGS_FORUM_ID = "DISCORD_BUGS_FORUM_ID"
ENV_DISCORD_FEATURES_FORUM_ID = "DISCORD_FEATURES_FORUM_ID"
ENV_NWNBOT_DB = "NWNBOT_DB"
ENV_NWNBOT_DRY_RUN = "NWNBOT_DRY_RUN"

# Fallback used when the bot runs on the same host as the editor.
LOCAL_ROADMAP_BASE_URL = "http://127.0.0.1:8765"

# Default local state database path (overridden by $NWNBOT_DB).
DEFAULT_DB_PATH = "state.db"
