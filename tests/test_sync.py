"""Tests for ``nwnbot.sync`` — [b6-sync]'s two pure planners.

Three properties carry the weight:

1. **Every branch is a table row.** Each Discord->roadmap and roadmap->Discord
   branch, and each review-queue exit, is exercised from a hand-built snapshot.
2. **In sync means empty.** A snapshot with nothing to do plans nothing, in
   both directions.
3. **A plan is a fixed point.** Applying a planner's own output (via the pure
   :func:`nwnbot.sync.simulate`) and re-planning produces an empty plan. That
   is the real no-loop test, not a claim in a docstring.

The tag names and channel ids below are **test placeholders**: the real ones
are unknown (review items ``r2``/``r3``, ``[b5-config]`` blocked), and the
planners take the mapping as an input precisely so nothing has to be invented
in shipped code.
"""

from __future__ import annotations

import pytest

from nwnbot.forum import ForumMessage, ForumSnapshot, ForumThread
from nwnbot.roadmap import ForbiddenWrite, Snapshot
from nwnbot.store import Store, StoreView, content_hash
from nwnbot.sync import (
    CREATION_ONLY_FIELDS,
    DEFAULT_ACTION_CAP,
    NEW_IDEA_STATUS,
    REVIEW_ACTION_CAP,
    REVIEW_BROKEN_LINK,
    REVIEW_DUPE_CYCLE,
    REVIEW_NO_CHANNEL_FOR_TYPE,
    REVIEW_PLAYER_NOT_ON_ROSTER,
    REVIEW_TAG_MAPPING_MISSING,
    REVIEW_THREAD_RENAMED,
    REVIEW_UNKNOWN_AUTHOR,
    REVIEW_UNKNOWN_CHANNEL,
    REVIEW_UNKNOWN_STATUS,
    REVIEW_UNMAPPED_TAG,
    AppendComment,
    ArchiveThread,
    CreateIdea,
    CreateThread,
    Plan,
    PlanContext,
    PostMessage,
    RecordBaseline,
    ReviewItem,
    UpdateIdeaField,
    mint_idea_id,
    plan_discord_to_roadmap,
    plan_roadmap_to_discord,
    resolve_canonical,
    shorten_id,
    simulate,
    slugify_id,
    unique_idea_id,
)

BUGS = "chan-bugs"
FEATURES = "chan-features"
BOT = "bot-user-id"
PLAYER = "Sync (Shync)"

CTX = PlanContext(
    tag_groups={"tag-forge": "forge", "tag-bosses": "bosses"},
    channel_types={BUGS: "Defect", FEATURES: "Enhancement"},
    players={"u-1": PLAYER},
    bot_user_id=BOT,
    editor_url="https://roadmap.invalid/",
    thread_url_template="https://discord.invalid/{thread_id}",
)


def roadmap(*ideas, players=(PLAYER,)) -> Snapshot:
    return Snapshot(ideas=[dict(i) for i in ideas],
                    vocab={"players": list(players)}, base_hashes={}, version="v1")


def idea(idea_id="forge-thing", **kw) -> dict:
    row = {"id": idea_id, "title": "A forge thing", "group": "forge",
           "status": "planned", "type": "Defect", "player": PLAYER}
    row.update(kw)
    return row


def thread(thread_id="t-1", *, channel_id=BUGS, title="A forge thing",
           author_id="u-1", tags=("tag-forge",), starter=None, messages=(),
           archived=False, locked=False) -> ForumThread:
    return ForumThread(id=thread_id, channel_id=channel_id, title=title,
                       author_id=author_id, author_name="Shync",
                       tag_names=tags, starter=starter, messages=tuple(messages),
                       archived=archived, locked=locked)


def forum(*threads, bot_user_id=BOT) -> ForumSnapshot:
    return ForumSnapshot(threads=threads, bot_user_id=bot_user_id)


def msg(message_id="m-1", *, author="u-1", content="it broke", starter=False,
        edited=False) -> ForumMessage:
    return ForumMessage(id=message_id, author_id=author, author_name="Shync",
                        content=content, is_starter=starter, edited=edited)


def kinds(plan: Plan) -> list[str]:
    return [type(a).__name__ for a in plan]


def review_kinds(plan: Plan) -> list[str]:
    return [a.kind for a in plan.reviews]


# ==========================================================================
# Id minting — mirrors roadmap-editor.py:4537 (slugifyId) / :4552 (shortenId)
# ==========================================================================

SLUG_CASES = [
    ("Smith can disenchant negative abilities", "smith-can-disenchant-negative-abilities"),
    ("Théoden's horse", "theodens-horse"),                 # NFD + combining marks
    ("boss's loot", "bosss-loot"),                          # apostrophes deleted
    ("curly ’quote’", "curly-quote"),            # U+2019 too
    ("  leading and trailing  ", "leading-and-trailing"),
    ("Multiple   ---   separators", "multiple-separators"),
    ("+20 weapons!", "20-weapons"),
    ("", ""),
    ("!!!", ""),
    (None, ""),
]


@pytest.mark.parametrize("title,expected", SLUG_CASES)
def test_slugify_matches_the_editors_own_rules(title, expected):
    assert slugify_id(title) == expected


SHORTEN_CASES = [
    ("short-slug", 60, "short-slug"),
    ("a" * 70, 60, "a" * 60),                     # no boundary past halfway: hard cut
    ("word-" * 20, 60, "word-word-word-word-word-word-word-word-word-word-word-word"),
    ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bb", 32, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
]


@pytest.mark.parametrize("slug,limit,expected", SHORTEN_CASES)
def test_shorten_id_cuts_on_a_word_boundary(slug, limit, expected):
    got = shorten_id(slug, limit)
    assert got == expected
    assert len(got) <= limit
    assert not got.endswith("-")


def test_unique_idea_id_is_case_insensitive_like_the_editor():
    assert unique_idea_id("thing", []) == "thing"
    assert unique_idea_id("thing", ["Thing"]) == "thing-2"
    assert unique_idea_id("thing", ["thing", "thing-2"]) == "thing-3"


def test_mint_idea_id_runs_the_whole_editor_chain():
    assert mint_idea_id("A forge thing", ["a-forge-thing"]) == "a-forge-thing-2"


# ==========================================================================
# dupe_of resolution — the close path follows it to the governing item
# ==========================================================================

def test_resolve_canonical_follows_a_chain():
    by_id = {"a": {"id": "a", "dupe_of": "b"}, "b": {"id": "b", "dupe_of": "c"},
             "c": {"id": "c"}}
    assert resolve_canonical(by_id, "a") == ("c", False)
    assert resolve_canonical(by_id, "c") == ("c", False)


def test_resolve_canonical_reports_a_cycle_instead_of_raising():
    by_id = {"a": {"id": "a", "dupe_of": "b"}, "b": {"id": "b", "dupe_of": "a"}}
    assert resolve_canonical(by_id, "a") == (None, True)


def test_resolve_canonical_stops_at_a_dangling_pointer():
    by_id = {"a": {"id": "a", "dupe_of": "ghost"}}
    assert resolve_canonical(by_id, "a") == ("a", False)


# ==========================================================================
# Actions refuse to even *describe* a forbidden write
# ==========================================================================

FORBIDDEN_UPDATES = [
    ("notes", "anything"),
    ("notes_h", "anything"),
    ("impl_notes", "anything"),
    ("merit_awarded", True),
    ("status", "awarded"),
    ("status", "implemented"),
    ("status", "manual"),
]


@pytest.mark.parametrize("field_name,value", FORBIDDEN_UPDATES)
def test_forbidden_field_cannot_be_planned(field_name, value):
    with pytest.raises(ForbiddenWrite):
        UpdateIdeaField(idea_id="x", field_name=field_name, value=value)


def test_allowed_update_is_fine():
    action = UpdateIdeaField(idea_id="x", field_name="group", value="bosses")
    assert action.effects().hashes == {("x", "group"): "bosses"}


FORBIDDEN_NEW_IDEAS = [
    {"id": "x", "hidden": True, "merit_awarded": True},
    {"id": "x", "hidden": True, "notes": "<div>hi</div>"},
    {"id": "x", "hidden": True, "impl_notes": "hi"},
    {"id": "x", "hidden": True, "status": "awarded"},
    {"id": "x"},                                   # not hidden
    {"id": "x", "hidden": False},
]


@pytest.mark.parametrize("row", FORBIDDEN_NEW_IDEAS)
def test_forbidden_new_idea_cannot_be_planned(row):
    with pytest.raises(ForbiddenWrite):
        CreateIdea(idea=row)


def test_empty_comment_and_post_are_refused():
    with pytest.raises(ForbiddenWrite):
        AppendComment(idea_id="x", text="   ")
    with pytest.raises(ForbiddenWrite):
        PostMessage(thread_id="t", idea_id="x", text="")


# ==========================================================================
# Discord -> roadmap
# ==========================================================================

def test_new_thread_creates_a_hidden_idea_and_carries_the_first_post_over():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(starter=msg(starter=True))), None, CTX)
    assert kinds(plan) == ["CreateIdea", "AppendComment"]
    created = plan[0].idea
    assert created["id"] == "a-forge-thing"
    assert created["hidden"] is True
    assert created["status"] == NEW_IDEA_STATUS
    assert created["group"] == "forge"          # from the tag
    assert created["type"] == "Defect"          # from the forum channel
    assert created["player"] == PLAYER          # from the identity map
    assert created["discord"]["thread_id"] == "t-1"
    assert "notes" not in created and "merit_awarded" not in created
    assert plan[1].idea_id == "a-forge-thing"
    assert "it broke" in plan[1].text


def test_new_idea_id_avoids_an_existing_one():
    plan = plan_discord_to_roadmap(roadmap(idea("a-forge-thing")), forum(thread()),
                                   None, CTX)
    assert plan[0].idea["id"] == "a-forge-thing-2"


def test_two_new_threads_do_not_mint_the_same_id():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread("t-1"), thread("t-2")), None, CTX)
    assert [a.idea["id"] for a in plan] == ["a-forge-thing", "a-forge-thing-2"]


def test_feature_forum_implies_enhancement():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(channel_id=FEATURES)), None, CTX)
    assert plan[0].idea["type"] == "Enhancement"


def test_new_replies_become_comments_never_notes():
    existing = idea(discord={"thread_id": "t-1"})
    plan = plan_discord_to_roadmap(
        roadmap(existing),
        forum(thread(messages=[msg("m-1"), msg("m-2", content="me too")])),
        None, CTX)
    assert kinds(plan) == ["AppendComment", "AppendComment"]
    assert all(a.idea_id == "forge-thing" for a in plan)
    assert all(not isinstance(a, UpdateIdeaField) for a in plan)


def test_bot_authored_messages_are_skipped():
    """Loop prevention, layer one."""
    existing = idea(discord={"thread_id": "t-1"})
    plan = plan_discord_to_roadmap(
        roadmap(existing),
        forum(thread(messages=[msg("m-1", author=BOT, content="status: wip")])),
        None, CTX)
    assert plan == []


def test_a_comment_already_synced_is_not_replanned():
    existing = idea(discord={"thread_id": "t-1"})
    first = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(messages=[msg("m-1")])), None, CTX)
    view = StoreView(hashes={("forge-thing", "comment:m-1"): content_hash(first[0].text)})
    again = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(messages=[msg("m-1")])), view, CTX)
    assert again == []


def test_an_edited_message_is_carried_over_again():
    existing = idea(discord={"thread_id": "t-1"})
    view = StoreView(hashes={("forge-thing", "comment:m-1"): content_hash("stale")})
    plan = plan_discord_to_roadmap(
        roadmap(existing),
        forum(thread(messages=[msg("m-1", content="now with detail", edited=True)])),
        view, CTX)
    assert kinds(plan) == ["AppendComment"]


def test_tag_change_updates_the_group():
    existing = idea(discord={"thread_id": "t-1"})
    plan = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(tags=("tag-bosses",))), None, CTX)
    assert kinds(plan) == ["UpdateIdeaField"]
    assert (plan[0].field_name, plan[0].value, plan[0].previous) == \
        ("group", "bosses", "forge")


def test_thread_rename_is_a_review_item_and_never_renames_the_idea():
    existing = idea(discord={"thread_id": "t-1"})
    plan = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(title="A forge thing, revised")), None, CTX)
    assert kinds(plan) == ["ReviewItem"]
    assert plan[0].kind == REVIEW_THREAD_RENAMED
    assert plan.writes == ()


def test_a_review_already_queued_is_not_raised_twice():
    existing = idea(discord={"thread_id": "t-1"})
    first = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(title="Renamed")), None, CTX)
    view = StoreView(reviewed={first[0].review_key})
    again = plan_discord_to_roadmap(
        roadmap(existing), forum(thread(title="Renamed")), view, CTX)
    assert again == []


def test_archived_thread_with_no_idea_is_left_alone():
    plan = plan_discord_to_roadmap(roadmap(), forum(thread(archived=True)), None, CTX)
    assert plan == []


D2R_REVIEW_CASES = [
    pytest.param(thread(author_id="stranger"), CTX, REVIEW_UNKNOWN_AUTHOR,
                 id="unrecognised author is never added to players:"),
    pytest.param(thread(tags=("tag-unknown",)), CTX, REVIEW_UNMAPPED_TAG,
                 id="tag with no group"),
    pytest.param(thread(), PlanContext(channel_types=CTX.channel_types,
                                       players=CTX.players, bot_user_id=BOT),
                 REVIEW_TAG_MAPPING_MISSING, id="no tag mapping supplied at all"),
    pytest.param(thread(channel_id="chan-other"), CTX, REVIEW_UNKNOWN_CHANNEL,
                 id="forum channel with no type"),
    pytest.param(thread(title="!!!"), CTX, "unsluggable_title",
                 id="title that slugifies to nothing"),
]


@pytest.mark.parametrize("forum_thread,ctx,expected", D2R_REVIEW_CASES)
def test_discord_to_roadmap_queues_instead_of_guessing(forum_thread, ctx, expected):
    plan = plan_discord_to_roadmap(roadmap(), forum(forum_thread), None, ctx)
    assert review_kinds(plan) == [expected]
    assert plan.writes == ()          # nothing is written on a judgement call


def test_a_mapped_player_who_is_not_on_the_roster_is_a_review_item():
    ctx = PlanContext(tag_groups=CTX.tag_groups, channel_types=CTX.channel_types,
                      players={"u-1": "Nobody (nobody)"}, bot_user_id=BOT)
    plan = plan_discord_to_roadmap(roadmap(), forum(thread()), None, ctx)
    assert review_kinds(plan) == [REVIEW_PLAYER_NOT_ON_ROSTER]


def test_a_link_to_a_vanished_idea_is_a_review_item():
    view = StoreView(links={"t-1": "gone"})
    plan = plan_discord_to_roadmap(roadmap(), forum(thread()), view, CTX)
    assert review_kinds(plan) == [REVIEW_BROKEN_LINK]


def test_store_link_is_honoured_when_the_idea_has_no_discord_field():
    view = StoreView(links={"t-1": "forge-thing"})
    plan = plan_discord_to_roadmap(
        roadmap(idea()), forum(thread(messages=[msg("m-1")])), view, CTX)
    assert kinds(plan) == ["AppendComment"]


# ==========================================================================
# Roadmap -> Discord
# ==========================================================================

def test_open_item_with_no_thread_gets_one_tagged_from_its_group():
    plan = plan_roadmap_to_discord(roadmap(idea(status="wip")), forum(), None, CTX)
    assert kinds(plan) == ["CreateThread"]
    assert plan[0].channel_id == BUGS               # Defect -> the bugs forum
    assert plan[0].tag_names == ("tag-forge",)
    assert plan[0].title == "A forge thing"


R2D_SKIP_CASES = [
    pytest.param(idea(hidden=True), id="hidden"),
    pytest.param(idea(dupe_of="other"), id="dupe row"),
    pytest.param(idea(status="awarded"), id="awarded"),
    pytest.param(idea(status="unlikely"), id="unlikely"),
    pytest.param(idea(merit_awarded=True), id="merit already paid"),
]


@pytest.mark.parametrize("row", R2D_SKIP_CASES)
def test_backfill_skips_what_it_must(row):
    assert plan_roadmap_to_discord(roadmap(row), forum(), None, CTX) == []


def test_status_change_posts_in_the_thread():
    row = idea(status="wip", discord={"thread_id": "t-1"})
    view = StoreView(hashes={("forge-thing", "status"): content_hash("planned")})
    plan = plan_roadmap_to_discord(roadmap(row), forum(thread()), view, CTX)
    assert kinds(plan) == ["PostMessage"]
    assert plan[0].kind == "status" and "wip" in plan[0].text


def test_the_first_sighting_of_an_adopted_thread_is_quiet():
    """No hash yet => record a baseline, do not announce a status nobody changed."""
    row = idea(status="wip", discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(row), forum(thread()), None, CTX)
    assert kinds(plan) == ["RecordBaseline"]
    assert plan.writes == ()                     # nothing reaches a player
    assert plan[0].effects().hashes == {("forge-thing", "status"): "wip"}


def test_an_unchanged_status_posts_nothing():
    """Loop prevention, layer two."""
    row = idea(status="wip", discord={"thread_id": "t-1"})
    view = StoreView(hashes={("forge-thing", "status"): content_hash("wip")})
    assert plan_roadmap_to_discord(roadmap(row), forum(thread()), view, CTX) == []


def test_merit_awarded_posts_then_archives_and_locks():
    row = idea(status="awarded", merit_awarded=True, discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(row), forum(thread()), None, CTX)
    assert kinds(plan) == ["PostMessage", "ArchiveThread"]
    assert plan[0].kind == "merit"
    assert "1 merit" in plan[0].text                # Defect is worth 1
    assert plan[1].locked is True


def test_merit_message_counts_by_type():
    row = idea(type="Exploit", status="awarded", merit_awarded=True,
               discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(row), forum(thread()), None, CTX)
    assert "3 merit points have" in plan[0].text


def test_unlikely_posts_and_archives_without_locking():
    row = idea(status="unlikely", discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(row), forum(thread()), None, CTX)
    assert kinds(plan) == ["PostMessage", "ArchiveThread"]
    assert plan[1].locked is False


def test_the_close_path_follows_dupe_of_to_the_governing_item():
    canonical = idea("canonical", status="awarded", merit_awarded=True,
                     type="Enhancement")
    dupe = idea("dupe-row", dupe_of="canonical", hidden=True,
                discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(canonical, dupe), forum(thread()),
                                   None, CTX)
    assert kinds(plan) == ["PostMessage", "ArchiveThread"]
    assert plan[0].idea_id == "dupe-row"          # posted in the dupe's own thread
    assert plan[0].value == "canonical"           # but merit came from the canonical
    assert "2 merit" in plan[0].text              # ...and from the canonical's type


def test_the_close_path_follows_a_transitive_dupe_chain():
    canonical = idea("canonical", merit_awarded=True)
    middle = idea("middle", dupe_of="canonical", hidden=True)
    leaf = idea("leaf", dupe_of="middle", hidden=True, discord={"thread_id": "t-1"})
    plan = plan_roadmap_to_discord(roadmap(canonical, middle, leaf), forum(thread()),
                                   None, CTX)
    assert [a.value for a in plan if isinstance(a, PostMessage)] == ["canonical"]


def test_a_dupe_of_cycle_is_a_review_item_not_an_exception():
    one = idea("one", dupe_of="two", discord={"thread_id": "t-1"})
    two = idea("two", dupe_of="one")
    plan = plan_roadmap_to_discord(roadmap(one, two), forum(thread()), None, CTX)
    assert review_kinds(plan) == [REVIEW_DUPE_CYCLE]
    assert plan.writes == ()


def test_nothing_is_posted_into_an_archived_thread():
    row = idea(status="wip", discord={"thread_id": "t-1"})
    view = StoreView(hashes={("forge-thing", "status"): content_hash("planned")})
    assert plan_roadmap_to_discord(roadmap(row), forum(thread(archived=True)),
                                   view, CTX) == []


def test_an_already_archived_and_locked_merit_thread_plans_nothing():
    row = idea(status="awarded", merit_awarded=True, discord={"thread_id": "t-1"})
    view = StoreView(hashes={("forge-thing", "merit_awarded"): content_hash("forge-thing")})
    assert plan_roadmap_to_discord(
        roadmap(row), forum(thread(archived=True, locked=True)), view, CTX) == []


R2D_REVIEW_CASES = [
    pytest.param(idea(status="nonsense"), CTX, REVIEW_UNKNOWN_STATUS,
                 id="status outside the ten"),
    pytest.param(idea(type="Exploit"), CTX, REVIEW_NO_CHANNEL_FOR_TYPE,
                 id="no forum channel for the type"),
    pytest.param(idea(group="meaningwave"), CTX, REVIEW_UNMAPPED_TAG,
                 id="group with no tag"),
    pytest.param(idea(), PlanContext(channel_types=CTX.channel_types,
                                     bot_user_id=BOT),
                 REVIEW_TAG_MAPPING_MISSING, id="no tag mapping supplied at all"),
]


@pytest.mark.parametrize("row,ctx,expected", R2D_REVIEW_CASES)
def test_roadmap_to_discord_queues_instead_of_guessing(row, ctx, expected):
    plan = plan_roadmap_to_discord(roadmap(row), forum(), None, ctx)
    assert review_kinds(plan) == [expected]
    assert plan.writes == ()


def test_a_link_to_a_thread_outside_the_snapshot_says_nothing():
    row = idea(discord={"thread_id": "t-elsewhere"})
    assert plan_roadmap_to_discord(roadmap(row), forum(), None, CTX) == []


# ==========================================================================
# Loop prevention, layer three: the per-run action cap
# ==========================================================================

def test_the_cap_defaults_to_25():
    assert DEFAULT_ACTION_CAP == 25
    assert PlanContext().action_cap == 25


def _many_threads(n: int) -> ForumSnapshot:
    return forum(*[thread(f"t-{i}", title=f"Thing number {i}") for i in range(n)])


def test_a_runaway_batch_aborts_and_reports_instead_of_executing():
    plan = plan_discord_to_roadmap(roadmap(), _many_threads(26), None, CTX)
    assert plan.aborted is True
    assert plan.executable is False
    assert REVIEW_ACTION_CAP in review_kinds(plan)
    assert len(plan.writes) == 26          # readable, but not runnable
    assert "cap of 25" in plan.reason


def test_exactly_the_cap_is_fine():
    plan = plan_discord_to_roadmap(roadmap(), _many_threads(25), None, CTX)
    assert plan.aborted is False and plan.executable is True


def test_review_items_do_not_count_against_the_cap():
    ctx = PlanContext(tag_groups=CTX.tag_groups, channel_types=CTX.channel_types,
                      players={}, bot_user_id=BOT, action_cap=2)
    plan = plan_discord_to_roadmap(roadmap(), _many_threads(30), None, ctx)
    assert plan.aborted is False
    assert plan.writes == ()


def test_the_cap_applies_to_the_other_direction_too():
    rows = [idea(f"thing-{i}", title=f"Thing number {i}", status="wip")
            for i in range(26)]
    plan = plan_roadmap_to_discord(roadmap(*rows), forum(), None, CTX)
    assert plan.aborted is True


# ==========================================================================
# Already in sync => empty; and a plan fed back in is a no-op
# ==========================================================================

def test_a_snapshot_already_in_sync_plans_nothing_in_either_direction():
    row = idea(discord={"thread_id": "t-1"})
    view = StoreView(links={"t-1": "forge-thing"},
                     hashes={("forge-thing", "status"): content_hash("planned")})
    snap, forum_snap = roadmap(row), forum(thread())
    assert plan_discord_to_roadmap(snap, forum_snap, view, CTX) == []
    assert plan_roadmap_to_discord(snap, forum_snap, view, CTX) == []


SEEN_PLANNED = StoreView(links={"t-1": "forge-thing"},
                         hashes={("forge-thing", "status"): content_hash("planned")})

ROUND_TRIP_CASES = [
    pytest.param(roadmap(), forum(thread(starter=msg(starter=True))), None,
                 "discord", id="new thread creates an idea"),
    pytest.param(roadmap(idea(discord={"thread_id": "t-1"})),
                 forum(thread(messages=[msg("m-1"), msg("m-2", content="me too")])),
                 SEEN_PLANNED, "discord", id="replies become comments"),
    pytest.param(roadmap(idea(discord={"thread_id": "t-1"})),
                 forum(thread(tags=("tag-bosses",))), SEEN_PLANNED, "discord",
                 id="tag change"),
    pytest.param(roadmap(idea(discord={"thread_id": "t-1"})),
                 forum(thread(title="Renamed")), SEEN_PLANNED, "discord",
                 id="rename review"),
    pytest.param(roadmap(idea(status="wip")), forum(), None, "roadmap",
                 id="backfill creates a thread"),
    pytest.param(roadmap(idea(status="wip", discord={"thread_id": "t-1"})),
                 forum(thread()), SEEN_PLANNED, "roadmap", id="status post"),
    pytest.param(roadmap(idea(status="wip", discord={"thread_id": "t-1"})),
                 forum(thread()), None, "roadmap", id="first sighting baseline"),
    pytest.param(roadmap(idea(status="awarded", merit_awarded=True,
                              discord={"thread_id": "t-1"})),
                 forum(thread()), None, "roadmap", id="merit close: archive and lock"),
    pytest.param(roadmap(idea(status="unlikely", discord={"thread_id": "t-1"})),
                 forum(thread()), None, "roadmap", id="unlikely: archive, no lock"),
    pytest.param(roadmap(idea(status="nonsense", discord={"thread_id": "t-1"})),
                 forum(thread()), None, "roadmap", id="unknown status review"),
]


@pytest.mark.parametrize("snap,forum_snap,view,direction", ROUND_TRIP_CASES)
def test_feeding_a_planners_own_output_back_in_is_a_no_op(snap, forum_snap, view,
                                                          direction):
    planner = (plan_discord_to_roadmap if direction == "discord"
               else plan_roadmap_to_discord)
    first = planner(snap, forum_snap, view, CTX)
    assert first != []                                  # the case does something
    snap2, forum2, view2 = simulate(first, snap, forum_snap, view, CTX)
    assert planner(snap2, forum2, view2, CTX) == []


@pytest.mark.parametrize("snap,forum_snap,view,direction", ROUND_TRIP_CASES)
def test_the_other_direction_is_also_quiet_after_a_plan_is_applied(
        snap, forum_snap, view, direction):
    """A plan must not hand the *opposite* planner new work to undo."""
    planner = (plan_discord_to_roadmap if direction == "discord"
               else plan_roadmap_to_discord)
    other = (plan_roadmap_to_discord if direction == "discord"
             else plan_discord_to_roadmap)
    first = planner(snap, forum_snap, view, CTX)
    snap2, forum2, view2 = simulate(first, snap, forum_snap, view, CTX)
    back = other(snap2, forum2, view2, CTX)
    assert back.writes == ()


def test_the_round_trip_also_holds_through_a_real_store(tmp_path):
    """The sqlite store and the pure simulation agree about what was recorded."""
    snap = roadmap()
    forum_snap = forum(thread(starter=msg(starter=True)))
    with Store(tmp_path / "state.db") as store:
        first = plan_discord_to_roadmap(snap, forum_snap, store, CTX)
        store.record_all(first)
        snap2, forum2, _ = simulate(first, snap, forum_snap, None, CTX)
        assert plan_discord_to_roadmap(snap2, forum2, store, CTX) == []


def test_simulate_does_not_mutate_its_inputs():
    snap = roadmap()
    forum_snap = forum(thread(starter=msg(starter=True)))
    plan = plan_discord_to_roadmap(snap, forum_snap, None, CTX)
    simulate(plan, snap, forum_snap, None, CTX)
    assert snap.ideas == []
    assert len(forum_snap.threads) == 1


def test_planners_never_touch_the_snapshot_they_were_given():
    row = idea(discord={"thread_id": "t-1"})
    snap = roadmap(row)
    before = [dict(i) for i in snap.ideas]
    plan_discord_to_roadmap(snap, forum(thread(tags=("tag-bosses",))), None, CTX)
    plan_roadmap_to_discord(snap, forum(thread()), None, CTX)
    assert snap.ideas == before


# ==========================================================================
# `[b5-config]` / `[r3]`: `type` is written once, at creation
#
# There is no Exploit forum tag. An exploit is reported in #bugs, created as a
# Defect (1 merit), and the admin promotes it to Exploit (3 merit) by hand in
# the editor. If any code path ever *updated* `type`, the next sync would
# silently demote it back to 1. These tests are the property, not a comment.
# ==========================================================================

def test_type_cannot_be_updated_on_an_existing_idea():
    with pytest.raises(ForbiddenWrite):
        UpdateIdeaField(idea_id="forge-thing", field_name="type", value="Defect")


def test_creation_only_fields_is_exactly_type():
    assert CREATION_ONLY_FIELDS == frozenset({"type"})


#: Every shape the Discord -> roadmap planner can meet with an already-linked
#: idea. In each one the idea has been promoted to Exploit in the editor.
PROMOTED_EXPLOIT_SCENARIOS = [
    ("tag changed", thread(tags=("tag-bosses",))),
    ("new reply", thread(messages=[msg("m-9", content="still happening")])),
    ("starter post", thread(starter=msg("m-0", content="how it broke", starter=True))),
    ("nothing changed", thread()),
    ("archived", thread(archived=True, locked=True)),
]


@pytest.mark.parametrize("label,forum_thread", PROMOTED_EXPLOIT_SCENARIOS,
                         ids=[s[0] for s in PROMOTED_EXPLOIT_SCENARIOS])
def test_a_promoted_exploit_is_never_demoted_by_a_later_sync(label, forum_thread):
    promoted = idea(type="Exploit", discord={"thread_id": "t-1"})
    before, world = roadmap(promoted), forum(forum_thread)
    view = StoreView(links={"t-1": "forge-thing"})
    plan = plan_discord_to_roadmap(before, world, view, CTX)
    updates = [a for a in plan if isinstance(a, UpdateIdeaField)]
    assert all(a.field_name == "group" for a in updates), \
        f"{label}: the only field an existing idea ever gets is `group`"
    # And the promotion really does survive applying the plan.
    after, _, _ = simulate(plan, before, world, view, CTX)
    assert after.by_id["forge-thing"]["type"] == "Exploit"


def test_group_is_the_only_field_either_planner_ever_updates():
    """A property over both planners, not a claim about one branch.

    Anything that starts updating a second field on an existing idea fails
    here, which is the guard [r3] asked for: `type` is the field whose update
    would cost a player two merit.
    """
    promoted = idea(type="Exploit", discord={"thread_id": "t-1"},
                    status="wip", merit_awarded=False)
    unlinked = idea("bosses-thing", group="bosses", type="Enhancement",
                    status="unlikely")
    snapshot = roadmap(promoted, unlinked)
    world = forum(thread(tags=("tag-bosses",),
                         messages=[msg("m-3", content="another report")]))
    view = StoreView(links={"t-1": "forge-thing"})
    fields = set()
    for plan in (plan_discord_to_roadmap(snapshot, world, view, CTX),
                 plan_roadmap_to_discord(snapshot, world, view, CTX)):
        fields |= {a.field_name for a in plan if isinstance(a, UpdateIdeaField)}
    assert fields <= {"group"}, f"unexpected field update(s): {sorted(fields)}"


def test_the_bot_only_ever_creates_defects_and_enhancements():
    """`type` comes from the forum: bugs => Defect, features => Enhancement."""
    for channel, expected in ((BUGS, "Defect"), (FEATURES, "Enhancement")):
        plan = plan_discord_to_roadmap(
            roadmap(), forum(thread(channel_id=channel)), None, CTX)
        created = [a for a in plan if isinstance(a, CreateIdea)]
        assert [a.idea["type"] for a in created] == [expected]
        assert all(a.idea["type"] != "Exploit" for a in created)
