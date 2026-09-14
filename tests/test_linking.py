"""Tests for thread-to-idea matching. Every match is a proposal, never a link."""

from nwnbot import linking
from nwnbot.linking import Proposal, ThreadRef


def idea(idea_id, title, **kw):
    row = {"id": idea_id, "title": title, "group": "forge", "status": "planned"}
    row.update(kw)
    return row


def thread(tid="t-1", title="Bank tab order resets", **kw):
    return ThreadRef(id=tid, channel_id="c-1", title=title, **kw)


IDEAS = [
    idea("bank-tab-order-resets", "Bank tab order resets"),
    idea("dragon-too-fast", "Dragon boss dies too fast"),
    idea("shipped-one", "Forge disenchant fix", status="awarded",
         merit_awarded=True),
]


class FakeLlm:
    """Answers `same` for the ids it was told about."""

    def __init__(self, same_titles=(), answer_none=False):
        self.same_titles = set(same_titles)
        self.answer_none = answer_none
        self.calls = []

    def judge_link(self, t_title, t_body, i_title, i_body=""):
        self.calls.append((t_title, i_title))
        if self.answer_none:
            return None
        from nwnbot.llm import Verdict
        same = i_title in self.same_titles
        return Verdict(same=same, why="because", model="fake-model")


# --------------------------------------------------------------------------
# shortlist
# --------------------------------------------------------------------------

def test_the_shortlist_finds_the_obvious_match():
    from nwnbot import dupes
    got = linking.shortlist(thread(), dupes.prepare(IDEAS))
    assert got[0].idea_id == "bank-tab-order-resets"


def test_a_shipped_idea_is_still_shortlisted():
    # Unlike duplicate detection, which refuses to reopen delivered work: here
    # a shipped idea is exactly what a check-marked thread is tracked by.
    from nwnbot import dupes
    got = linking.shortlist(thread(title="Forge disenchant fix"),
                            dupes.prepare(IDEAS))
    assert "shipped-one" in [c.idea_id for c in got]


def test_an_unrelated_thread_shortlists_nothing():
    from nwnbot import dupes
    assert linking.shortlist(thread(title="zzz qqq vvv"),
                             dupes.prepare(IDEAS)) == []


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------

def test_a_thread_already_linked_is_skipped():
    ideas = IDEAS + [idea("linked", "Linked already",
                          discord={"thread_id": "t-1"})]
    assert linking.propose([thread(tid="t-1")], ideas) == []


def test_an_unlinked_thread_is_proposed():
    out = linking.propose([thread()], IDEAS)
    assert len(out) == 1
    assert out[0].best["id"] == "bank-tab-order-resets"


def test_nothing_is_ever_linked_by_proposing():
    # The safety property: propose() returns data and mutates no idea.
    ideas = [dict(i) for i in IDEAS]
    linking.propose([thread()], ideas)
    assert all("discord" not in i for i in ideas)


def test_the_model_verdict_outranks_the_token_score():
    # The whole reason the model is asked: the token scorer ranks the
    # near-identical title first, and the model says the OTHER one is the item
    # actually tracking this report. Both must be on the shortlist for the
    # model to have a say -- the scorer gates what it ever sees.
    ideas = [
        idea("bank-tab-order-resets", "Bank tab order resets"),
        idea("bank-tab-ordering", "Bank tab ordering is wrong after relog"),
    ]
    llm = FakeLlm(same_titles={"Bank tab ordering is wrong after relog"})
    out = linking.propose([thread()], ideas, llm)
    assert len(out[0].candidates) == 2, "both must reach the model"
    assert out[0].best["id"] == "bank-tab-ordering"
    assert out[0].best["llm"] == "same"
    # and the one it rejected is kept, ranked below, not discarded
    assert out[0].candidates[1]["llm"] == "different"


def test_the_scorer_gates_what_the_model_is_asked_about():
    # Cost control: the model is ~2s a call and there are 423 ideas. It is
    # asked about a handful, never the corpus.
    llm = FakeLlm()
    linking.propose([thread()], IDEAS, llm)
    assert 0 < len(llm.calls) <= linking.SHORTLIST


def test_the_model_id_is_recorded_on_every_row_it_judged():
    out = linking.propose([thread()], IDEAS, FakeLlm())
    assert all(c["by"] == "fake-model" for c in out[0].candidates)


def test_without_a_model_the_rows_say_which_scorer_ran():
    out = linking.propose([thread()], IDEAS, None, scorer_id="stdlib-token-v1")
    assert all(c["by"] == "stdlib-token-v1" for c in out[0].candidates)
    assert all("llm" not in c for c in out[0].candidates)


def test_a_model_that_cannot_answer_leaves_the_token_score_standing():
    # A dead model must not lose the proposal entirely.
    out = linking.propose([thread()], IDEAS, FakeLlm(answer_none=True))
    assert out[0].best["id"] == "bank-tab-order-resets"
    assert "llm" not in out[0].best


def test_the_shipped_flag_is_carried_so_the_ui_can_show_it():
    out = linking.propose([thread(title="Forge disenchant fix")], IDEAS)
    assert out[0].best["shipped"] is True


# --------------------------------------------------------------------------
# The admin's reaction convention
# --------------------------------------------------------------------------

def test_a_salute_marks_a_thread_as_already_in_the_roadmap():
    t = thread(reactions=(linking.SALUTE,))
    assert t.marked_created is True and t.marked is True


def test_a_check_marks_it_as_shipped():
    t = thread(reactions=(linking.CHECK,))
    assert t.marked_shipped is True and t.marked is True


def test_an_unrelated_reaction_marks_nothing():
    # Only 7 of 37 threads carry the convention and several carry other emoji,
    # so an unrelated reaction must not be read as a signal.
    assert thread(reactions=("👍", "🤯")).marked is False


def test_a_marked_thread_sorts_ahead_of_an_unmarked_one():
    out = linking.propose(
        [thread(tid="plain"), thread(tid="marked", reactions=(linking.CHECK,))],
        IDEAS)
    assert [p.thread.id for p in out] == ["marked", "plain"]


# --------------------------------------------------------------------------
# summarise / serialisation
# --------------------------------------------------------------------------

def test_the_summary_counts_what_the_admin_needs_to_see():
    out = linking.propose(
        [thread(tid="a", reactions=(linking.CHECK,)), thread(tid="b", title="zzz qqq")],
        IDEAS, FakeLlm(same_titles={"Bank tab order resets"}))
    s = linking.summarise(out)
    assert s["threads"] == 2 and s["model_agrees"] == 1
    assert s["marked_by_admin"] == 1 and s["no_candidates"] == 1


def test_a_proposal_serialises_everything_the_editor_needs():
    out = linking.propose([thread(url="https://discord/x",
                                  reactions=(linking.SALUTE,))], IDEAS)
    d = out[0].as_dict()
    assert d["thread_id"] == "t-1" and d["url"] == "https://discord/x"
    assert d["marked_created"] is True and d["marked_shipped"] is False
    assert d["candidates"][0]["id"] == "bank-tab-order-resets"
