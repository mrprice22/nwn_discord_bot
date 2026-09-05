"""Local sqlite state: thread<->idea links, content hashes, review queue.

Filled in by ``[b6-sync]``. Will hold the schema and accessors for:

- the thread id <-> idea id link (the mirror of the idea's ``discord`` field,
  kept locally so a planner can run without a round-trip);
- a content hash per ``(idea_id, field)``, the second of the three
  loop-prevention layers: an unchanged value plans no action;
- the review queue (unmatched authors, renamed threads, conflicting saves,
  duplicate proposals) that the CLI reports;
- decisions the admin made in the editor that the bot must not undo, e.g. a
  ``dupe_of`` removed by hand and therefore never re-added.

The store is pure local state; it is never the source of truth for anything
the roadmap or Discord already knows.
"""

from nwnbot.config import DEFAULT_DB_PATH

__all__ = ["DEFAULT_DB_PATH"]
