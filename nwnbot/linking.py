"""Matching existing Discord threads to roadmap ideas they already describe.

A one-off migration, not part of the sync loop. Both repositories predate the
bot: 37 forum threads and 423 roadmap ideas, and **not one idea carries a
``discord`` link**. Until they do, both directions duplicate — a backfill opens
a second thread for work that already has one, and an inbound sync mints a
second idea for a report already logged (the planner was observed about to
create ``anaralia-spider-queen-second-drop-2``).

Writing ``discord`` onto the idea fixes both at once: ``_linked_idea_id`` and
``_thread_id_for_idea`` in :mod:`nwnbot.sync` each fall back to that field, so
one write is seen from both sides.

**Every match is a proposal.** A wrong link is not cosmetic: status updates and
merit announcements for one player's report would be posted into another
player's thread. So this scores, shortlists and judges, and then writes a file
for a human to work through — it never links anything itself.

Three signals, in decreasing order of trust:

1. **The admin's own reaction convention** — a salute on a thread whose idea was
   created, a check mark once it shipped. Strong corroboration where present,
   but only 7 of 37 threads carry it, so it cannot drive the matching.
2. **The LLM judge**, asked whether the roadmap item is already tracking this
   report. Semantic, and the reason near-identical work with different wording
   is found at all.
3. **The token scorer**, which shortlists cheaply so the model is asked about a
   handful of candidates rather than 423.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from nwnbot import dupes

#: A link to another thread in the same guild. When the admin moves an idea
#: between #bugs and #feature-requests they open a new thread and CLOSE the old
#: one rather than deleting it, and leaving a link behind says which is which.
#: Declared beats inferred: without it the two threads are near-identical text
#: and only their timestamps hint at the direction.
THREAD_URL_RE = re.compile(
    r"https?://(?:\w+\.)?discord\.com/channels/(\d+)/(\d+)(?:/\d+)?")

#: The admin's convention, and what each mark means about the thread.
#: Neither is required and neither is sufficient — see the module docstring.
SALUTE = "\U0001fae1"      # 🫡  an idea was created in the roadmap for this
CHECK = "✅"           # ✅  it has been deployed / awarded

#: How many candidates the token scorer hands the judge per thread. The scorer
#: is cheap and the model is ~2s a call, so this is the whole cost knob.
SHORTLIST = 5

#: Below this the token scorer does not even offer a candidate to the judge.
#: Deliberately low: the judge is the precise half, and its whole value is
#: catching pairs whose wording diverged. Too high here and it never sees them.
SHORTLIST_FLOOR = 0.25


@dataclass(frozen=True)
class ThreadRef:
    """One Discord thread, as the matcher needs it."""

    id: str
    channel_id: str
    title: str
    body: str = ""
    url: str = ""
    archived: bool = False
    reactions: tuple[str, ...] = ()
    #: Every message in the thread, so a "moved to <link>" note can be found.
    #: Only the text is needed, so this is deliberately not ForumMessage.
    messages: tuple[str, ...] = ()
    #: Who opened the thread, resolved through the identity map where possible.
    #: Carried so the review tab can say who to credit: an idea the admin wrote
    #: first and a player later suggested independently is a real case, and the
    #: credit decision needs a name in front of it, not a lookup.
    author_id: str = ""
    author: str = ""

    @property
    def marked_created(self) -> bool:
        return SALUTE in self.reactions

    @property
    def marked_shipped(self) -> bool:
        return CHECK in self.reactions

    @property
    def marked(self) -> bool:
        """Whether the admin marked this thread as already in the roadmap."""
        return self.marked_created or self.marked_shipped

    @property
    def superseded_by(self) -> str:
        """The thread this one was moved to, or "".

        Only read on a CLOSED thread. An open thread linking to another is
        ordinary cross-referencing — "see also" — and reading that as a move
        would silently retire a live thread. A closed one carrying a link is
        the admin's own convention for "this went over there", which is the
        whole reason it is worth honouring.
        """
        if not self.archived:
            return ""
        for text in self.messages:
            for _guild, thread_id in THREAD_URL_RE.findall(text or ""):
                if thread_id != self.id:
                    return thread_id
        return ""


@dataclass(frozen=True)
class Proposal:
    """One thread, and the ideas it might already be tracked by."""

    thread: ThreadRef
    candidates: tuple[dict, ...] = ()

    #: Set when this thread was closed in favour of another one.
    superseded_by: str = ""

    @property
    def best(self) -> dict | None:
        return self.candidates[0] if self.candidates else None

    def as_dict(self) -> dict:
        return {
            "thread_id": self.thread.id,
            "channel_id": self.thread.channel_id,
            "title": self.thread.title,
            "url": self.thread.url,
            "archived": self.thread.archived,
            "marked_created": self.thread.marked_created,
            "marked_shipped": self.thread.marked_shipped,
            "author": self.thread.author,
            "author_id": self.thread.author_id,
            "superseded_by": self.superseded_by,
            "candidates": [dict(c) for c in self.candidates],
        }


def shortlist(thread: ThreadRef, prepared: Sequence[dupes.Prepared], *,
              limit: int = SHORTLIST,
              floor: float = SHORTLIST_FLOOR) -> list[dupes.Candidate]:
    """The cheap pass: which ideas are worth asking the model about.

    Shipped ideas are NOT excluded here, unlike duplicate detection. That rule
    is about not reopening delivered work; this is about discovering that a
    thread is already tracked, and a thread whose idea shipped is precisely the
    case the check-mark convention marks.
    """
    ranked = dupes.rank(thread.title, thread.body, prepared,
                        title_weight=0.75, limit=limit)
    return [c for c in ranked if c.value >= floor]


def judge_thread(thread: ThreadRef, candidates: Sequence[dupes.Candidate],
                 by_id: Mapping[str, Mapping[str, Any]],
                 client: Any = None, *,
                 scorer_id: str = "stdlib-token-v1") -> Proposal:
    """Score, then ask the model about the shortlist. Never links anything.

    With no client, or a client that cannot answer, the token score stands
    alone and every row says so in ``by`` — so a later pass can tell which
    proposals a model has actually seen.
    """
    rows: list[dict] = []
    for cand in candidates:
        idea = by_id.get(cand.idea_id) or {}
        row = {
            "id": cand.idea_id,
            "title": cand.title,
            "score": round(cand.value, 3),
            "status": str(idea.get("status") or ""),
            "shipped": bool(idea.get("merit_awarded")
                            or idea.get("status") in ("awarded", "implemented")),
            "by": scorer_id,
        }
        if client is not None:
            verdict = client.judge_link(
                thread.title, thread.body, cand.title,
                dupes._flatten_notes(idea.get("notes"), 600))
            if verdict is not None:
                row["llm"] = "same" if verdict.same else "different"
                row["why"] = verdict.why
                row["by"] = verdict.model or scorer_id
        rows.append(row)

    # The model's opinion outranks the token score: that is the whole reason it
    # is asked. Within each group the score still orders, so the most plausible
    # of several "same" answers is first.
    rows.sort(key=lambda r: (r.get("llm") != "same", -r["score"]))
    return Proposal(thread=thread, candidates=tuple(rows))


def propose(threads: Iterable[ThreadRef], ideas: Sequence[Mapping[str, Any]],
            client: Any = None, *, scorer_id: str = "stdlib-token-v1",
            limit: int = SHORTLIST) -> list[Proposal]:
    """Every unlinked thread, with its ranked candidate ideas.

    Threads already linked to an idea are skipped: this is a migration, and
    re-proposing a settled link would be noise the admin has to dismiss.
    """
    linked = {str((i.get("discord") or {}).get("thread_id") or "")
              for i in ideas if isinstance(i.get("discord"), Mapping)}
    threads = list(threads)
    ids = {t.id for t in threads}
    prepared = dupes.prepare(ideas)
    by_id = {str(i.get("id")): i for i in ideas}
    out: list[Proposal] = []
    for thread in threads:
        if thread.id in linked:
            continue
        moved_to = thread.superseded_by
        if moved_to and moved_to in ids:
            # The admin moved this idea between #bugs and #feature-requests,
            # closed this thread and left a link to its replacement. The
            # replacement is the one that should carry the roadmap link, and
            # asking the model about a thread whose answer is already written
            # down is both wasted time and an invitation to link the wrong one.
            out.append(Proposal(thread=thread, candidates=(),
                                superseded_by=moved_to))
            continue
        cands = shortlist(thread, prepared, limit=limit)
        out.append(judge_thread(thread, cands, by_id, client,
                                scorer_id=scorer_id))
    # Most likely first, and a thread the admin already marked ahead of one
    # they did not: those are the rows where a yes/no is quickest.
    # Superseded rows sink: they need one dismissal, not a decision.
    out.sort(key=lambda p: (
        bool(p.superseded_by),
        not (p.best or {}).get("llm") == "same",
        not p.thread.marked,
        -float((p.best or {}).get("score") or 0.0),
    ))
    return out


def summarise(proposals: Sequence[Proposal]) -> dict:
    """Counts for the run summary, so the shape is visible before any linking."""
    agreed = sum(1 for p in proposals if (p.best or {}).get("llm") == "same")
    marked = sum(1 for p in proposals if p.thread.marked)
    moved = sum(1 for p in proposals if p.superseded_by)
    none = sum(1 for p in proposals if not p.candidates and not p.superseded_by)
    return {"threads": len(proposals), "model_agrees": agreed,
            "marked_by_admin": marked, "no_candidates": none,
            "superseded": moved}


__all__ = ["CHECK", "SALUTE", "SHORTLIST", "SHORTLIST_FLOOR", "Proposal",
           "ThreadRef", "judge_thread", "propose", "shortlist", "summarise"]
