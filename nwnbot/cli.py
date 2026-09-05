"""Command line entry point: doctor, plan, apply, backfill, serve.

Filled in by ``[b7-cli-runtime]`` (``backfill`` by ``[b8-backfill]``). Will hold
the argument parser and the subcommand implementations:

- ``doctor``   — check env, credentials, and that all 12 forum tags map to all
  12 roadmap groups; exit non-zero on drift;
- ``plan``     — run the pure planners and print the action list; writes nothing;
- ``apply``    — execute the action list; refuses without ``--yes`` *and*
  ``NWNBOT_DRY_RUN=0``;
- ``backfill`` — write ``backfill-plan.md`` and stop; ``--yes`` executes the
  rate-limited batch, checkpointing each new thread id immediately;
- ``serve``    — run the ``nwnbot.bot`` runtime.
"""

COMMANDS = ("doctor", "plan", "apply", "backfill", "serve")

__all__ = ["COMMANDS"]
