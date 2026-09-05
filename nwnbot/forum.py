"""Discord forum access: snapshots in, planned actions out.

Filled in alongside ``[b6-sync]`` and ``[b7-cli-runtime]``. Will hold:

- the read side: a plain-data snapshot of a forum channel (threads, their
  tags, authors, first post and replies) that the pure planners in
  ``nwnbot.sync`` consume;
- the write side: the executor that applies the planner's action list —
  create thread, post message, edit tags, archive/lock — rate-limited, with
  exponential backoff on 429.

Two forums, two item types: ``#bugs`` implies ``Defect`` and
``#feature-requests`` implies ``Enhancement`` (whether ``Exploit`` can come
from a tag is open review item ``r3``). Channel ids come from the environment;
none are hard-coded here.
"""

# Minimum delay between thread creations during a batch (seconds), per the
# backfill rate limit in plan.md.
THREAD_CREATE_DELAY_SECONDS = 2.0

__all__ = ["THREAD_CREATE_DELAY_SECONDS"]
