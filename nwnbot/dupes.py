"""Duplicate scoring: how alike are a new forum thread and an existing idea?

Shipped by ``[b9-dupes]``. **Stdlib only** — :mod:`difflib` plus token-set
overlap. No fuzzy-match dependency, no index, no cache, no network.

The whole module is **pure**: same inputs, same answer, no clock, no I/O. That
is what lets :func:`rank` be called from inside ``nwnbot.sync``'s planners
without breaking their purity contract (``nwnbot/sync.py:6-12``), and what lets
``python -m nwnbot dupes --calibrate`` reuse the identical code path the live
bot runs.

**Scoring is a proposal, never a decision** (review item ``[r6]``, answered
2026-09-05). Nothing here writes ``dupe_of``; nothing here even knows the
thresholds. A number comes out, the planner puts it in a band, and a human with
editor access is the only thing that ever makes a duplicate real. A wrong merge
silently steals a player's merit credit, so the bot does not get to make one.

Two deliberate exclusions, both measured against the real ``roadmap.yaml``:

* ``notes`` is truncated (see ``DUPE_NOTES_MAX_CHARS``). It is p90 1,028 chars
  and runs to 4,347; a long note dilutes its token set into noise and starts
  matching every other long note.
* ``impl_notes`` is ignored outright — mean 2,547 chars, and it describes how a
  fix was *built*, not what a player reported. Including it matches items by
  technology instead of by symptom.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "are_siblings",
    "Candidate",
    "Prepared",
    "STOPWORDS",
    "group_words",
    "normalize",
    "prepare",
    "rank",
    "score",
    "tokens",
]


#: Words carrying no signal about *which* issue is being described. Ordinary
#: English glue, plus the handful of report-shaped words that appear in nearly
#: every bug report on the server and would otherwise pull unrelated items
#: together. Deliberately moderate: an over-eager list throws away the words
#: that actually distinguish two reports.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been being but by can cant could did do does doesnt
    doing dont for from get gets getting had has have having he her him his how
    i if in into is it its just me my no not of off on once only or other our
    out over please should so some such than that the their them then there
    these they this those to too up us use used using very was we were what
    when where which while who why will with would you your
    able add adding also always another anything back better bug currently
    else even ever every feature fix fixed idea instead issue keep like little
    lot made make making many maybe might much need needs new nothing now
    request seems still sure take thing things think time try trying want way
    work working
    """.split()
)

_WORD = re.compile(r"[^0-9a-z]+")


def normalize(text: str | None) -> str:
    """Casefold, drop punctuation, collapse whitespace. The comparison form."""
    if not text:
        return ""
    return " ".join(_WORD.sub(" ", str(text).casefold()).split())


def tokens(text: str | None, *, drop: Iterable[str] = ()) -> frozenset[str]:
    """The token set of ``text``, minus ``drop`` and minus one-character noise."""
    dropped = frozenset(drop)
    return frozenset(
        word for word in normalize(text).split()
        if len(word) > 1 and word not in dropped
    )


def group_words(*values: Any) -> frozenset[str]:
    """The words inside a group id or forum tag name, as a drop set.

    Every idea in ``forge`` shares the word "forge" and every thread tagged
    ``Forge & Crafting`` shares "forge" and "crafting". Left in, they score the
    whole group against itself: a report about smithing and a report about
    crafting recipes look alike for no better reason than living in the same
    bucket. Stripped from *both* sides, what is left is what the reports
    actually say.
    """
    out: set[str] = set()
    for value in values:
        if isinstance(value, str):
            out.update(w for w in normalize(value).split() if len(w) > 1)
        elif value:
            for item in value:
                out.update(w for w in normalize(str(item)).split() if len(w) > 1)
    return frozenset(out)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def score(title_a: str | None, body_a: str | None,
          title_b: str | None, body_b: str | None,
          *, drop: Iterable[str] = STOPWORDS, title_weight: float = 0.6) -> float:
    """How alike are two (title, body) pairs? ``0.0``-``1.0``, higher is closer.

    Two halves, because they fail differently. :class:`difflib.SequenceMatcher`
    over the normalized *titles* catches a reworded restatement of the same
    sentence; token overlap over title + body catches the report that says the
    same thing at a different length. Neither catches a paraphrase that reuses
    no words — that needs semantics, and is deliberately out of scope here (see
    the LLM design in the plan's *Future work*).

    Symmetric in its two arguments and free of hidden state, both asserted by
    tests: ``score(a, b) == score(b, a)``.
    """
    if not 0.0 <= title_weight <= 1.0:
        raise ValueError(f"title_weight must be in 0..1, got {title_weight!r}")
    dropped = frozenset(drop)

    left_title, right_title = normalize(title_a), normalize(title_b)
    title_ratio = (
        difflib.SequenceMatcher(None, left_title, right_title).ratio()
        if left_title and right_title else 0.0
    )

    left = tokens(f"{title_a or ''} {body_a or ''}", drop=dropped)
    right = tokens(f"{title_b or ''} {body_b or ''}", drop=dropped)
    overlap = _jaccard(left, right)

    return title_weight * title_ratio + (1.0 - title_weight) * overlap


#: Statuses that mean the work shipped. An idea in one of these is never
#: offered as something to merge INTO: the admin's rule is that an idea is not
#: reopened once it has been awarded or deployed, so a defect or a follow-up
#: reported afterwards is a new story in its own right, with its own merit.
#: `unlikely` is terminal but NOT shipped, and stays a legitimate merge target.
SHIPPED_STATUSES: frozenset[str] = frozenset({"awarded", "implemented"})


def is_shipped(idea: Mapping[str, Any]) -> bool:
    """Whether this idea is past the point of being reopened.

    ``merit_awarded`` is the merit DB's own receipt and is checked first: status
    can bounce, that boolean cannot, so it is the more reliable of the two.
    """
    if idea.get("merit_awarded") is True:
        return True
    return str(idea.get("status") or "") in SHIPPED_STATUSES


@dataclass(frozen=True)
class Prepared:
    """One candidate idea, with its HTML notes already flattened and cut.

    Built once per snapshot by :func:`prepare` rather than once per comparison:
    ``notes`` is HTML and flattening it is the expensive part, so a calibration
    run over ~163k pairs pays for 404 conversions, not 163k.
    """

    idea_id: str
    title: str
    group: str = ""
    body: str = ""
    #: Shipped: scoreable, but only ever reportable as an echo, never as a
    #: merge target. Kept in the pool rather than dropped so a report that
    #: resembles something already delivered can still be *recognised* as a
    #: regression or follow-up instead of silently looking novel.
    shipped: bool = False
    #: The idea's ``depends_on``, verbatim. Read only to decide siblinghood —
    #: see :func:`are_siblings`. Never inferred from titles or epics.
    depends_on: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Candidate:
    """One scored match. ``value`` is what the planner bands."""

    idea_id: str
    title: str
    value: float


def _flatten_notes(notes: Any, limit: int) -> str:
    """Roadmap ``notes`` are HTML. Reuse the real reader, never a second one."""
    if not notes:
        return ""
    from nwnbot import render  # local, like sync.py's own deferred render import

    text = render.html_to_md(str(notes))
    return text[:limit] if limit and len(text) > limit else text


def prepare(ideas: Iterable[Mapping[str, Any]], *,
            notes_max: int = 800) -> tuple[Prepared, ...]:
    """The scoreable form of a roadmap snapshot's ideas.

    Dupe rows are dropped: a second report must be matched against the item it
    would duplicate, never against another duplicate of it. ``impl_notes`` is
    not read at all.

    Shipped ideas are *kept and marked*, not dropped — see :data:`SHIPPED_STATUSES`.
    The caller decides what to do with them, which is what lets a match against
    delivered work be reported as an echo rather than as a duplicate.
    """
    out: list[Prepared] = []
    for idea in ideas:
        if not isinstance(idea, Mapping):
            continue
        idea_id = str(idea.get("id") or "")
        if not idea_id or idea.get("dupe_of"):
            continue
        out.append(Prepared(
            idea_id=idea_id,
            title=str(idea.get("title") or ""),
            group=str(idea.get("group") or ""),
            body=_flatten_notes(idea.get("notes"), notes_max),
            shipped=is_shipped(idea),
            depends_on=_depends_on(idea.get("depends_on")),
        ))
    return tuple(out)


def _depends_on(value: Any) -> frozenset[str]:
    """``depends_on`` as a set of ids, tolerating anything the lint would refuse.

    A ``str`` is rejected outright rather than iterated: a bare string is
    iterable, so accepting one would turn ``"not-a-list"`` into a set of
    *characters*, and two malformed items sharing any letter would then look
    like siblings and silently suppress a real duplicate suggestion.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(d.strip() for d in value if isinstance(d, str) and d.strip())


def are_siblings(a: Prepared, b: Prepared) -> bool:
    """Whether two ideas are related work rather than the same idea twice.

    True when one depends on the other, or when both depend on something in
    common. The second case is the important one: twelve prestige quests all
    waiting on the same fix are twelve pieces of work that share a parent, and
    they read as near-identical text.

    Deliberately reads only ``depends_on``, which a human wrote. Epics group
    the same families correctly but cannot be used for this — ``legendary-levels``
    holds both real duplicates and non-duplicates, so inferring siblinghood
    from it would suppress true positives. A declared link cannot guess wrong.
    """
    if a.idea_id == b.idea_id:
        return False
    if b.idea_id in a.depends_on or a.idea_id in b.depends_on:
        return True
    return bool(a.depends_on & b.depends_on)


def rank(title: str | None, body: str | None,
         candidates: Sequence[Prepared], *,
         tag_names: Iterable[str] = (),
         stopwords: Iterable[str] = STOPWORDS,
         title_weight: float = 0.6,
         limit: int = 5,
         exclude: Iterable[str] = (),
         sibling_of: Prepared | None = None) -> tuple[Candidate, ...]:
    """Score one thread against every candidate, best first.

    O(n) over the candidate list — ~404 ideas today, so no index is warranted
    and none is built. Ties break on ``idea_id`` so the result is stable across
    runs and a replay plans the same thing twice.
    """
    base = frozenset(stopwords) | group_words(tag_names)
    skip = frozenset(exclude)
    if sibling_of is not None:
        skip |= {c.idea_id for c in candidates if are_siblings(sibling_of, c)}
    scored: list[Candidate] = []
    for cand in candidates:
        if cand.idea_id in skip:
            continue
        drop = base | group_words(cand.group)
        value = score(title, body, cand.title, cand.body,
                      drop=drop, title_weight=title_weight)
        scored.append(Candidate(idea_id=cand.idea_id, title=cand.title, value=value))
    scored.sort(key=lambda c: (-c.value, c.idea_id))
    return tuple(scored[:limit]) if limit else tuple(scored)
