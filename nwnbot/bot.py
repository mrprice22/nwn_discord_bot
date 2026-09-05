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
from dataclasses import dataclass, field
from typing import Any, Iterable

from nwnbot import config as cfg
from nwnbot.forum import ForumSnapshot, ForumThread, ForumMessage, ForumWriter, RecordingForumWriter
from nwnbot.roadmap import SaveConflict, Snapshot
from nwnbot.store import StoreView
from nwnbot.sync import (
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

# How often the full reconcile runs, in seconds (15 minutes, per plan.md).
RECONCILE_INTERVAL_SECONDS = 15 * 60

# PROVISIONAL WORDING / THRESHOLD — not stated in plan.md, queued for review.
# A burst of forum activity (someone posting five replies in a row) should cost
# one reconcile, not five. Events set a dirty flag and this is how long the
# worker waits for the burst to settle before planning. Small enough that the
# bot still feels immediate; large enough that a conversation is one cycle.
EVENT_DEBOUNCE_SECONDS = 5.0

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
                 dry_run: bool = True, strict_config: bool = False) -> None:
        self.source = source
        self.context = context
        self.store = store
        self.roadmap_client = roadmap_client
        self.forum_writer = forum_writer if forum_writer is not None else RecordingForumWriter()
        self.dry_run = dry_run
        #: `[b5-config]`'s startup validation. On the live path the tag mapping
        #: is checked against the editor's own `vocab` and the forums' real
        #: `available_tags` the first time a snapshot pair arrives, and drift on
        #: either side raises `ConfigError` before anything is planned. Off for
        #: fixtures and tests, whose fake worlds carry fake tag names on
        #: purpose.
        self.strict_config = strict_config
        self._config_validated = False

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
        plans = plan_all(roadmap, forum, view, self.context)
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
            thread_id = await self.forum_writer.create_thread(
                action.channel_id, action.title, action.body, action.tag_names)
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
                 reconcile_interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
        self.engine = engine
        self.debounce = debounce
        self.reconcile_interval = reconcile_interval
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

    def start(self) -> None:  # pragma: no cover - needs a running loop
        self._tasks = [asyncio.create_task(self.worker()),
                       asyncio.create_task(self.reconcile_loop())]
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
        available[str(channel_id)] = tuple(
            t.name for t in getattr(channel, "available_tags", []) or [])
        seen = list(getattr(channel, "threads", []) or [])
        async for archived in channel.archived_threads(limit=None):
            seen.append(archived)
        for thread in seen:
            threads.append(await _read_thread(thread, str(channel_id)))
    return ForumSnapshot(tuple(threads), bot_user_id=str(bot_user_id or ""),
                         available_tags=available)


async def _read_thread(thread: Any, channel_id: str
                       ) -> ForumThread:  # pragma: no cover - needs a gateway
    messages = [m async for m in thread.history(limit=None, oldest_first=True)]
    starter = None
    replies: list[ForumMessage] = []
    for index, message in enumerate(messages):
        item = ForumMessage(
            id=str(message.id),
            author_id=str(message.author.id),
            author_name=str(message.author.display_name or message.author.name),
            content=message.content or "",
            created_at=message.created_at.isoformat() if message.created_at else "",
            is_starter=index == 0,
            edited=bool(getattr(message, "edited_at", None)),
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
        tag_names=tuple(t.name for t in getattr(thread, "applied_tags", []) or []),
        archived=bool(getattr(thread, "archived", False)),
        locked=bool(getattr(thread, "locked", False)),
        starter=starter, messages=tuple(replies),
        url=getattr(thread, "jump_url", "") or "",
    )


@dataclass
class LiveSource(SnapshotSource):  # pragma: no cover - needs a gateway
    """The production snapshot pair: one roadmap fetch, one forum sweep."""

    roadmap_client: Any
    discord_client: Any
    channel_ids: tuple[str, ...] = ()
    bot_user_id: str = ""

    async def snapshots(self) -> tuple[Snapshot, ForumSnapshot]:
        roadmap = await self.roadmap_client.fetch()
        forum = await build_forum_snapshot(self.discord_client, self.channel_ids,
                                           self.bot_user_id)
        return roadmap, forum


__all__ = [
    "DiscordForumWriter",
    "EVENT_DEBOUNCE_SECONDS",
    "EventFunnel",
    "LiveSource",
    "RECONCILE_INTERVAL_SECONDS",
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
