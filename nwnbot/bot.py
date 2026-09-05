"""The long-running Discord runtime.

Filled in by ``[b7-cli-runtime]``. Will hold a ``discord.Client`` with the
``message_content`` and ``guilds`` intents handling ``on_thread_create``,
``on_message`` and ``on_raw_thread_update``, plus a periodic full reconcile so
the event path and the poll path cannot diverge — both funnel through the same
pure planners in ``nwnbot.sync``.

Shipped with ``systemd/nwnbot.service`` as a user unit, ``Restart=on-failure``,
deliberately not enabled by default: arming it is a decision.
"""

# How often the full reconcile runs, in seconds (15 minutes).
RECONCILE_INTERVAL_SECONDS = 15 * 60

__all__ = ["RECONCILE_INTERVAL_SECONDS"]
