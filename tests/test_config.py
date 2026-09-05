"""Tests for ``nwnbot.config`` — ``[b5-config]``.

Four properties carry the weight here:

1. **The mapping is real and it is the admin's.** The literal dict is
   ``tag-map.json`` byte for byte (review item ``[r2]``), 12 tags onto the 12
   groups, and drift on either side — a renamed forum tag, a moved group — is a
   loud failure rather than a silent misfile.
2. **No secret is in the repo.** No token, password, channel id or Discord user
   id appears in ``config.py`` or in this file. Tag names and roadmap player
   names are not secret; the two forum channel ids live only in ``.env``.
3. **An identity is never guessed.** ``PlayerMap.resolve`` reads the explicit
   Discord-id map and nothing else. A roster parenthetical is a display name
   (and in one case a role), so it seeds a *candidate* a human must confirm.
   Player identity is merit money.
4. **Nothing loads ``.env``.** Every value in this file arrives from a literal
   dict; ``nwnbot`` imports no dotenv (asserted in ``test_scaffold.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nwnbot import config as cfg

REPO = Path(__file__).resolve().parent.parent

#: The roadmap's `players:` list as it stands, copied verbatim from
#: nwn_homers_lotr/roadmap.yaml. Player names are not secret. Note the shape is
#: NOT uniform: 11 of the 19 have no parenthetical at all, and
#: "HomelessSon (Server Admin)" has one that is a role, not a handle.
ROSTER = [
    "community",
    "dc0960 (Dungeon_Crawler)",
    "FLYING HITCHER",
    "Fugdish (Try_this)",
    "McGondy",
    "-Methonash-",
    "Piskan (Alek Cain)",
    "Tukwut",
    "Y a z k i r",
    "Llikanthus",
    "HomelessSon (Server Admin)",
    "Magyk",
    "Zambro (Xil)",
    "Rajmund (Ray)",
    "Szescian82",
    "Sync (Shync)",
    "dormic",
    "Agent51950",
    "Balendin (Balendin_2222)",
]

#: A complete, entirely fake environment. Nothing here is a credential.
FAKE_ENV = {
    "DISCORD_BOT_TOKEN": "fake-token",
    "DISCORD_GUILD_ID": "fake-guild",
    "DISCORD_BOT_USER_ID": "fake-bot-user",
    "DISCORD_BUGS_FORUM_ID": "fake-channel-bugs",
    "DISCORD_FEATURES_FORUM_ID": "fake-channel-features",
    "ROADMAP_BASE_URL": "https://roadmap.example.invalid/",
    "ROADMAP_USER": "fake-user",
    "ROADMAP_PASSWORD": "fake-password",
    "NWNBOT_DB": "state.db",
    "NWNBOT_DRY_RUN": "1",
}


# --------------------------------------------------------------------------
# The tag -> group mapping
# --------------------------------------------------------------------------
def test_the_built_in_map_is_tag_map_json():
    on_disk = json.loads((REPO / "tag-map.json").read_text(encoding="utf-8"))
    assert cfg.TAG_GROUPS == on_disk["tag_groups"]


def test_twelve_tags_onto_twelve_groups():
    assert len(cfg.TAG_GROUPS) == 12
    assert len(cfg.GROUP_IDS) == 12
    assert sorted(cfg.TAG_GROUPS.values()) == sorted(cfg.GROUP_IDS)
    assert cfg.tag_map_problems(cfg.TAG_GROUPS) == []


def test_both_forums_carry_the_same_twelve_tags():
    """`[r3]`: there is no Exploit tag and no per-forum tag."""
    available = {"bugs": tuple(cfg.TAG_GROUPS), "features": tuple(cfg.TAG_GROUPS)}
    assert cfg.tag_map_problems(cfg.TAG_GROUPS, available_tags=available) == []
    assert "Exploit" not in cfg.TAG_GROUPS


BROKEN_MAPPINGS = [
    ("unknown group id",
     dict(cfg.TAG_GROUPS, **{"Forge & Crafting": "no-such-group"}),
     "do not exist"),
    ("a group with no tag",
     {k: v for k, v in cfg.TAG_GROUPS.items() if v != "qol"},
     "groups with no tag"),
    ("two tags claiming one group",
     dict(cfg.TAG_GROUPS, **{"Forge & Crafting": "qol"}),
     "more than one tag"),
    ("thirteen tags",
     dict(cfg.TAG_GROUPS, **{"Brand New Tag": "qol"}),
     "more than one tag"),
    ("empty", {}, "groups with no tag"),
]


@pytest.mark.parametrize("label,mapping,expected",
                         BROKEN_MAPPINGS, ids=[b[0] for b in BROKEN_MAPPINGS])
def test_a_broken_mapping_is_reported_not_repaired(label, mapping, expected):
    problems = cfg.tag_map_problems(mapping)
    assert problems, label
    assert any(expected in p for p in problems), problems
    with pytest.raises(cfg.ConfigError):
        cfg.validate_tag_map(mapping)


def test_a_renamed_forum_tag_fails_loudly():
    """The forum side of the drift check: the mapping is fine, the forum moved."""
    renamed = {k: v for k, v in cfg.TAG_GROUPS.items()}
    available = {"bugs": tuple(t for t in cfg.TAG_GROUPS if t != "Wiki & Tools")}
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_tag_map(renamed, available_tags=available)
    assert "Wiki & Tools" in str(exc.value)


def test_a_moved_group_fails_loudly():
    """The editor side: `vocab.groups` no longer matches GROUP_IDS."""
    vocab = [{"id": g} for g in cfg.GROUP_IDS if g != "banking"]
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.validate_tag_map(cfg.TAG_GROUPS, vocab_group_ids=vocab)
    assert "banking" in str(exc.value)


def test_the_live_vocab_shape_is_accepted_both_ways():
    assert cfg.group_problems([{"id": g, "title": g} for g in cfg.GROUP_IDS]) == []
    assert cfg.group_problems(list(cfg.GROUP_IDS)) == []


def test_a_tag_map_file_overrides_the_built_in_one(tmp_path):
    override = tmp_path / "tags.json"
    override.write_text(json.dumps({f"t-{g}": g for g in cfg.GROUP_IDS}),
                        encoding="utf-8")
    assert cfg.load_tag_groups(override) == {f"t-{g}": g for g in cfg.GROUP_IDS}
    assert cfg.load_tag_groups(None) == cfg.TAG_GROUPS


def test_a_tag_map_file_may_carry_a_comment_block(tmp_path):
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"_comment": ["hi"], "tag_groups": {"a": "qol"}}),
                    encoding="utf-8")
    assert cfg.load_tag_groups(path) == {"a": "qol"}


def test_an_unreadable_tag_map_is_a_config_error(tmp_path):
    bad = tmp_path / "tags.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.load_tag_groups(bad)
    with pytest.raises(cfg.ConfigError):
        cfg.load_tag_groups(tmp_path / "missing.json")


# --------------------------------------------------------------------------
# Forum -> type. `[r3]`: the bot never writes Exploit.
# --------------------------------------------------------------------------
def test_forum_decides_the_type_and_exploit_is_not_on_the_menu():
    assert cfg.BUGS_ITEM_TYPE == "Defect"
    assert cfg.FEATURES_ITEM_TYPE == "Enhancement"
    assert cfg.BOT_WRITABLE_TYPES == {"Defect", "Enhancement"}
    assert "Exploit" not in cfg.BOT_WRITABLE_TYPES
    # Exploit still exists and is still worth 3 — the admin sets it by hand.
    assert cfg.MERIT_BY_TYPE["Exploit"] == 3
    assert cfg.CREATION_ONLY_FIELDS == {"type"}


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
def test_settings_reads_the_mapping_it_is_given():
    s = cfg.Settings.from_env(FAKE_ENV)
    assert s.discord_bot_user_id == "fake-bot-user"
    assert s.roadmap_base_url == "https://roadmap.example.invalid/"
    assert s.dry_run is True
    assert s.channel_types() == {"fake-channel-bugs": "Defect",
                                 "fake-channel-features": "Enhancement"}


def test_dry_run_is_only_off_for_a_deliberate_zero():
    for value in ("", "1", "0 ", "no", "false", "true"):
        env = dict(FAKE_ENV, NWNBOT_DRY_RUN=value)
        expected = value.strip() != "0"
        assert cfg.Settings.from_env(env).dry_run is expected, value
    assert cfg.Settings.from_env({}).dry_run is True


def test_an_empty_environment_yields_empty_values_not_defaults():
    s = cfg.Settings.from_env({})
    assert s.discord_bot_token == "" and s.discord_bot_user_id == ""
    assert s.bugs_forum_id == "" and s.features_forum_id == ""
    assert s.channel_types() == {}
    assert s.roadmap_base_url == cfg.LOCAL_ROADMAP_BASE_URL


def test_channel_types_only_names_channels_the_environment_supplied():
    assert cfg.channel_types({"DISCORD_BUGS_FORUM_ID": "b"}) == {"b": "Defect"}


# --------------------------------------------------------------------------
# The player identity map — merit money, so nothing is guessed
# --------------------------------------------------------------------------
PARENTHETICALS = [
    ("Sync (Shync)", "Sync", "Shync"),
    ("Balendin (Balendin_2222)", "Balendin", "Balendin_2222"),
    ("dc0960 (Dungeon_Crawler)", "dc0960", "Dungeon_Crawler"),
    ("Piskan (Alek Cain)", "Piskan", "Alek Cain"),
    # A role, not a handle. The parser cannot tell; the human can.
    ("HomelessSon (Server Admin)", "HomelessSon", "Server Admin"),
    # No parenthetical at all — five of the nineteen look like this.
    ("community", "community", None),
    ("McGondy", "McGondy", None),
    ("-Methonash-", "-Methonash-", None),
    ("FLYING HITCHER", "FLYING HITCHER", None),
    ("Tukwut", "Tukwut", None),
    ("Y a z k i r", "Y a z k i r", None),
    ("Agent51950", "Agent51950", None),
    ("  padded  ", "padded", None),
]


@pytest.mark.parametrize("entry,head,alias", PARENTHETICALS)
def test_a_roster_entry_splits_without_assuming_a_shape(entry, head, alias):
    cand = cfg.split_player_entry(entry)
    assert (cand.head, cand.alias) == (head, alias)
    assert cand.roadmap_name == entry.strip()


def test_the_whole_real_roster_parses():
    cands = [cfg.split_player_entry(e) for e in ROSTER]
    assert len(cands) == 19
    with_alias = [c for c in cands if c.has_alias]
    assert len(with_alias) == 8
    assert len(cands) - len(with_alias) == 11
    assert all(c.roadmap_name for c in cands)


def test_seeding_cannot_produce_a_single_discord_id():
    """The honest ceiling, asserted rather than promised in a docstring.

    A Discord user id is a snowflake and appears nowhere in `roadmap.yaml`, so
    seeding produces an EMPTY id map plus candidates for a human to resolve.
    """
    doc = cfg.seed_players_document(ROSTER)
    assert doc["discord_ids"] == {}
    assert len(doc["candidates"]) == 19
    assert {c["roadmap_name"] for c in doc["candidates"]} == set(ROSTER)


def test_a_display_name_never_resolves_an_author():
    """The property that keeps merit off the wrong player's account."""
    doc = cfg.seed_players_document(ROSTER)
    players = cfg.PlayerMap.from_document(doc)
    assert players.ids == {}
    for cand in players.candidates:
        assert players.resolve(cand.roadmap_name) is None
        if cand.alias:
            assert players.resolve(cand.alias) is None
        assert players.resolve(cand.head) is None
    assert players.resolve("123456789012345678") is None


def test_only_an_explicit_id_entry_resolves():
    players = cfg.PlayerMap(ids={"111": "Sync (Shync)"},
                            candidates=(cfg.split_player_entry("Sync (Shync)"),))
    assert players.resolve("111") == "Sync (Shync)"
    assert players.resolve(111) == "Sync (Shync)"     # ids arrive as ints too
    assert players.resolve("222") is None
    assert players.resolve("Shync") is None
    # A name with an id is no longer waiting for one.
    assert players.unresolved_candidates == ()


def test_unresolved_candidates_are_what_the_human_still_owes():
    doc = cfg.seed_players_document(ROSTER)
    doc["discord_ids"] = {"111": "Sync (Shync)", "222": "Tukwut"}
    players = cfg.PlayerMap.from_document(doc)
    names = {c.roadmap_name for c in players.unresolved_candidates}
    assert len(names) == 17
    assert "Sync (Shync)" not in names and "Tukwut" not in names


def test_seeding_writes_only_to_the_path_it_is_given(tmp_path):
    path = tmp_path / "players.json"
    cfg.write_players_seed(path, ROSTER)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["discord_ids"] == {}
    assert "gitignored" in " ".join(doc["_comment"])
    assert list(tmp_path.iterdir()) == [path]


def test_reseeding_never_discards_hand_made_ids(tmp_path):
    path = tmp_path / "players.json"
    cfg.write_players_seed(path, ROSTER)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["discord_ids"] = {"111": "Sync (Shync)"}
    path.write_text(json.dumps(doc), encoding="utf-8")

    cfg.write_players_seed(path, ROSTER + ["Newcomer (new_guy)"])
    again = cfg.PlayerMap.load(path)
    assert again.ids == {"111": "Sync (Shync)"}
    assert len(again.candidates) == 20


def test_a_plain_id_to_name_object_still_loads(tmp_path):
    """The pre-`[b5]` shape `cli._live_context` used to read."""
    path = tmp_path / "players.json"
    path.write_text(json.dumps({"111": "Sync (Shync)"}), encoding="utf-8")
    assert cfg.PlayerMap.load(path).ids == {"111": "Sync (Shync)"}


def test_a_missing_player_map_is_empty_not_an_error(tmp_path):
    players = cfg.PlayerMap.load(tmp_path / "nope.json")
    assert players.ids == {} and players.resolve("111") is None


def test_a_malformed_player_map_is_a_config_error(tmp_path):
    path = tmp_path / "players.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.PlayerMap.load(path)
    path.write_text('{"discord_ids": 3}', encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.PlayerMap.load(path)


# --------------------------------------------------------------------------
# Nothing secret, and nothing that reads .env
# --------------------------------------------------------------------------
def test_config_holds_no_id_shaped_literal():
    """A Discord snowflake is 17-19 digits. None may appear as a value.

    The two forum channel ids and the bot's user id live in `.env` and nowhere
    else. Tag names and player names are not secret and may live in the code,
    so this looks at string and number *literals*, not at prose.
    """
    import ast
    import re

    tree = ast.parse((REPO / "nwnbot" / "config.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Skip docstrings: they are the place that explains the rule.
            if "\n" in node.value:
                continue
            assert not re.fullmatch(r"\d{17,19}", node.value.strip()), node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert node.value < 10 ** 16, node.value


def test_nothing_in_config_reaches_for_the_process_environment():
    """Every value arrives from a mapping the caller passes in.

    That is why no test in this repo can accidentally read the real `.env`,
    which holds a live bot token and the two real channel ids.
    """
    import ast

    tree = ast.parse((REPO / "nwnbot" / "config.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "dotenv" not in imported
    assert "os" not in imported
