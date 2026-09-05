"""Tests for ``nwnbot.store`` — [b6-sync]'s local sqlite state.

The database is always a ``tmp_path``: ``state.db`` is gitignored runtime state
and must never be written into the repo by a test run.
"""

from __future__ import annotations

import pytest

from nwnbot.store import (
    DECISION_DUPE_REMOVED,
    ReviewEntry,
    Store,
    StoreView,
    content_hash,
)
from nwnbot.sync import AppendComment, CreateIdea, ReviewItem


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "state.db") as s:
        yield s


# --- content hash ----------------------------------------------------------

HASH_EQUAL = [
    ("plain", "plain"),
    ("  spaced  ", "spaced"),
    ("two\nlines", "two lines"),
    ("tabs\there", "tabs here"),
    (True, "true"),
    (False, "false"),
    (None, ""),
    (12, "12"),
]


@pytest.mark.parametrize("left,right", HASH_EQUAL)
def test_content_hash_normalises_whitespace_and_scalars(left, right):
    assert content_hash(left) == content_hash(right)


def test_content_hash_distinguishes_real_changes():
    assert content_hash("planned") != content_hash("wip")


# --- links -----------------------------------------------------------------

def test_link_round_trips_both_ways(store):
    store.link_thread("t1", "forge-thing", "chan-bugs")
    assert store.idea_for_thread("t1") == "forge-thing"
    assert store.thread_for_idea("forge-thing") == "t1"
    assert store.links() == {"t1": "forge-thing"}


def test_relinking_a_thread_updates_in_place(store):
    store.link_thread("t1", "one")
    store.link_thread("t1", "two")
    assert store.links() == {"t1": "two"}


def test_link_requires_both_ids(store):
    with pytest.raises(ValueError):
        store.link_thread("", "idea")


def test_unknown_link_is_none(store):
    assert store.idea_for_thread("nope") is None
    assert store.thread_for_idea("nope") is None


# --- hashes ----------------------------------------------------------------

def test_hash_set_get_and_upsert(store):
    store.set_hash("idea", "status", "planned")
    assert store.get_hash("idea", "status") == content_hash("planned")
    store.set_hash("idea", "status", "wip")
    assert store.get_hash("idea", "status") == content_hash("wip")
    assert list(store.hashes()) == [("idea", "status")]


def test_hashes_are_per_field(store):
    store.set_hash("idea", "status", "wip")
    assert store.get_hash("idea", "group") is None


# --- review queue ----------------------------------------------------------

def test_review_is_queued_once(store):
    assert store.queue_review("k", "unknown_author", "123", "who?") is True
    assert store.queue_review("k", "unknown_author", "123", "who?") is False
    (entry,) = store.reviews()
    assert isinstance(entry, ReviewEntry)
    assert (entry.kind, entry.subject, entry.status) == ("unknown_author", "123", "open")


def test_resolved_review_leaves_the_open_list(store):
    store.queue_review("k", "kind")
    store.resolve_review("k")
    assert store.reviews() == []
    assert [e.status for e in store.reviews(None)] == ["resolved"]
    # ...but the planner still sees it, so it is never re-raised.
    assert store.view().has_review("k")


def test_review_needs_a_key(store):
    with pytest.raises(ValueError):
        store.queue_review("", "kind")


# --- admin decisions -------------------------------------------------------

def test_decision_the_bot_must_not_undo(store):
    store.record_decision(DECISION_DUPE_REMOVED, "dupe-idea", "removed by hand")
    assert store.decision(DECISION_DUPE_REMOVED, "dupe-idea") == "removed by hand"
    assert store.view().decision(DECISION_DUPE_REMOVED, "dupe-idea") == "removed by hand"
    assert store.decision(DECISION_DUPE_REMOVED, "other") is None


# --- the planner boundary --------------------------------------------------

def test_view_is_a_full_immutable_snapshot(store):
    store.link_thread("t1", "idea-a", "chan")
    store.set_hash("idea-a", "status", "wip")
    store.queue_review("r1", "thread_renamed")
    store.record_decision(DECISION_DUPE_REMOVED, "idea-a")
    view = store.view()
    assert view.idea_for_thread("t1") == "idea-a"
    assert view.unchanged("idea-a", "status", "wip")
    assert not view.unchanged("idea-a", "status", "soon")
    assert view.seen("idea-a", "status")
    assert not view.seen("idea-a", "group")
    assert view.has_review("r1")
    # The view is a copy: later writes do not leak into it.
    store.set_hash("idea-a", "status", "soon")
    assert view.unchanged("idea-a", "status", "wip")


def test_store_view_evolve_is_pure():
    base = StoreView.empty()
    grown = base.evolve(links={"t": "i"}, hashes={("i", "status"): "x"},
                        reviewed=["r"])
    assert base.links == {} and base.hashes == {} and base.reviewed == frozenset()
    assert grown.idea_for_thread("t") == "i"
    assert grown.has_review("r")


def test_record_persists_what_an_applied_action_leaves_behind(store):
    idea = {"id": "forge-thing", "title": "Forge thing", "group": "forge",
            "status": "planned", "hidden": True, "type": "Defect", "player": "Sync"}
    store.record_all([
        CreateIdea(idea=idea, thread_id="t1", channel_id="chan"),
        AppendComment(idea_id="forge-thing", text="hello", message_id="m1"),
        ReviewItem(kind="thread_renamed", subject="t1", detail="renamed"),
    ])
    assert store.idea_for_thread("t1") == "forge-thing"
    assert store.get_hash("forge-thing", "status") == content_hash("planned")
    assert store.get_hash("forge-thing", "comment:m1") == content_hash("hello")
    assert [e.kind for e in store.reviews()] == ["thread_renamed"]


def test_record_ignores_a_non_action(store):
    store.record(object())  # duck-typed: no effects(), nothing happens
    assert store.links() == {}


def test_db_is_written_only_where_the_test_asked(tmp_path):
    path = tmp_path / "nested" / "state.db"
    path.parent.mkdir()
    with Store(path) as s:
        s.link_thread("t", "i")
    assert path.exists()
