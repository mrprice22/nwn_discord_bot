"""The runtime: one funnel from snapshots, through the planners, to writes.

Shipped by ``[b7-cli-runtime]``. Three things live here:

1. :func:`plan_all` — **the only place in the codebase that calls a planner.**
   Both directions, one call, no arguments of its own. Every entry point below
   goes through it, which is the mechanical reason the event path and the
   15-minute reconcile cannot drift apart: there is no second code path for
   them to drift *into*.
2. :class:`SyncEngine` — gathers a snapshot pair, calls :func:`plan_all`,
   and (only when explicitly told to) executes the resulting actions,
   recording each applied action in the store. A dry run walks the identical
   code and simply does not make the call.
3. :class:`EventFunnel` and :func:`make_client` — the ``discord.Client`` with
   the ``message_content`` and ``guilds`` intents, handling
   ``on_thread_create``, ``on_message`` and ``on_raw_thread_update``. **None of
   the handlers plan anything.** Each one does exactly one thing: mark the
   world dirty. A single worker coroutine then runs one
   :meth:`SyncEngine.cycle`, and the reconcile loop marks the world dirty on a
   timer through that same call. An event and a timer tick are therefore
   indistinguishable by the time any planning happens.

``discord.py`` is imported **lazily**, inside :func:`make_client` and
:class:`DiscordForumWriter`, and nowhere else. Importing this module, running
``plan``, and the whole test suite need no token, no gateway and no socket.

Shipped with ``systemd/nwnbot.service`` as a user unit, ``Restart=on-failure``,
deliberately not enabled by default: arming it is a decision.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from nwnbot import attachments, config as cfg
from nwnbot.forum import (Attachment, ForumSnapshot, ForumThread, ForumMessage,
                          ForumWriter, RecordingForumWriter)
from nwnbot.roadmap import SaveConflict, Snapshot
from nwnbot.store import StoreView
from nwnbot.sync import (
    unlinked_threads,
    AppendComment,
    ArchiveThread,
    CreateIdea,
    CreateThread,
    Plan,
    PlanContext,
    PostMessage,
    RecordBaseline,
    ReviewItem,
    UpdateIdeaField,
    plan_discord_to_roadmap,
    plan_roadmap_to_discord,
)

log = logging.getLogger("nwnbot")

#: A Discord user mention. `<@123>` and `<@!123>` are the same thing; the bang
#: form is legacy but still turns up in older messages.
_MENTION_RE = re.compile(r"<@!?(\d+)>")

# How often the full reconcile runs, in seconds (15 minutes, per plan.md).
RECONCILE_INTERVAL_SECONDS = 15 * 60

# Settled by review item [r12].1 — approved at 5 s. A burst of forum activity
# (someone posting five replies in a row) should cost one reconcile, not five.
# Events set a dirty flag and this is how long the worker waits for the burst to
# settle before planning. Small enough that the bot still feels immediate; large
# enough that a conversation is one cycle, with the 15-minute reconcile as the
# backstop if a burst is ever missed entirely.
EVENT_DEBOUNCE_SECONDS = 5.0

# How often to ask the roadmap whether its file has changed, in seconds.
#
# The two directions had wildly different latency and only one of them was
# visible: Discord PUSHES events, so a new report is picked up in seconds, while
# the roadmap can push nothing, so an approval waited for the next 15-minute
# sweep. To the admin that reads as the approval being broken -- they click,
# they check the thread, nothing is there.
#
# So: ask for a content hash (`/api/version`, a few dozen bytes) on this cadence
# and only run a real cycle when it moves. One small request a minute against a
# LAN service, and an approval reaches the reporter inside a minute instead of
# up to fifteen. The reconcile sweep stays as the backstop -- this is an
# optimisation on top of it, never a replacement for it, which is why a failed
# poll is simply skipped.
ROADMAP_POLL_SECONDS = 30.0

#: Review kind recorded when /api/save comes back `conflict: true` twice.
#: [r10]'s proposed answer: record it, report it, exit non-zero. No retry (the
#: client already retried once), and never a force.
REVIEW_SAVE_CONFLICT = "save_conflict"


# --------------------------------------------------------------------------
# The one funnel
# --------------------------------------------------------------------------
def plan_all(roadmap: Snapshot, forum: ForumSnapshot, view: StoreView,
             context: PlanContext) -> tuple[Plan, Plan]:
    """Run both pure planners over one snapshot pair. Nothing executes.

    Every caller — ``plan``, ``apply``, ``backfill``, a Discord event, the
    15-minute reconcile — arrives here. Grep for ``plan_discord_to_roadmap``
    and ``plan_roadmap_to_discord``: this function is the only hit outside
    :mod:`nwnbot.sync` itself and its tests.
    """
    return (plan_discord_to_roadmap(roadmap, forum, view, context),
            plan_roadmap_to_discord(roadmap, forum, view, context))


# --------------------------------------------------------------------------
# Where a snapshot pair comes from
# --------------------------------------------------------------------------
class SnapshotSource:
    """Produces one (roadmap, forum) pair. The seam that keeps tests offline."""

    async def snapshots(self) -> tuple[Snapshot, ForumSnapshot]:
        raise NotImplementedError


@dataclass
class StaticSource(SnapshotSource):
    """A fixed pair, loaded from a fixture file or built in a test."""

    roadmap: Snapshot
    forum: ForumSnapshot

    async def snapshots(self) -> tuple[Snapshot, ForumSnapshot]:
        return self.roadmap, self.forum


# --------------------------------------------------------------------------
# What a run produced
# --------------------------------------------------------------------------
@dataclass
class RunReport:
    """The outcome of one cycle: what was planned, what happened, what broke."""

    dry_run: bool = True
    reason: str = ""
    plans: tuple[Plan, ...] = ()
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def actions(self) -> tuple:
        return tuple(a for p in self.plans for a in p.actions)

    @property
    def writes(self) -> tuple:
        return tuple(a for p in self.plans for a in p.writes)

    @property
    def reviews(self) -> tuple[ReviewItem, ...]:
        return tuple(a for p in self.plans for a in p.reviews)

    @property
    def ok(self) -> bool:
        return not (self.failures or self.conflicts or self.aborted)

    def summary(self) -> list[str]:
        """Human-readable run summary, one line per fact worth knowing."""
        lines: list[str] = []
        for plan in self.plans:
            lines.append(f"{plan.direction}: {len(plan.writes)} write(s), "
                         f"{len(plan.reviews)} review item(s), "
                         f"{len(plan.baselines)} baseline(s)"
                         + ("  [ABORTED: over the action cap]" if plan.aborted else ""))
            if plan.aborted and plan.reason:
                lines.append(f"  cap: {plan.reason}")
        if self.dry_run:
            lines.append("dry run: nothing was written to Discord or the roadmap")
        else:
            lines.append(f"applied {len(self.applied)} action(s), "
                         f"skipped {len(self.skipped)}, "
                         f"{len(self.failures)} failure(s)")
        for conflict in self.conflicts:
            lines.append(f"SAVE CONFLICT (recorded, not retried, not forced): {conflict}")
        for failure in self.failures:
            lines.append(f"FAILED: {failure}")
        return lines


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------
class SyncEngine:
    """Snapshot -> :func:`plan_all` -> (optionally) execute -> record.

    ``dry_run`` defaults to **True**. Executing is the thing you have to ask
    for, in two places at once (``apply --yes`` *and* ``NWNBOT_DRY_RUN=0``);
    nothing here can start writing because a default was left off.
    """

    def __init__(self, source: SnapshotSource, context: PlanContext, *,
                 store: Any = None, roadmap_client: Any = None,
                 forum_writer: ForumWriter | None = None,
                 dry_run: bool = True, strict_config: bool = False,
                 pace: float = 0.0, llm_client: Any = None) -> None:
        self.source = source
        self.context = context
        self.store = store
        self.roadmap_client = roadmap_client
        self.forum_writer = forum_writer if forum_writer is not None else RecordingForumWriter()
        #: Optional. ``None`` means no summaries and no duplicate judging: the
        #: token scorer stands alone, which is a supported state.
        self.llm_client = llm_client
        self.dry_run = dry_run
        #: `[b5-config]`'s startup validation. On the live path the tag mapping
        #: is checked against the editor's own `vocab` and the forums' real
        #: `available_tags` the first time a snapshot pair arrives, and drift on
        #: either side raises `ConfigError` before anything is planned. Off for
        #: fixtures and tests, whose fake worlds carry fake tag names on
        #: purpose.
        self.strict_config = strict_config
        self._config_validated = False
        #: `[b8-backfill]`. Seconds to wait between thread creations. Zero on
        #: every path but `backfill`: a reconcile plans a handful of actions and
        #: sleeping through them would stall the event loop for no reason.
        self.pace = pace
        self._threads_made = 0

    def view(self) -> StoreView:
        if self.store is None:
            return StoreView.empty()
        if isinstance(self.store, StoreView):
            return self.store
        return self.store.view()

    async def cycle(self, *, reason: str = "") -> RunReport:
        """One full pass. The *only* entry point events and the timer use."""
        roadmap, forum = await self.source.snapshots()
        self.validate_config(roadmap, forum)
        view = self.view()
        # The one model call a cycle makes, and it happens HERE rather than in
        # a planner: the planners are pure and synchronous, and the fixed-point
        # tests depend on it. Only threads about to become ideas are asked
        # about, so a quiet cycle costs nothing.
        context = self.context
        if self.llm_client is not None:
            summaries = summarise_new_threads(roadmap, forum, view,
                                              self.llm_client)
            if summaries:
                context = replace(context, summaries=summaries)
        plans = plan_all(roadmap, forum, view, context)
        report = RunReport(dry_run=self.dry_run, reason=reason, plans=plans)
        report.aborted = any(p.aborted for p in plans)

        if report.aborted:
            # Loop-prevention layer three. Report the runaway batch; run none of
            # it. The review item the planner already appended still gets
            # recorded so the question survives the process.
            self._record_state_only(plans)
            return report
        if self.dry_run:
            return report

        for plan in plans:
            await self._execute(plan, roadmap, report)
            if report.conflicts:
                break  # [r10]: stop on a conflict, do not retry, never force
        return report

    def validate_config(self, roadmap: Snapshot, forum: ForumSnapshot) -> None:
        """Fail loudly if the tag mapping has drifted from the live systems.

        Runs once per engine, on the first cycle, because that is the first
        moment both sides are actually in hand. A forum that gained, lost or
        renamed a tag, or an editor whose `groups:` moved, stops the run here
        rather than filing every new thread under the wrong group.
        """
        if not self.strict_config or self._config_validated:
            return
        cfg.validate_tag_map(self.context.tag_groups,
                             available_tags=forum.available_tags,
                             vocab_group_ids=roadmap.vocab.get("groups") or (),
                             where="the tag -> group mapping")
        self._config_validated = True

    # -- execution ---------------------------------------------------------
    def _record_state_only(self, plans: Iterable[Plan]) -> None:
        if self.store is None or isinstance(self.store, StoreView):
            return
        for plan in plans:
            for action in plan.actions:
                if isinstance(action, (ReviewItem, RecordBaseline)):
                    self.store.record(action)

    def _record(self, action: Any) -> None:
        if self.store is not None and not isinstance(self.store, StoreView):
            self.store.record(action)

    async def _execute(self, plan: Plan, roadmap: Snapshot, report: RunReport) -> None:
        for action in plan.actions:
            try:
                applied = await self._apply_one(action, roadmap)
            except SaveConflict as exc:
                detail = (f"{action.describe()}: {exc}")
                report.conflicts.append(detail)
                self._queue_conflict(action, detail)
                return
            except Exception as exc:  # one bad action must not lose the rest
                report.failures.append(f"{action.describe()}: {exc!r}")
                log.exception("action failed: %s", action.key)
                continue
            (report.applied if applied else report.skipped).append(action.describe())

    def _queue_conflict(self, action: Any, detail: str) -> None:
        """[r10]'s conservative answer: it goes in the store and in the summary."""
        if self.store is None or isinstance(self.store, StoreView):
            return
        self.store.queue_review(f"{REVIEW_SAVE_CONFLICT}:{action.key}",
                                REVIEW_SAVE_CONFLICT, subject=action.key,
                                detail=detail)

    async def _create_thread_with_backoff(self, action: Any) -> str:
        """One thread creation, retried through Discord's rate limiter.

        Waits the server's own ``retry_after`` when it sends one and doubles a
        local backoff otherwise. A 429 is not a failure -- it is the API asking
        us to slow down -- so it must not land in the run report as one, and it
        must not abort the batch: everything already created is checkpointed and
        the remaining work is still worth doing.
        """
        delay = self.pace or cfg.BACKFILL_MIN_INTERVAL
        for attempt in range(cfg.BACKFILL_MAX_RETRIES + 1):
            try:
                return await self.forum_writer.create_thread(
                    action.channel_id, action.title, action.body, action.tag_names)
            except Exception as exc:
                # Duck-typed on purpose: discord.py's HTTPException carries
                # `status`, and the fakes in the tests carry the same two
                # attributes, so neither this module nor its tests import it.
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                if status != 429 or attempt >= cfg.BACKFILL_MAX_RETRIES:
                    raise
                wait = float(getattr(exc, "retry_after", 0) or delay)
                log.warning("rate limited creating a thread for %s; waiting %.1fs "
                            "(attempt %d)", action.idea_id, wait, attempt + 1)
                await asyncio.sleep(wait)
                delay *= 2
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _apply_one(self, action: Any, roadmap: Snapshot) -> bool:
        """Do one action for real. Returns False for state-only actions."""
        if isinstance(action, (ReviewItem, RecordBaseline)):
            self._record(action)
            return False

        if isinstance(action, CreateIdea):
            await self._roadmap().new_idea(dict(action.idea))
        elif isinstance(action, AppendComment):
            await self._roadmap().comment(action.idea_id, action.text)
        elif isinstance(action, UpdateIdeaField):
            await self._roadmap().save(_set_field(action.idea_id, action.field_name,
                                                  action.value))
        elif isinstance(action, CreateThread):
            # [b8-backfill]. Pace the batch and ride out a 429. `pace` is 0 on
            # the ordinary cycle -- a reconcile plans a handful of actions and
            # must not sleep -- and only `backfill` sets it.
            if self.pace and self._threads_made:
                await asyncio.sleep(self.pace)
            thread_id = await self._create_thread_with_backoff(action)
            self._threads_made += 1
            # Checkpoint the link on both sides *immediately*: an interruption
            # after this point resumes, it does not open a second thread.
            if self.store is not None and not isinstance(self.store, StoreView):
                self.store.link_thread(thread_id, action.idea_id, action.channel_id)
            await self._roadmap().save(_set_field(
                action.idea_id, "discord",
                {"thread_id": thread_id, "channel_id": action.channel_id,
                 "url": self.context.thread_url(thread_id)}))
        elif isinstance(action, PostMessage):
            await self.forum_writer.post_message(action.thread_id, action.text)
        elif isinstance(action, ArchiveThread):
            await self.forum_writer.archive_thread(action.thread_id,
                                                   locked=action.locked)
        else:
            raise TypeError(f"no executor for action type {type(action).__name__}")

        self._record(action)
        return True

    def _roadmap(self) -> Any:
        if self.roadmap_client is None:
            raise RuntimeError(
                "this action writes to the roadmap but no client was configured")
        return self.roadmap_client


def _set_field(idea_id: str, field_name: str, value: Any):
    """A ``mutate`` for :meth:`RoadmapClient.save` that touches exactly one field.

    The client's own assertions run over the result, so a forbidden field or a
    vanished idea raises before a request goes out.
    """

    def mutate(ideas: list[dict]) -> None:
        for idea in ideas:
            if idea.get("id") == idea_id:
                idea[field_name] = value
                return
        raise KeyError(f"idea {idea_id!r} is not in the document any more")

    return mutate


# --------------------------------------------------------------------------
# The Discord event path
# --------------------------------------------------------------------------
class EventFunnel:
    """Turns every Discord event and every timer tick into one dirty flag.

    Kept free of ``discord.py`` so it can be tested without a gateway.
    :func:`make_client` mixes it into a real ``discord.Client``.

    The handlers below plan **nothing**. They cannot: they have no snapshot, no
    store and no planner — only :meth:`request_cycle`. Whatever wakes the bot,
    the work that follows is one :meth:`SyncEngine.cycle`.
    """

    def __init__(self, engine: SyncEngine, *,
                 debounce: float = EVENT_DEBOUNCE_SECONDS,
                 reconcile_interval: float = RECONCILE_INTERVAL_SECONDS,
                 roadmap_poll: float = ROADMAP_POLL_SECONDS,
                 roadmap_client: Any = None) -> None:
        self.engine = engine
        self.debounce = debounce
        self.reconcile_interval = reconcile_interval
        self.roadmap_poll = roadmap_poll
        #: Only for the version poll. None disables it, which is what every
        #: offline test and `--fixture` gets: no client, no polling, and the
        #: reconcile loop alone -- exactly the behaviour before this existed.
        self.roadmap_client = roadmap_client
        #: Last seen roadmap file hash. None means "not asked yet", which is
        #: NOT the same as "unchanged": the first answer must be adopted
        #: silently, or every startup would plan a cycle it does not need.
        self._roadmap_version: str | None = None
        self.reasons: list[str] = []
        self.cycles: list[RunReport] = []
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # -- the funnel --------------------------------------------------------
    def request_cycle(self, reason: str) -> None:
        """Mark the world dirty. The single thing every event handler does."""
        self.reasons.append(reason)
        self._wake.set()

    async def run_pending(self) -> RunReport | None:
        """Drain the dirty flag and run exactly one cycle. Coalesces a burst."""
        if not self._wake.is_set():
            return None
        self._wake.clear()
        reasons, self.reasons = self.reasons, []
        report = await self.engine.cycle(reason=", ".join(reasons[:5]))
        self.cycles.append(report)
        for line in report.summary():
            log.info("%s", line)
        return report

    # -- the two paths, which are the same path ---------------------------
    async def worker(self) -> None:  # pragma: no cover - driven by the loop
        while True:
            await self._wake.wait()
            await asyncio.sleep(self.debounce)
            try:
                await self.run_pending()
            except Exception:
                log.exception("sync cycle failed; the next event or reconcile retries")

    async def reconcile_loop(self) -> None:  # pragma: no cover - driven by the loop
        while True:
            await asyncio.sleep(self.reconcile_interval)
            # Not a second code path: the timer pulls the same lever an event does.
            self.request_cycle("reconcile")

    async def poll_roadmap_once(self) -> bool:
        """Ask for the roadmap's content hash; wake the bot if it moved.

        Returns whether a cycle was requested, which is what the tests assert
        on. Separate from the loop below so the decision is testable without a
        clock: the loop is a sleep around this.
        """
        if self.roadmap_client is None:
            return False
        version = await self.roadmap_client.version()
        if not version:
            return False  # the poll failed; the reconcile sweep still covers it
        first, self._roadmap_version = self._roadmap_version, version
        if first is None or first == version:
            return False
        self.request_cycle("roadmap changed")
        return True

    async def roadmap_loop(self) -> None:  # pragma: no cover - driven by the loop
        while True:
            await asyncio.sleep(self.roadmap_poll)
            try:
                await self.poll_roadmap_once()
            except Exception:
                # Never let a poll failure kill the loop: a dead roadmap for a
                # minute must not silently cost us every future poll.
                log.exception("roadmap version poll failed; retrying next tick")

    def start(self) -> None:  # pragma: no cover - needs a running loop
        self._tasks = [asyncio.create_task(self.worker()),
                       asyncio.create_task(self.reconcile_loop())]
        if self.roadmap_client is not None:
            self._tasks.append(asyncio.create_task(self.roadmap_loop()))
        self.request_cycle("startup")

    async def stop(self) -> None:  # pragma: no cover - shutdown path
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    # -- Discord event handlers -------------------------------------------
    async def on_thread_create(self, thread: Any) -> None:
        self.request_cycle(f"thread_create:{getattr(thread, 'id', '?')}")

    async def on_message(self, message: Any) -> None:
        # Loop-prevention layer one, at the very edge: never react to our own
        # message. The planners skip it too; both is deliberate.
        author = getattr(message, "author", None)
        author_id = str(getattr(author, "id", "") or "")
        if author_id and author_id == str(self.engine.context.bot_user_id or ""):
            return
        self.request_cycle(f"message:{getattr(message, 'id', '?')}")

    async def on_raw_thread_update(self, payload: Any) -> None:
        self.request_cycle(f"thread_update:{getattr(payload, 'thread_id', '?')}")


def make_intents():  # pragma: no cover - needs discord.py
    """``message_content`` + ``guilds``, exactly as ``[b7-cli-runtime]`` says."""
    import discord

    intents = discord.Intents.none()
    intents.guilds = True
    intents.message_content = True
    intents.guild_messages = True  # message_content is meaningless without it
    return intents


def make_client(engine: SyncEngine, **kwargs):  # pragma: no cover - needs discord.py
    """Build the ``discord.Client``. The only place the gateway is touched."""
    import discord

    funnel_kwargs = kwargs

    class NwnBotClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(intents=make_intents())
            self.funnel = EventFunnel(engine, **funnel_kwargs)

        async def setup_hook(self) -> None:
            self.funnel.start()

        async def on_ready(self) -> None:
            log.info("connected as %s", self.user)

        async def on_thread_create(self, thread) -> None:
            await self.funnel.on_thread_create(thread)

        async def on_message(self, message) -> None:
            await self.funnel.on_message(message)

        async def on_raw_thread_update(self, payload) -> None:
            await self.funnel.on_raw_thread_update(payload)

        async def close(self) -> None:
            await self.funnel.stop()
            await super().close()

    return NwnBotClient()


# --------------------------------------------------------------------------
# The live adapters — the only discord.py in the repo
# --------------------------------------------------------------------------
class DiscordForumWriter(ForumWriter):  # pragma: no cover - needs a gateway
    """Applies forum actions through a live ``discord.Client``.

    Never constructed by ``plan``, by a test, or by anything that has not been
    handed ``--yes`` and ``NWNBOT_DRY_RUN=0``.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def _channel(self, channel_id: str):
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        return channel

    async def _thread(self, thread_id: str):
        return await self._channel(thread_id)

    async def create_thread(self, channel_id: str, title: str, body: str,
                            tag_names: tuple[str, ...] = ()) -> str:
        channel = await self._channel(channel_id)
        wanted = {name.lower() for name in tag_names}
        tags = [t for t in getattr(channel, "available_tags", []) or []
                if t.name.lower() in wanted]
        created = await channel.create_thread(name=title, content=body,
                                              applied_tags=tags)
        thread = getattr(created, "thread", created)
        return str(thread.id)

    async def post_message(self, thread_id: str, text: str) -> str:
        thread = await self._thread(thread_id)
        message = await thread.send(text)
        return str(message.id)

    async def archive_thread(self, thread_id: str, *, locked: bool = False) -> None:
        thread = await self._thread(thread_id)
        await thread.edit(archived=True, locked=locked)


async def build_forum_snapshot(client: Any, channel_ids: Iterable[str],
                               bot_user_id: str = "",
                               players: Mapping[str, str] | None = None,
                               ) -> ForumSnapshot:  # pragma: no cover - needs a gateway
    """Read the forums into the plain data the planners consume."""
    threads: list[ForumThread] = []
    available: dict[str, tuple[str, ...]] = {}
    for channel_id in channel_ids:
        if not channel_id:
            continue
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        forum_tags = getattr(channel, "available_tags", []) or []
        available[str(channel_id)] = tuple(t.name for t in forum_tags)
        # Tag id -> name for this forum. `Thread.applied_tags` resolves ids
        # through `thread.parent`, which is a *guild cache* lookup and so is
        # None on the gateway-less `plan` path -- it then returns [] and every
        # thread looks untagged, which reads as "no group" and files nothing.
        # We already hold the forum channel here, so resolve them ourselves.
        tag_names = {str(t.id): t.name for t in forum_tags}
        seen = list(getattr(channel, "threads", []) or [])
        if not seen:
            # `channel.threads` is the *gateway cache*, and the `plan` /
            # `backfill` path deliberately logs in over HTTP with no gateway
            # (cli.py:_live), so that cache is always empty there. Archived
            # threads still arrive over REST below, but `_plan_new_idea` skips
            # archived threads as history -- so without this fallback the whole
            # Discord->roadmap direction silently plans nothing on the CLI
            # path, which looks exactly like "already in sync".
            seen = await _active_threads_via_rest(client, channel)
        async for archived in channel.archived_threads(limit=None):
            seen.append(archived)
        for thread in seen:
            threads.append(await _read_thread(thread, str(channel_id), tag_names,
                                              players))
    return ForumSnapshot(tuple(threads), bot_user_id=str(bot_user_id or ""),
                         available_tags=available)


def _applied_tag_names(thread: Any, tag_names: Mapping[str, str] | None
                       ) -> tuple[str, ...]:  # pragma: no cover - needs a gateway
    """The forum tags on a thread, by name.

    `Thread.applied_tags` is preferred and is what runs under `serve`. It goes
    through `thread.parent`, though, which is a guild-cache lookup, so on the
    gateway-less `plan`/`backfill` path it yields nothing and the thread reads
    as untagged. Falling back to the raw ids resolved against the forum channel
    we already fetched keeps both paths agreeing, which is the property
    `[b7-cli-runtime]` asserts about the event and poll paths.
    """
    names = tuple(t.name for t in getattr(thread, "applied_tags", None) or ())
    if names or not tag_names:
        return names
    raw = getattr(thread, "_applied_tags", None) or ()
    return tuple(tag_names[str(i)] for i in raw if str(i) in tag_names)


def resolve_mentions(text: str, mentions: Any = (),
                     players: Mapping[str, str] | None = None) -> str:
    """Turn ``<@123…>`` into a name a human can read.

    Discord stores a mention as a bare id and renders it in the client only.
    Copied anywhere else -- an idea's notes, an internal comment, a roadmap
    page -- it is an 18-digit number, and "requested by <@139336304784703488>"
    tells the reader nothing at all.

    The roadmap's own player name is preferred over the Discord display name:
    it is what the `player` field and the merit ledger use, so an admin reading
    "requested by @Sync (Shync)" can act on it directly.
    """
    if not text or "<@" not in text:
        return text or ""
    by_id = {str(getattr(u, "id", "")): (getattr(u, "display_name", "")
                                         or getattr(u, "name", ""))
             for u in (mentions or ())}
    lookup = dict(players or {})

    def sub(match):
        uid = match.group(1)
        # Roadmap name first, Discord display name second, the raw id last --
        # never silently dropped, because an unresolvable mention is itself
        # worth seeing.
        name = lookup.get(uid) or by_id.get(uid)
        return f"@{name}" if name else match.group(0)

    return _MENTION_RE.sub(sub, text)


def _attachments_of(message: Any) -> tuple:  # pragma: no cover - needs a gateway
    """The files posted with a message.

    Worth stating because it is the whole reason this exists: a Discord message
    whose only content is a screenshot has ``content == ""``. Reading just
    ``content`` — which is what the bot did until now — dropped those messages
    entirely and every image in every report with them.
    """
    out = []
    for a in getattr(message, "attachments", None) or ():
        out.append(Attachment(
            id=str(getattr(a, "id", "")),
            filename=str(getattr(a, "filename", "") or ""),
            url=str(getattr(a, "url", "") or ""),
            content_type=str(getattr(a, "content_type", "") or ""),
            size=int(getattr(a, "size", 0) or 0),
        ))
    return tuple(out)


async def _active_threads_via_rest(client: Any, channel: Any
                                   ) -> list:  # pragma: no cover - needs a gateway
    """The forum's open threads, fetched over REST rather than read from cache.

    ``GET /guilds/{id}/threads/active`` is guild-wide, so the result is filtered
    back down to this forum by ``parent_id``. Returns ``[]`` rather than raising
    when the guild cannot be resolved: a forum we cannot enumerate is a reason
    to plan nothing for it, not to abort the whole run.
    """
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        return []
    if not hasattr(guild, "active_threads"):
        guild = await client.fetch_guild(guild_id)
    return [t for t in await guild.active_threads()
            if str(getattr(t, "parent_id", "")) == str(channel.id)]


async def _read_thread(thread: Any, channel_id: str,
                       tag_names: Mapping[str, str] | None = None,
                       players: Mapping[str, str] | None = None,
                       ) -> ForumThread:  # pragma: no cover - needs a gateway
    messages = [m async for m in thread.history(limit=None, oldest_first=True)]
    starter = None
    replies: list[ForumMessage] = []
    for index, message in enumerate(messages):
        item = ForumMessage(
            id=str(message.id),
            author_id=str(message.author.id),
            author_name=str(message.author.display_name or message.author.name),
            content=resolve_mentions(message.content or "",
                                     getattr(message, "mentions", ()), players),
            created_at=message.created_at.isoformat() if message.created_at else "",
            is_starter=index == 0,
            edited=bool(getattr(message, "edited_at", None)),
            attachments=_attachments_of(message),
        )
        if index == 0:
            starter = item
        else:
            replies.append(item)
    owner_id = str(getattr(thread, "owner_id", "") or
                   (starter.author_id if starter else ""))
    return ForumThread(
        id=str(thread.id), channel_id=channel_id, title=thread.name,
        author_id=owner_id,
        author_name=starter.author_name if starter else "",
        created_at=thread.created_at.isoformat() if thread.created_at else "",
        tag_names=_applied_tag_names(thread, tag_names),
        archived=bool(getattr(thread, "archived", False)),
        locked=bool(getattr(thread, "locked", False)),
        starter=starter, messages=tuple(replies),
        url=getattr(thread, "jump_url", "") or "",
    )


def summarise_new_threads(roadmap: Snapshot, forum: ForumSnapshot, view: Any,
                          client: Any) -> dict[str, str]:
    """One summary per thread that is about to become an idea.

    Runs BEFORE planning, which is the whole point: the planners are pure, and
    a model call inside one would end that. Only unlinked threads are asked
    about, so this costs nothing on a cycle with no new reports -- which is
    most of them.

    A model that cannot answer yields no entry, and the planner then uses the
    reporter's own words. The bot never blocks on it.
    """
    if client is None:
        return {}
    out: dict[str, str] = {}
    for thread in unlinked_threads(roadmap, forum, view):
        body = thread.starter.content if thread.starter else ""
        if not (body or "").strip():
            continue
        try:
            summary = client.summarise(thread.title, body)
        except Exception as exc:      # never fail a cycle over a summary
            log.warning("no summary for thread %s: %s", thread.id, exc)
            continue
        if summary:
            out[thread.id] = summary
    return out


async def rehost_images(forum: ForumSnapshot, store: Any, *,
                        fetch: Any = None) -> ForumSnapshot:
    """Copy every Discord image somewhere permanent; return an updated snapshot.

    Runs BEFORE planning and returns plain data, which is what keeps the
    planners pure: by the time one runs, an attachment either has a
    ``rehosted_url`` or it does not, and no I/O can happen inside a plan.

    A failure on one image is not a failure of the run. The report is worth
    more than the screenshot, so a fetch or transcode error leaves that
    attachment un-rehosted -- the planner then names it as not kept -- and
    everything else carries on. The one thing never done is falling back to the
    signed Discord url, which would look like success and 404 by tomorrow.
    """
    fetch = fetch or _fetch_bytes
    cache: dict[str, str] = {}
    threads = []
    for thread in forum.threads:
        messages = []
        changed = False
        for message in thread.all_messages:
            if not any(a.is_image and not a.rehosted_url
                       for a in message.attachments):
                messages.append(message)
                continue
            done = []
            for item in message.attachments:
                if not item.is_image or item.rehosted_url or not item.url:
                    done.append(item)
                    continue
                if item.url in cache:
                    done.append(replace(item, rehosted_url=cache[item.url]))
                    changed = True
                    continue
                try:
                    raw = await fetch(item.url)
                    url, _ = attachments.store_image(raw, store)
                except Exception as exc:
                    log.warning("could not rehost %s (%s): %s",
                                attachments.safe_filename(item.filename),
                                item.id, exc)
                    done.append(item)
                    continue
                cache[item.url] = url
                done.append(replace(item, rehosted_url=url))
                changed = True
            messages.append(replace(message, attachments=tuple(done)))
        if not changed:
            threads.append(thread)
            continue
        starter = messages[0] if thread.starter is not None else None
        rest = messages[1:] if thread.starter is not None else messages
        threads.append(replace(thread, starter=starter, messages=tuple(rest)))
    return replace(forum, threads=tuple(threads))


async def _fetch_bytes(url: str) -> bytes:  # pragma: no cover - network
    """GET the attachment. No credentials: a signed CDN url carries its own."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()


@dataclass
class LiveSource(SnapshotSource):  # pragma: no cover - needs a gateway
    """The production snapshot pair: one roadmap fetch, one forum sweep."""

    roadmap_client: Any
    discord_client: Any
    channel_ids: tuple[str, ...] = ()
    bot_user_id: str = ""
    #: Discord id -> roadmap player name, so a `<@123…>` mention is rewritten to
    #: a readable name at capture time. Without it every mention reaches the
    #: roadmap as an 18-digit number.
    players: Mapping[str, str] = field(default_factory=dict)
    #: Where rehosted screenshots go. ``None`` disables rehosting: images are
    #: then reported as present-but-not-kept rather than written as a signed
    #: link that dies within the day.
    image_store: Any = None

    async def snapshots(self) -> tuple[Snapshot, ForumSnapshot]:
        roadmap = await self.roadmap_client.fetch()
        forum = await build_forum_snapshot(self.discord_client, self.channel_ids,
                                           self.bot_user_id, self.players)
        if self.image_store is not None:
            forum = await rehost_images(forum, self.image_store)
        return roadmap, forum


__all__ = [
    "DiscordForumWriter",
    "EVENT_DEBOUNCE_SECONDS",
    "EventFunnel",
    "rehost_images",
    "resolve_mentions",
    "summarise_new_threads",
    "LiveSource",
    "RECONCILE_INTERVAL_SECONDS",
    "ROADMAP_POLL_SECONDS",
    "REVIEW_SAVE_CONFLICT",
    "RunReport",
    "SnapshotSource",
    "StaticSource",
    "SyncEngine",
    "build_forum_snapshot",
    "make_client",
    "make_intents",
    "plan_all",
]
