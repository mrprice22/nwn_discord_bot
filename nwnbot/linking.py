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

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from nwnbot import dupes

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


@dataclass(frozen=True)
class Proposal:
    """One thread, and the ideas it might already be tracked by."""

    thread: ThreadRef
    candidates: tuple[dict, ...] = ()

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
    prepared = dupes.prepare(ideas)
    by_id = {str(i.get("id")): i for i in ideas}
    out: list[Proposal] = []
    for thread in threads:
        if thread.id in linked:
            continue
        cands = shortlist(thread, prepared, limit=limit)
        out.append(judge_thread(thread, cands, by_id, client,
                                scorer_id=scorer_id))
    # Most likely first, and a thread the admin already marked ahead of one
    # they did not: those are the rows where a yes/no is quickest.
    out.sort(key=lambda p: (
        not (p.best or {}).get("llm") == "same",
        not p.thread.marked,
        -float((p.best or {}).get("score") or 0.0),
    ))
    return out


def summarise(proposals: Sequence[Proposal]) -> dict:
    """Counts for the run summary, so the shape is visible before any linking."""
    agreed = sum(1 for p in proposals if (p.best or {}).get("llm") == "same")
    marked = sum(1 for p in proposals if p.thread.marked)
    none = sum(1 for p in proposals if not p.candidates)
    return {"threads": len(proposals), "model_agrees": agreed,
            "marked_by_admin": marked, "no_candidates": none}


__all__ = ["CHECK", "SALUTE", "SHORTLIST", "SHORTLIST_FLOOR", "Proposal",
           "ThreadRef", "judge_thread", "propose", "shortlist", "summarise"]
