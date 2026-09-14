"""Tests for ``nwnbot.dupes`` — ``[b9-dupes]``.

Three properties carry the weight:

1. **The scorer is pure and symmetric.** ``score(a, b) == score(b, a)``, and the
   same inputs give the same answer every time. That is what lets it be called
   from inside a planner without breaking the purity contract.
2. **Group and tag words are stripped from both sides.** Without it every idea
   in ``forge`` scores against every other one for sharing the word "forge".
3. **The measured limits are written down as tests, not as hopes.** The last
   block pins what this scorer can and cannot do on the real corpus, so a future
   change that claims to improve it has a number to beat.
"""

from __future__ import annotations

import pytest

from nwnbot import dupes


# --------------------------------------------------------------------------
# normalize / tokens
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    pytest.param("Forge & Crafting!!", "forge crafting", id="punctuation"),
    pytest.param("  Bank   TAB  order ", "bank tab order", id="whitespace"),
    pytest.param("Prestige quest: Pale Master (L11+)",
                 "prestige quest pale master l11", id="title with level"),
    pytest.param(None, "", id="none"),
    pytest.param("", "", id="empty"),
    pytest.param("—–…", "", id="all punctuation"),
])
def test_normalize(raw, expected):
    assert dupes.normalize(raw) == expected


def test_tokens_drops_stopwords_and_single_characters():
    got = dupes.tokens("The bank a tab I resets", drop=dupes.STOPWORDS)
    assert got == frozenset({"bank", "tab", "resets"})


def test_group_words_reads_both_a_group_id_and_a_tag_name():
    assert dupes.group_words("combat-classes") == frozenset({"combat", "classes"})
    assert dupes.group_words(["Forge & Crafting"]) == frozenset({"forge", "crafting"})


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------
def test_score_is_symmetric_and_deterministic():
    a = ("Bank tab order resets", "my tabs keep going back to the old order")
    b = ("Storage tabs revert", "the order of my storage tabs resets on relog")
    first = dupes.score(*a, *b)
    assert first == dupes.score(*b, *a)          # symmetric
    assert first == dupes.score(*a, *b)          # no hidden state


def test_identical_text_scores_one_and_unrelated_text_scores_low():
    assert dupes.score("Bank tab order resets", "x",
                       "Bank tab order resets", "x") == pytest.approx(1.0)
    assert dupes.score("Bank tab order resets", "my tabs reorder themselves",
                       "Dragon boss dies too fast", "the fight is over instantly") < 0.2


def test_the_group_word_is_stripped_from_both_sides():
    """Two forge items sharing only the word "forge" must not look alike."""
    args = ("Forge upgrade costs too much", "the fourth forge slot is expensive",
            "Forge UI hides the preview", "the forge preview pane is cut off")
    with_group = dupes.score(*args, drop=dupes.STOPWORDS | dupes.group_words("forge"))
    without = dupes.score(*args, drop=dupes.STOPWORDS)
    assert with_group < without, "stripping the group word must lower the score"


def test_title_weight_bounds_are_enforced():
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError):
            dupes.score("a", "", "b", "", title_weight=bad)


@pytest.mark.parametrize("weight", [0.0, 1.0])
def test_the_two_halves_can_each_be_isolated(weight):
    """title_weight 0 is pure token overlap; 1 is pure title similarity."""
    value = dupes.score("Bank tabs reset", "the order goes back",
                        "Tab order resets", "ordering reverts",
                        title_weight=weight)
    assert 0.0 <= value <= 1.0


# --------------------------------------------------------------------------
# prepare / rank
# --------------------------------------------------------------------------
def idea(idea_id, title, **kw):
    return {"id": idea_id, "title": title, **kw}


def test_prepare_skips_dupe_rows_and_idless_entries():
    prepared = dupes.prepare([
        idea("a", "Alpha"),
        idea("b", "Beta", dupe_of="a"),
        {"title": "no id"},
        "not a mapping",
    ])
    assert [p.idea_id for p in prepared] == ["a"]


def test_prepare_flattens_html_notes_and_ignores_impl_notes():
    prepared = dupes.prepare([idea(
        "a", "Alpha",
        notes="<div>the <b>bank</b> tab resets</div>",
        impl_notes="<div>rewrote the persistence layer in nwscript</div>")])
    assert "bank" in prepared[0].body
    assert "<" not in prepared[0].body, "HTML must not survive into the token set"
    assert "nwscript" not in prepared[0].body, "impl_notes must not be scored"


def test_prepare_truncates_long_notes():
    prepared = dupes.prepare([idea("a", "Alpha", notes="word " * 500)], notes_max=50)
    assert len(prepared[0].body) <= 50


def test_rank_is_sorted_and_ties_break_on_id_so_a_replay_plans_the_same_thing():
    cands = dupes.prepare([idea("zeta", "Same title"), idea("alpha", "Same title")])
    ranked = dupes.rank("Same title", "", cands)
    assert [c.idea_id for c in ranked] == ["alpha", "zeta"]
    assert ranked[0].value >= ranked[1].value


def test_rank_honours_exclude_and_limit():
    cands = dupes.prepare([idea(f"i{n}", f"Thing {n}") for n in range(5)])
    assert len(dupes.rank("Thing 1", "", cands, limit=2)) == 2
    ranked = dupes.rank("Thing 1", "", cands, limit=0, exclude=["i1"])
    assert "i1" not in [c.idea_id for c in ranked]


def test_rank_over_no_candidates_is_empty_not_an_error():
    assert dupes.rank("anything", "", ()) == ()


# --------------------------------------------------------------------------
# What this scorer actually does — the measured ceiling
#
# These are not aspirations. They are what `dupes --calibrate` reported against
# the real roadmap, reproduced here on the same shapes so that a future change
# claiming to do better has a number to beat. See cfg.DUPE_POST_IN_THREAD.
# --------------------------------------------------------------------------
def test_a_reworded_restatement_is_found():
    """The case lexical matching is good at, and the reason it ships at all."""
    value = dupes.score(
        "Be able to de-level a character when not exploitable", "",
        "A 1-40 re-level / de-level option (when not exploitable)", "",
        title_weight=0.3)
    assert value >= 0.50, "a restatement must clear the low threshold"


def test_a_paraphrase_is_missed_and_that_is_a_known_limit():
    """The case it is blind to — and on this corpus, the common one.

    This pair is a real confirmed duplicate from roadmap.yaml. It scores far
    below any usable threshold, and no weighting fixes it: the two reports share
    almost no words. Recovering it needs semantic matching, which is deliberately
    out of scope (see future-llm-dupe-matching.md).

    Asserted as a *limit*, so that if a future change does find it, this test
    fails and someone updates the story rather than the number quietly moving.
    """
    value = dupes.score(
        "Rest-menu teleport back to where you last used the Well-of-Eru port", "",
        "Expand rest-menu teleports (earned via quests or by killing each "
        "area's boss)", "", title_weight=0.3)
    assert value < 0.50, "if this now passes, the scorer changed — update the docs"


def test_template_titled_siblings_are_the_scorers_worst_false_positive():
    """Two deliberately distinct items that share a title template.

    They outscore every real duplicate in the corpus. This is why
    DUPE_TITLE_WEIGHT is 0.3 and not [r6]'s implied higher weighting.
    """
    args = ("Prestige quest: Pale Master (L11+)", "",
            "Prestige quest: Weapon Master (L13+)", "")
    # 0.67 on titles alone; 0.83 in the live corpus, where the notes are
    # templated too. Either way it outranks every real duplicate.
    assert dupes.score(*args, title_weight=0.6) > 0.6, "the problem, at tw=0.6"
    assert dupes.score(*args, title_weight=0.3) < dupes.score(*args, title_weight=0.6)


# --------------------------------------------------------------------------
# `shipped`: scoreable, but never a merge target. See SHIPPED_STATUSES.
# --------------------------------------------------------------------------

def test_prepare_marks_shipped_ideas_rather_than_dropping_them():
    prepared = dupes.prepare([
        idea("a", "Alpha", status="awarded"),
        idea("b", "Beta", status="planned"),
    ])
    assert {p.idea_id: p.shipped for p in prepared} == {"a": True, "b": False}


def test_merit_awarded_marks_shipped_whatever_the_status():
    prepared = dupes.prepare([dict(idea("a", "Alpha", status="wip"),
                                   merit_awarded=True)])
    assert prepared[0].shipped is True


def test_unlikely_is_not_shipped():
    prepared = dupes.prepare([idea("a", "Alpha", status="unlikely")])
    assert prepared[0].shipped is False


def test_is_shipped_reads_the_receipt_before_the_status():
    assert dupes.is_shipped({"status": "planned", "merit_awarded": True}) is True
    assert dupes.is_shipped({"status": "implemented"}) is True
    assert dupes.is_shipped({"status": "wip"}) is False


# --------------------------------------------------------------------------
# Siblings: related work, never the same idea twice. Declared, never inferred.
# --------------------------------------------------------------------------

def _prep(*rows):
    return {p.idea_id: p for p in dupes.prepare(rows)}


def test_one_depending_on_the_other_makes_them_siblings():
    p = _prep(idea("a", "Alpha"), dict(idea("b", "Beta"), depends_on=["a"]))
    assert dupes.are_siblings(p["a"], p["b"]) is True
    assert dupes.are_siblings(p["b"], p["a"]) is True     # symmetric


def test_a_shared_dependency_makes_them_siblings():
    # The prestige-quest shape: twelve items, one common parent.
    p = _prep(idea("parent", "Halmir as a general guide"),
              dict(idea("a", "Prestige quest: Harper Scout"), depends_on=["parent"]),
              dict(idea("b", "Prestige quest: Shifter"), depends_on=["parent"]))
    assert dupes.are_siblings(p["a"], p["b"]) is True


def test_unrelated_ideas_are_not_siblings():
    p = _prep(idea("a", "Alpha"), idea("b", "Beta"))
    assert dupes.are_siblings(p["a"], p["b"]) is False


def test_an_idea_is_not_its_own_sibling():
    p = _prep(dict(idea("a", "Alpha"), depends_on=["x"]))
    assert dupes.are_siblings(p["a"], p["a"]) is False


def test_siblinghood_is_not_inferred_from_the_epic():
    # `legendary-levels` holds both real duplicates and non-duplicates, so an
    # epic can never stand in for a declared link. Same epic, no depends_on.
    p = _prep(idea("a", "Legendary Feats: dominion feats", epic="legendary-levels"),
              idea("b", "Legendary Feats: arcane feats", epic="legendary-levels"))
    assert dupes.are_siblings(p["a"], p["b"]) is False


def test_rank_skips_siblings_of_the_subject():
    p = _prep(idea("parent", "Halmir fix"),
              dict(idea("a", "Prestige quest: Harper Scout"), depends_on=["parent"]),
              dict(idea("b", "Prestige quest: Shifter"), depends_on=["parent"]))
    others = [p["b"]]
    assert dupes.rank("Prestige quest: Harper Scout", "", others) != ()
    assert dupes.rank("Prestige quest: Harper Scout", "", others,
                      sibling_of=p["a"]) == ()


def test_a_malformed_depends_on_is_ignored_rather_than_raising():
    # The roadmap lint refuses these, but the bot must never crash on data it
    # merely read: a non-list, or a list with non-strings in it.
    p = _prep(dict(idea("a", "Alpha"), depends_on="not-a-list"),
              dict(idea("b", "Beta"), depends_on=[None, 7, "a"]))
    assert p["b"].depends_on == frozenset({"a"})
    assert dupes.are_siblings(p["a"], p["b"]) is True   # b depends on a
    # A bare string must yield NOTHING, not a set of its characters: two
    # malformed items sharing a letter would otherwise look like siblings and
    # silently suppress a real duplicate suggestion.
    assert p["a"].depends_on == frozenset()
    q = _prep(dict(idea("x", "X"), depends_on="not-a-list"),
              dict(idea("y", "Y"), depends_on="lemon"))
    assert dupes.are_siblings(q["x"], q["y"]) is False
