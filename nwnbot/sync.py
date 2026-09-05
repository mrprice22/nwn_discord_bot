"""Pure planners: (roadmap snapshot, forum snapshot, store) -> action list.

Filled in by ``[b6-sync]``, extended by ``[b9-dupes]``. Will hold
``plan_discord_to_roadmap()`` and ``plan_roadmap_to_discord()``.

**These functions stay pure.** They never call Discord or the roadmap API;
they return a list of planned actions that an executor applies. That is what
makes dry-run honest and the tests cheap, and every new behaviour is a new
action type plus a table-driven test rather than an inline side effect.

Notably, the Discord -> roadmap direction appends to the internal ``comments``
list and never writes ``notes``: ``notes`` is the admin's player-facing
release note and is only ever written by a human.

Loop prevention has three layers: skip messages authored by the bot's own user
id, a content hash per ``(idea, field)`` that skips unchanged values, and a
per-run action cap that aborts and reports rather than executing a runaway
batch.
"""

# Per-run cap on planned actions; exceeding it aborts the run and reports.
DEFAULT_ACTION_CAP = 25

__all__ = ["DEFAULT_ACTION_CAP"]
