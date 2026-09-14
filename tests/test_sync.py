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
    THREAD_HEADER,
    UNKNOWN_DATE,
    UNKNOWN_PLAYER,
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
        edited=False, attachments=()) -> ForumMessage:
    return ForumMessage(id=message_id, author_id=author, author_name="Shync",
                        content=content, is_starter=starter, edited=edited,
                        attachments=attachments)


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
    ("impl_notes", "anything"),
    ("impl_notes_h", "anything"),
    ("merit_awarded", True),
    ("status", "awarded"),
    ("status", "implemented"),
    ("status", "manual"),
]

#: `notes` came OFF the forbidden list on 2026-09-14: it is the reporter-facing
#: description the admin fills by hand-copying the thread, which is the job this
#: bot exists to take over. The implementation notes stay the admin's alone.
ALLOWED_NOW = [("notes", "<div>from Discord</div>"), ("notes_h", "anything")]


@pytest.mark.parametrize("field_name,value", FORBIDDEN_UPDATES)
def test_forbidden_field_cannot_be_planned(field_name, value):
    with pytest.raises(ForbiddenWrite):
        UpdateIdeaField(idea_id="x", field_name=field_name, value=value)


@pytest.mark.parametrize("field_name,value", ALLOWED_NOW)
def test_notes_is_writable_but_the_implementation_notes_are_not(field_name, value):
    action = UpdateIdeaField(idea_id="x", field_name=field_name, value=value)
    assert action.field_name == field_name


def test_the_developer_notes_stay_off_limits():
    # The distinction the whole change turns on: the description is the
    # reporter's, the implementation notes are the admin's.
    for blocked in ("impl_notes", "impl_notes_h"):
        with pytest.raises(ForbiddenWrite):
            UpdateIdeaField(idea_id="x", field_name=blocked, value="x")


def test_allowed_update_is_fine():
    action = UpdateIdeaField(idea_id="x", field_name="group", value="bosses")
    assert action.effects().hashes == {("x", "group"): "bosses"}


FORBIDDEN_NEW_IDEAS = [
    {"id": "x", "hidden": True, "merit_awarded": True},
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
    assert "merit_awarded" not in created
    # `notes` is the reporter-facing description. With no model summary the
    # report's own words are used, which is the thing being described.
    assert "it broke" in created["notes"]
    assert plan[1].idea_id == "a-forge-thing"
    assert "it broke" in plan[1].text


def test_a_model_summary_becomes_the_description():
    ctx = _replace(CTX, summaries={"t-1": "The forge stopped accepting ore."})
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(starter=msg(starter=True))), None, ctx)
    assert "The forge stopped accepting ore." in plan[0].idea["notes"]
    # The raw report still reaches the internal comment, so nothing is lost to
    # a summary that turns out to have dropped something.
    assert "it broke" in plan[1].text


def test_the_description_is_the_editors_html_not_raw_text():
    ctx = _replace(CTX, summaries={"t-1": "Ore is refused."})
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(starter=msg(starter=True))), None, ctx)
    assert plan[0].idea["notes"].startswith("<div>")


def test_a_summary_for_another_thread_is_not_used():
    ctx = _replace(CTX, summaries={"t-999": "Wrong thread."})
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(starter=msg(starter=True))), None, ctx)
    assert "Wrong thread" not in plan[0].idea["notes"]
    assert "it broke" in plan[0].idea["notes"]


def test_an_image_only_report_still_gets_an_idea_without_notes():
    # content == "" and no summary: there is nothing to describe, and an empty
    # `notes` is better than an empty <div>.
    plan = plan_discord_to_roadmap(
        roadmap(), forum(thread(starter=msg(content="", starter=True))), None, CTX)
    created = [a for a in plan if isinstance(a, CreateIdea)][0].idea
    assert "notes" not in created


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


def test_a_bot_opened_thread_says_where_it_came_from():
    # [r11]: backfill opens one thread per open item, so the opening post has to
    # explain itself. The header leads, so the 4000-char cut can never eat it.
    notes = "<div>Something is wrong with the forge.</div>"
    plan = plan_roadmap_to_discord(roadmap(idea(status="wip", notes=notes)),
                                   forum(), None, CTX)
    # Attribution, not an explanation of the mechanism: a backfill posts this
    # line once per thread to the same people, so it carries the one thing that
    # differs each time.
    assert plan[0].body.startswith(f"Reported by: {PLAYER} on ")
    assert "Something is wrong with the forge." in plan[0].body


def test_the_header_stands_alone_when_the_item_has_no_notes():
    plan = plan_roadmap_to_discord(roadmap(idea(status="wip")), forum(), None, CTX)
    body = plan[0].body
    header = THREAD_HEADER.format(player=PLAYER, date=UNKNOWN_DATE)
    # No `notes` must not leave a blank gap between the header and the link.
    assert body == header + "\n\n" + CTX.idea_url("forge-thing")


def test_the_header_names_the_reporter_and_the_date():
    row = idea(status="wip", player="Tukwut", date="2026-07-16")
    plan = plan_roadmap_to_discord(roadmap(row, players=("Tukwut",)), forum(),
                                   None, CTX)
    assert plan[0].body.startswith("Reported by: Tukwut on 2026-07-16.")


def test_a_missing_date_says_so_rather_than_being_dropped():
    # Roughly one open item in seven has no date; implying one is known would
    # be worse than admitting it is not.
    plan = plan_roadmap_to_discord(roadmap(idea(status="wip")), forum(), None, CTX)
    assert f"on {UNKNOWN_DATE}." in plan[0].body


def test_a_missing_player_says_so_too():
    row = idea(status="wip")
    row.pop("player")
    plan = plan_roadmap_to_discord(roadmap(row), forum(), None, CTX)
    assert f"Reported by: {UNKNOWN_PLAYER} on" in plan[0].body


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


def test_creation_only_fields_is_exactly_type_and_triage():
    # Pinned as an exact set on purpose: a field added here silently stops
    # being updatable, and one removed silently becomes updatable. Both
    # entries are decisions the bot must never make for the admin -- promoting
    # a Defect to an Exploit ([r3]), and approving a report.
    assert CREATION_ONLY_FIELDS == frozenset({"type", "triage"})


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


# --------------------------------------------------------------------------
# [b9-dupes] — duplicate detection. Every outcome is a proposal.
#
# The safety property is asserted first and directly: the bot never writes
# `dupe_of`, in any band, on any path. Everything else is wording and volume.
# --------------------------------------------------------------------------
from dataclasses import replace as _replace  # noqa: E402

from nwnbot.sync import (  # noqa: E402
    REVIEW_DUPE_UNLINKED,
    REVIEW_POSSIBLE_DUPE,
    plan_roadmap_to_discord as _r2d,
)

#: A context with the scorer armed. Thresholds default to 0.0, so every test
#: written before b9 plans exactly what it always did.
DUPE_CTX = _replace(CTX, dupe_low=0.30, dupe_high=0.60, dupe_title_weight=0.6,
                    dupe_post_in_thread=True)

#: The same, with the shipped posting gate: candidates are filed, never posted.
QUIET_CTX = _replace(DUPE_CTX, dupe_post_in_thread=False)

EXISTING = idea("forge-bank-tab-order-resets", title="Bank tab order resets",
                group="forge", notes="<div>my bank tabs revert to the old order</div>")


def dupe_thread(title="Bank tab order resets", body="my bank tabs revert"):
    return thread("t-dupe", title=title, starter=msg("m-1", content=body, starter=True))


def test_below_the_low_threshold_says_nothing_about_duplicates():
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING), forum(dupe_thread(title="Dragon boss dies too fast",
                                             body="the fight is over instantly")),
        None, DUPE_CTX)
    assert kinds(plan) == ["CreateIdea", "AppendComment"]
    assert review_kinds(plan) == []


def test_the_middle_band_files_a_review_entry_and_stays_out_of_discord():
    # "Bank storage order" against "Bank tab order resets" scores 0.50 — over
    # the low threshold, under the high one. A question for the admin only.
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING), forum(dupe_thread(title="Bank storage order", body="")),
        None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_POSSIBLE_DUPE]
    assert "PostMessage" not in kinds(plan)


def test_the_high_band_also_tells_the_reporter_when_posting_is_armed():
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING),
        forum(dupe_thread(title="Bank tab order resets", body="")),
        None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_POSSIBLE_DUPE]
    assert "PostMessage" in kinds(plan)
    post = [a for a in plan if isinstance(a, PostMessage)][0]
    assert post.kind == "dupe_hint"
    assert EXISTING["title"] in post.text
    # It names the candidate, and it is a question, not a verdict.
    assert post.idea_id and post.idea_id != EXISTING["id"]


def test_the_shipped_gate_keeps_even_a_perfect_match_out_of_the_thread():
    """DUPE_POST_IN_THREAD is off: measured recall does not earn a player-visible
    claim. The admin still sees every candidate in the review queue."""
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING),
        forum(dupe_thread(title="Bank tab order resets", body="")),
        None, QUIET_CTX)
    assert review_kinds(plan) == [REVIEW_POSSIBLE_DUPE]
    assert "PostMessage" not in kinds(plan)


def test_a_thread_scored_as_a_duplicate_still_gets_its_own_idea():
    """The whole safety property of [r6]: a false positive never swallows a report."""
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING),
        forum(dupe_thread(title="Bank tab order resets", body="")),
        None, DUPE_CTX)
    created = [a for a in plan if isinstance(a, CreateIdea)]
    assert len(created) == 1
    assert "dupe_of" not in created[0].idea
    assert created[0].idea["player"] == PLAYER, "the reporter keeps their credit"


@pytest.mark.parametrize("ctx", [DUPE_CTX, QUIET_CTX, CTX], ids=["loud", "quiet", "off"])
def test_no_planner_path_ever_writes_dupe_of(ctx):
    """The rule [r6] settled, asserted rather than commented.

    A wrong merge silently steals a player's merit credit, so the bot proposes
    and a human with editor access confirms. Nothing here may write the field.
    """
    snapshot = roadmap(EXISTING, idea("other", title="Bank tab ordering", group="forge"))
    threads = forum(dupe_thread(), thread("t-2", title="Bank tab order resets too"))
    for plan in (plan_discord_to_roadmap(snapshot, threads, None, ctx),
                 _r2d(snapshot, threads, None, ctx)):
        for action in plan:
            if isinstance(action, CreateIdea):
                assert "dupe_of" not in action.idea
            if isinstance(action, UpdateIdeaField):
                assert action.field_name != "dupe_of"


def test_a_resolved_review_entry_is_the_rejection_and_is_never_re_raised():
    key = f"{REVIEW_POSSIBLE_DUPE}:t-dupe:{EXISTING['id']}"
    view = StoreView(reviewed=frozenset({key}))
    plan = plan_discord_to_roadmap(roadmap(EXISTING), forum(dupe_thread()),
                                   view, DUPE_CTX)
    assert review_kinds(plan) == []


def test_a_guard_that_stops_the_idea_also_stops_the_hint():
    """An unknown author files no idea, so nothing may reference one."""
    ctx = _replace(DUPE_CTX, players={})
    plan = plan_discord_to_roadmap(roadmap(EXISTING), forum(dupe_thread()), None, ctx)
    assert review_kinds(plan) == [REVIEW_UNKNOWN_AUTHOR]
    assert kinds(plan) == ["ReviewItem"]


def test_dupe_rows_are_never_offered_as_candidates():
    """A second report is matched to the canonical item, never to another dupe."""
    dupe_row = idea("bank-tab-order-resets-again", title="Bank tab order resets",
                    group="forge", dupe_of=EXISTING["id"])
    plan = plan_discord_to_roadmap(roadmap(EXISTING, dupe_row),
                                   forum(dupe_thread()), None, DUPE_CTX)
    entries = [a for a in plan if isinstance(a, ReviewItem)]
    assert entries and dupe_row["id"] not in entries[0].review_key


# -- the confirmed duplicate: what the bot does after a human sets dupe_of ---
# `hidden`, so the roadmap->Discord planner does not also try to open a thread
# for the canonical item in the same plan and drown the assertions.
CANONICAL = idea("canonical-item", title="Bank tab order resets", group="forge",
                 status="planned", type="Defect", hidden=True)


def confirmed(**kw):
    return idea("dupe-row", title="Bank tabs revert", group="forge", status="planned",
                type="Defect", player=PLAYER, dupe_of="canonical-item",
                discord={"thread_id": "t-1"}, **kw)


def test_a_confirmed_duplicate_tells_the_reporter_and_notes_the_canonical():
    plan = _r2d(roadmap(CANONICAL, confirmed()), forum(thread()), None, CTX)
    assert kinds(plan) == ["PostMessage", "AppendComment"]
    post, comment = plan.actions
    assert post.kind == "dupe_confirmed"
    assert CANONICAL["title"] in post.text
    assert comment.idea_id == "canonical-item", "the demand lands on the canonical"
    assert PLAYER in comment.text and "dupe-row" in comment.text


def test_a_confirmed_duplicate_never_archives_or_locks_the_reporters_thread():
    plan = _r2d(roadmap(CANONICAL, confirmed()), forum(thread()), None, CTX)
    assert not [a for a in plan if isinstance(a, ArchiveThread)]


def test_the_confirmation_is_announced_once():
    world = roadmap(CANONICAL, confirmed())
    first = _r2d(world, forum(thread()), None, CTX)
    view = simulate(first, world, forum(thread()), StoreView.empty(), CTX)[2]
    second = _r2d(world, forum(thread()), view, CTX)
    assert second.writes == (), "the announcement must not repeat"
    assert not [a for a in second if isinstance(a, (PostMessage, AppendComment))]


def test_a_confirmation_is_not_announced_when_the_thread_is_about_to_close():
    """Real news is one message away; "this is a duplicate" first is noise."""
    awarded = idea("canonical-item", title="Bank tab order resets", group="forge",
                   status="planned", type="Defect", hidden=True, merit_awarded=True)
    plan = _r2d(roadmap(awarded, confirmed()), forum(thread()), None, CTX)
    assert kinds(plan) == ["PostMessage", "ArchiveThread"]
    assert plan.actions[0].kind == "merit"


def test_removing_a_dupe_of_that_was_announced_is_a_review_item():
    """The reporter was told something that is no longer true. Ask, do not retract."""
    plain = idea("dupe-row", title="Bank tabs revert", group="forge", status="planned",
                 type="Defect", player=PLAYER, discord={"thread_id": "t-1"})
    view = StoreView(hashes={("dupe-row", "dupe_of"): content_hash("canonical-item")})
    plan = _r2d(roadmap(CANONICAL, plain), forum(thread()), view, CTX)
    assert REVIEW_DUPE_UNLINKED in review_kinds(plan)


# --------------------------------------------------------------------------
# [b8-backfill] — who earns a Discord thread
#
# One policy, shared by `backfill` and the live `serve` loop, because they run
# the same planner. A filter that lived only in the command would let `serve`
# plan a thread for every open item on its first cycle, blow the action cap and
# abort every run -- taking the Discord -> roadmap direction down with it, since
# a cycle aborts whole.
# --------------------------------------------------------------------------
STAFF = "HomelessSon (Server Admin)"
NEAR = frozenset({"implemented", "confirmed", "manual", "design", "wip", "soon"})
STAFF_CTX = _replace(CTX, staff_players=frozenset({STAFF}),
                     staff_thread_statuses=NEAR)

ELIGIBILITY_CASES = [
    pytest.param(PLAYER, "planned", True, id="player, planned -> yes"),
    pytest.param(PLAYER, "later", True, id="player, later -> yes"),
    pytest.param(PLAYER, "wip", True, id="player, wip -> yes"),
    pytest.param(STAFF, "planned", False, id="staff, planned -> no"),
    pytest.param(STAFF, "later", False, id="staff, later -> no"),
    pytest.param(STAFF, "soon", True, id="staff, soon -> yes"),
    pytest.param(STAFF, "wip", True, id="staff, wip -> yes"),
    pytest.param(STAFF, "implemented", True, id="staff, implemented -> yes"),
    pytest.param("", "wip", True, id="no player, near-term -> yes"),
    pytest.param("", "planned", False, id="no player counts as staff"),
]


@pytest.mark.parametrize("player,status,expected", ELIGIBILITY_CASES)
def test_who_earns_a_thread(player, status, expected):
    assert STAFF_CTX.earns_thread({"player": player, "status": status}) is expected


def test_with_no_staff_configured_everything_earns_a_thread():
    """The pre-b8 behaviour, which is what a hand-built PlanContext still gets."""
    for status in ("planned", "later", "wip"):
        assert CTX.earns_thread({"player": STAFF, "status": status}) is True


@pytest.mark.parametrize("player,status,expected", ELIGIBILITY_CASES)
def test_the_policy_reaches_the_planner(player, status, expected):
    """Not just the predicate: the planner really skips an ineligible item."""
    item = idea("thing", title="A thing", group="forge", status=status,
                type="Defect", player=player)
    plan = _r2d(roadmap(item, players=(PLAYER, STAFF)), forum(), None, STAFF_CTX)
    opened = [a for a in plan if isinstance(a, CreateThread)]
    assert bool(opened) is expected


def test_an_ineligible_item_is_silent_not_a_review_item():
    """Most of the backlog is ineligible; that is normal, not a question."""
    item = idea("thing", title="A thing", group="forge", status="planned",
                type="Defect", player=STAFF)
    plan = _r2d(roadmap(item, players=(STAFF,)), forum(), None, STAFF_CTX)
    assert kinds(plan) == []


def test_eligibility_does_not_touch_an_item_that_already_has_a_thread():
    """Promotion opens a thread; demotion must never orphan one.

    An item that already has a thread keeps getting status posts whatever its
    status, because `earns_thread` is only consulted on the create branch.
    """
    item = idea("thing", title="A thing", group="forge", status="planned",
                type="Defect", player=STAFF, discord={"thread_id": "t-1"})
    plan = _r2d(roadmap(item, players=(STAFF,)), forum(thread()), None, STAFF_CTX)
    # Quiet adoption ([r11].3): the first sighting records a baseline rather than
    # posting retroactively. What matters here is that the item was *considered*
    # at all -- an ineligible item with no thread plans nothing whatsoever.
    assert kinds(plan) == ["RecordBaseline"]


def test_the_staff_status_set_is_the_soon_and_beyond_prefix():
    """`soon or beyond`, derived from the ordered STATUSES tuple.

    Asserted rather than commented so inserting a status upstream cannot
    silently widen or narrow who gets told about what.
    """
    import nwnbot.config as cfg
    from nwnbot.sync import STATUSES as ORDER, TERMINAL_STATUSES as TERMINAL
    prefix = set(ORDER[:ORDER.index("soon") + 1]) - TERMINAL
    assert prefix == cfg.STAFF_THREAD_STATUSES

# ==========================================================================
# `only` — narrowing a run to one item, so a first live run can be one wide
# ==========================================================================

def _only_ctx(*ids, cap=25):
    return PlanContext(tag_groups=CTX.tag_groups, channel_types=CTX.channel_types,
                       players=CTX.players, bot_user_id=BOT, action_cap=cap,
                       only=frozenset(ids))


def test_only_narrows_the_plan_to_the_named_idea():
    rows = [idea(f"thing-{i}", title=f"Thing number {i}", status="wip")
            for i in range(5)]
    plan = plan_roadmap_to_discord(roadmap(*rows), forum(), None,
                                   _only_ctx("thing-3"))
    assert [a.idea_id for a in plan.writes] == ["thing-3"]


def test_only_makes_an_over_cap_plan_executable():
    # 26 items is over the cap of 25 and aborts; narrowing to one must leave a
    # plan that actually runs, which is the whole point of the flag.
    rows = [idea(f"thing-{i}", title=f"Thing number {i}", status="wip")
            for i in range(26)]
    snap = roadmap(*rows)
    assert plan_roadmap_to_discord(snap, forum(), None, CTX).aborted is True
    narrowed = plan_roadmap_to_discord(snap, forum(), None, _only_ctx("thing-7"))
    assert narrowed.aborted is False and narrowed.executable is True
    assert len(narrowed.writes) == 1


def test_only_applies_to_the_discord_to_roadmap_direction_too():
    plan = plan_discord_to_roadmap(roadmap(), _many_threads(5), None,
                                   _only_ctx("nothing-matches-this"))
    assert plan.writes == ()


def test_an_empty_only_is_no_restriction():
    rows = [idea(f"thing-{i}", title=f"Thing number {i}", status="wip")
            for i in range(3)]
    plan = plan_roadmap_to_discord(roadmap(*rows), forum(), None, _only_ctx())
    assert len(plan.writes) == 3


# --------------------------------------------------------------------------
# The awarded-exclusion: an idea is not reopened once it has shipped.
#
# A report matching delivered work is a NEW story with its own merit, and the
# resemblance is reported as an echo — most likely a regression in that work,
# or a follow-up to it. It is never a merge proposal, and the player is never
# told "we already did that".
# --------------------------------------------------------------------------
from nwnbot.sync import REVIEW_DUPE_ECHO  # noqa: E402

SHIPPED = dict(EXISTING, id="forge-bank-tab-order-resets",
               status="awarded", merit_awarded=True)


def test_a_shipped_idea_is_never_offered_as_a_duplicate():
    plan = plan_discord_to_roadmap(
        roadmap(SHIPPED), forum(dupe_thread()), None, DUPE_CTX)
    assert REVIEW_POSSIBLE_DUPE not in review_kinds(plan)
    assert "CreateIdea" in kinds(plan)          # the report is still filed


def test_a_shipped_near_match_is_reported_as_an_echo():
    plan = plan_discord_to_roadmap(
        roadmap(SHIPPED), forum(dupe_thread()), None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_DUPE_ECHO]
    detail = [a for a in plan if getattr(a, "kind", "") == REVIEW_DUPE_ECHO][0].detail
    assert "already shipped" in detail and "NOT a duplicate" in detail


def test_an_echo_never_speaks_to_the_player():
    # Even with the in-thread gate ON and a score above the high band: telling
    # someone who just hit a bug that it was already fixed is the wrong answer.
    loud = _replace(DUPE_CTX, dupe_post_in_thread=True)
    plan = plan_discord_to_roadmap(
        roadmap(SHIPPED), forum(dupe_thread()), None, loud)
    assert "PostMessage" not in kinds(plan)


def test_merit_awarded_alone_is_enough_to_exclude():
    # Status can bounce; the merit receipt cannot. The boolean wins on its own.
    row = dict(EXISTING, status="wip", merit_awarded=True)
    plan = plan_discord_to_roadmap(roadmap(row), forum(dupe_thread()), None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_DUPE_ECHO]


def test_unlikely_is_terminal_but_still_a_merge_target():
    # `unlikely` means "we are not doing this", not "we did this" — nothing was
    # delivered, so a second report of it is a genuine duplicate.
    row = dict(EXISTING, status="unlikely")
    plan = plan_discord_to_roadmap(roadmap(row), forum(dupe_thread()), None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_POSSIBLE_DUPE]


def test_an_open_idea_is_unaffected_by_the_rule():
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING), forum(dupe_thread()), None, DUPE_CTX)
    assert review_kinds(plan) == [REVIEW_POSSIBLE_DUPE]


# --------------------------------------------------------------------------
# Attachments reaching the roadmap. Only ever the rehosted url: a signed
# Discord link reviews fine today and 404s tomorrow, which is the failure the
# whole rehosting path exists to prevent.
# --------------------------------------------------------------------------
from nwnbot.forum import Attachment  # noqa: E402

SIGNED = "https://cdn.discordapp.com/attachments/1/2/x.png?ex=deadbeef&hm=abc"


def _img(**kw):
    return Attachment(id=kw.pop("id", "a-1"), content_type="image/png", **kw)


def _thread_with(*atts, content="see screenshot"):
    return thread("t-att", starter=msg("m-1", content=content, starter=True,
                                       attachments=atts))


def test_a_rehosted_image_reaches_the_comment():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(_thread_with(_img(filename="a.png",
                                           rehosted_url="https://img/a.webp"))),
        None, CTX)
    body = [a for a in plan if a.__class__.__name__ == "AppendComment"][0].text
    assert "https://img/a.webp" in body


def test_the_signed_discord_url_is_never_written():
    # The single most important assertion in this file: storing `url` is what
    # produced seven dead images in roadmap.yaml before the bot existed.
    plan = plan_discord_to_roadmap(
        roadmap(), forum(_thread_with(_img(filename="a.png", url=SIGNED))),
        None, CTX)
    body = [a for a in plan if a.__class__.__name__ == "AppendComment"][0].text
    assert SIGNED not in body
    assert "cdn.discordapp.com" not in body


def test_an_un_rehosted_image_is_named_not_silently_dropped():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(_thread_with(_img(filename="shot.png", url=SIGNED))),
        None, CTX)
    body = [a for a in plan if a.__class__.__name__ == "AppendComment"][0].text
    assert "shot.png" in body and "not rehosted" in body


def test_non_images_are_ignored():
    plan = plan_discord_to_roadmap(
        roadmap(), forum(_thread_with(Attachment(id="z", filename="save.zip",
                                                 content_type="application/zip"))),
        None, CTX)
    body = [a for a in plan if a.__class__.__name__ == "AppendComment"][0].text
    assert "save.zip" not in body and "Images:" not in body


def test_a_message_with_no_attachments_is_unchanged():
    plan = plan_discord_to_roadmap(roadmap(), forum(_thread_with()), None, CTX)
    body = [a for a in plan if a.__class__.__name__ == "AppendComment"][0].text
    assert "Images:" not in body


def test_an_image_only_message_still_produces_a_comment():
    # content == "" is exactly what Discord sends for a screenshot-only post,
    # and what the old code dropped on the floor.
    plan = plan_discord_to_roadmap(
        roadmap(), forum(_thread_with(_img(filename="a.png",
                                           rehosted_url="https://img/a.webp"),
                                      content="")),
        None, CTX)
    comments = [a for a in plan if a.__class__.__name__ == "AppendComment"]
    assert comments and "https://img/a.webp" in comments[0].text


# ==========================================================================
# Approval: the reporter hears, once, that their report is on the roadmap.
# ==========================================================================
from nwnbot.sync import APPROVED_MESSAGE  # noqa: E402

LINKED = StoreView(links={"t-1": "forge-thing"})


def _view(**hashes):
    return StoreView(links={"t-1": "forge-thing"},
                     hashes={("forge-thing", k): content_hash(v)
                             for k, v in hashes.items()})


def _plan(row, view):
    return plan_roadmap_to_discord(roadmap(row), forum(thread()), view, CTX)


def posts(plan):
    return [a for a in plan if isinstance(a, PostMessage)]


def baselines(plan):
    return [a for a in plan if isinstance(a, RecordBaseline)]


def test_approving_a_pending_idea_posts_once():
    # Seen as pending, now approved: that is the news.
    plan = _plan(idea(discord={"thread_id": "t-1"}),
                 _view(triage=True, status="planned"))
    assert len(posts(plan)) == 1
    assert "Added to the roadmap" in posts(plan)[0].text
    assert "A forge thing" in posts(plan)[0].text


def test_the_same_approval_is_not_announced_twice():
    row = idea(discord={"thread_id": "t-1"})
    view = _view(triage=True, status="planned")
    first = _plan(row, view)
    snap2, forum2, view2 = simulate(first, roadmap(row), forum(thread()), view, CTX)
    assert plan_roadmap_to_discord(snap2, forum2, view2, CTX) == []


def test_a_still_pending_idea_says_nothing():
    plan = _plan(idea(triage=True, discord={"thread_id": "t-1"}),
                 _view(triage=True, status="planned"))
    assert posts(plan) == []


def test_the_first_sighting_of_a_pending_idea_is_baselined_not_announced():
    plan = _plan(idea(triage=True, discord={"thread_id": "t-1"}), LINKED)
    assert posts(plan) == []
    assert [b.field_name for b in baselines(plan)] == ["triage"]


def test_an_idea_that_was_never_pending_is_left_entirely_alone():
    # THE trap: ~420 existing ideas have no `triage` and no stored hash. Read
    # naively, every one of them looks freshly approved. Nothing may be posted,
    # and no baseline row may be written for them either.
    plan = _plan(idea(discord={"thread_id": "t-1"}), LINKED)
    assert posts(plan) == []
    assert [b for b in baselines(plan) if b.field_name == "triage"] == []


def test_returning_an_idea_to_the_queue_is_silent():
    # The admin un-approving something is their business, not the reporter's.
    plan = _plan(idea(triage=True, discord={"thread_id": "t-1"}),
                 _view(triage=False, status="planned"))
    assert posts(plan) == []
    assert [b.field_name for b in baselines(plan)] == ["triage"]


def test_nothing_is_posted_into_an_archived_thread_on_approval():
    plan = plan_roadmap_to_discord(
        roadmap(idea(discord={"thread_id": "t-1"})),
        forum(thread(archived=True)), _view(triage=True, status="planned"), CTX)
    assert posts(plan) == []


def test_approval_beats_the_status_branch_on_the_same_cycle():
    # Approval usually looks like planned -> planned, so a status-only check
    # would miss it; and when both change, the approval is the bigger news.
    plan = _plan(idea(status="wip", discord={"thread_id": "t-1"}),
                 _view(triage=True, status="planned"))
    assert len(posts(plan)) == 1
    assert "Added to the roadmap" in posts(plan)[0].text


def test_the_duplicate_message_does_not_promise_merit_to_the_reporter():
    # A dupe row is never marked merit_awarded, so its player is never paid --
    # _merit_write pays idea["player"], once per row. The message must not
    # imply otherwise, and must point at the way they CAN earn merit.
    from nwnbot.sync import DUPE_CONFIRMED_MESSAGE
    text = DUPE_CONFIRMED_MESSAGE.format(title="X", link="")
    assert "still counts towards merit" not in text
    assert "credited as a requester" in text
    assert "helping test it" in text


# ==========================================================================
# dupe_candidates: what the approval tab reads. Advisory, never a merge.
# ==========================================================================

def _cands(plan):
    created = [a for a in plan if isinstance(a, CreateIdea)]
    return created[0].idea.get("dupe_candidates", []) if created else []


def test_a_new_idea_carries_its_ranked_candidates():
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING), forum(dupe_thread(title="Bank storage order", body="")),
        None, DUPE_CTX)
    rows = _cands(plan)
    assert [r["id"] for r in rows] == ["forge-bank-tab-order-resets"]
    assert rows[0]["kind"] == "candidate" and 0 < rows[0]["score"] <= 1


def test_a_shipped_match_is_recorded_as_an_echo_not_a_candidate():
    plan = plan_discord_to_roadmap(
        roadmap(SHIPPED), forum(dupe_thread()), None, DUPE_CTX)
    rows = _cands(plan)
    assert [r["kind"] for r in rows] == ["echo"]


def test_candidates_are_ranked_best_first():
    near = idea("forge-bank-tab-order-resets", title="Bank tab order resets",
                group="forge")
    far = idea("forge-something-else", title="Bank tab ordering", group="forge")
    plan = plan_discord_to_roadmap(
        roadmap(near, far), forum(dupe_thread()), None, DUPE_CTX)
    rows = _cands(plan)
    assert len(rows) >= 2
    assert rows == sorted(rows, key=lambda r: -r["score"])


def test_no_candidates_means_no_field_at_all():
    # An idea with nothing to compare against must not carry an empty list:
    # `pruneEmpty` in the editor would drop it anyway, and an absent field
    # reads correctly as "nothing was suggested".
    plan = plan_discord_to_roadmap(
        roadmap(), forum(dupe_thread(title="Totally unrelated thing", body="")),
        None, DUPE_CTX)
    created = [a for a in plan if isinstance(a, CreateIdea)]
    assert "dupe_candidates" not in created[0].idea


def test_candidates_never_become_a_dupe_of():
    # The safety property, restated where it is easiest to break.
    plan = plan_discord_to_roadmap(
        roadmap(EXISTING), forum(dupe_thread()), None, DUPE_CTX)
    created = [a for a in plan if isinstance(a, CreateIdea)]
    assert "dupe_of" not in created[0].idea
