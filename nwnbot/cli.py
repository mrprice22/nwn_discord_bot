"""Command line entry point: ``doctor``, ``plan``, ``apply``, ``backfill``, ``serve``.

Shipped by ``[b7-cli-runtime]``. Run it as ``python -m nwnbot <command>`` — the
package is not installed (review item ``[r7]``: ``pyproject.toml`` stays
pytest-config only, ``nwnbot/__main__.py`` provides the entry point, and
``systemd/nwnbot.service`` sets ``WorkingDirectory=`` and ``PYTHONPATH=``
instead of requiring ``pip install -e .`` on the server).

The five commands, and what each is allowed to touch:

``doctor``    reads. Checks the environment, the local database, the group
              vocabulary and — only with ``--check-roadmap`` — the roadmap
              login. It never contacts Discord and never contacts the roadmap
              unless explicitly asked.
``plan``      reads. Runs :func:`nwnbot.bot.plan_all` and prints the action
              list. Writes nothing, anywhere, including the local database.
``apply``     writes. Refuses unless ``--yes`` **and** ``NWNBOT_DRY_RUN=0``.
              Both, together, deliberately: one is a habit, two is a decision.
``backfill``  reads, and writes one local file. Report-only: it produces
              ``backfill-plan.md`` and stops. ``--yes`` is **refused** — the
              live batch is ``[b8-backfill]``, blocked on review item ``[r4]``.
``serve``     the long-running runtime. Honours ``NWNBOT_DRY_RUN``, which
              defaults to plan-only.

Every command that plans anything goes through :func:`nwnbot.bot.plan_all`, the
single funnel that the Discord event path and the 15-minute reconcile also use.

Offline by construction: ``--fixture PATH`` supplies both snapshots from a JSON
file, so ``python -m nwnbot plan --fixture tests/fixtures/fake_world.json``
runs the real planners against fakes with no token, no socket and no
credentials. That is the acceptance invocation, and it is what the tests use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from nwnbot import config as cfg
from nwnbot.bot import (
    RECONCILE_INTERVAL_SECONDS,
    RunReport,
    StaticSource,
    SyncEngine,
)
from nwnbot.forum import ForumMessage, ForumSnapshot, ForumThread, RecordingForumWriter
from nwnbot.roadmap import RoadmapError, Snapshot
from nwnbot.store import Store, StoreView, content_hash
from nwnbot.sync import DEFAULT_ACTION_CAP, CreateThread, PlanContext

COMMANDS = ("doctor", "plan", "apply", "backfill", "serve")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2

DEFAULT_BACKFILL_PATH = "backfill-plan.md"

# The message `apply` prints when it has not been given both keys. Operator-
# facing, so the exact wording is a choice: PROVISIONAL WORDING.
APPLY_REFUSAL = (
    "refusing to apply: this writes to the live Discord guild and the live roadmap.\n"
    "  It needs both keys turned at once:\n"
    "    * pass --yes on the command line, and\n"
    "    * set NWNBOT_DRY_RUN=0 in the environment.\n"
    "  Run `python -m nwnbot plan` first and read the action list."
)

# `backfill --yes` is not merely unset — it is blocked. PROVISIONAL WORDING.
BACKFILL_REFUSAL = (
    "refusing to run the backfill batch: it would open a forum thread per open\n"
    "  roadmap item, in one go, on a live player forum.\n"
    "  The executor is [b8-backfill], which is blocked on review item [r4]\n"
    "  (backfill approval gate) and is deliberately not implemented here.\n"
    "  What this command does today: writes the report and stops. Read it."
)


# --------------------------------------------------------------------------
# Fixtures — how `plan` runs against fakes
# --------------------------------------------------------------------------
@dataclass
class World:
    """One offline (roadmap, forum, store view, context) quadruple."""

    roadmap: Snapshot
    forum: ForumSnapshot
    view: StoreView
    context: PlanContext


def _message(raw: Mapping[str, Any]) -> ForumMessage:
    return ForumMessage(
        id=str(raw["id"]), author_id=str(raw.get("author_id") or ""),
        content=raw.get("content") or "", author_name=raw.get("author_name") or "",
        created_at=raw.get("created_at") or "",
        is_starter=bool(raw.get("is_starter")), edited=bool(raw.get("edited")))


def _thread(raw: Mapping[str, Any]) -> ForumThread:
    starter = raw.get("starter")
    return ForumThread(
        id=str(raw["id"]), channel_id=str(raw.get("channel_id") or ""),
        title=raw.get("title") or "", author_id=str(raw.get("author_id") or ""),
        author_name=raw.get("author_name") or "", created_at=raw.get("created_at") or "",
        tag_names=tuple(raw.get("tag_names") or ()),
        archived=bool(raw.get("archived")), locked=bool(raw.get("locked")),
        starter=_message(starter) if starter else None,
        messages=tuple(_message(m) for m in raw.get("messages") or ()),
        url=raw.get("url") or "")


def load_fixture(path: str | Path) -> World:
    """Load a fake world from JSON. No network, no database, no credentials.

    Shape::

        {"roadmap": <a /api/data payload>,
         "forum":   {"threads": [...], "bot_user_id": "...",
                     "available_tags": {"<channel id>": ["tag", ...]}},
         "context": {"tag_groups": {...}, "channel_types": {...},
                     "players": {...}, "bot_user_id": "...",
                     "editor_url": "...", "thread_url_template": "..."},
         "store":   {"links": {"<thread>": "<idea>"},
                     "hashes": [["<idea>", "<field>", <value>], ...],
                     "reviewed": [...], "decisions": {...}}}

    ``store.hashes`` takes plain values and hashes them here, so a fixture
    never has to contain a hash literal.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    roadmap = Snapshot.from_payload(raw.get("roadmap") or {})
    forum_raw = raw.get("forum") or {}
    forum = ForumSnapshot(
        tuple(_thread(t) for t in forum_raw.get("threads") or ()),
        bot_user_id=str(forum_raw.get("bot_user_id") or ""),
        available_tags={str(k): tuple(v)
                        for k, v in (forum_raw.get("available_tags") or {}).items()})
    ctx_raw = dict(raw.get("context") or {})
    context = PlanContext(
        tag_groups=ctx_raw.get("tag_groups") or {},
        channel_types=ctx_raw.get("channel_types") or {},
        players=ctx_raw.get("players") or {},
        bot_user_id=str(ctx_raw.get("bot_user_id") or forum.bot_user_id or ""),
        action_cap=int(ctx_raw.get("action_cap", DEFAULT_ACTION_CAP)),
        editor_url=ctx_raw.get("editor_url") or "",
        thread_url_template=ctx_raw.get("thread_url_template") or "")
    store_raw = raw.get("store") or {}
    view = StoreView(
        links={str(k): str(v) for k, v in (store_raw.get("links") or {}).items()},
        hashes={(str(i), str(f)): content_hash(v)
                for i, f, v in store_raw.get("hashes") or ()},
        reviewed=frozenset(store_raw.get("reviewed") or ()),
        decisions=dict(store_raw.get("decisions") or {}))
    return World(roadmap, forum, view, context)


def load_tag_map(path: str | Path | None) -> dict[str, str]:
    """The forum-tag-name -> group-id mapping in force for this run.

    ``--tag-map PATH`` wins; otherwise the built-in :data:`nwnbot.config.
    TAG_GROUPS`, which is ``tag-map.json`` folded into the code by
    ``[b5-config]``. The override exists so a renamed forum tag can be fixed
    without a release, not so the mapping can be invented.
    """
    return cfg.load_tag_groups(path)


def tag_map_for(args: argparse.Namespace,
                world: World | None) -> tuple[dict[str, str], str]:
    """The mapping and where it came from, in precedence order.

    ``--tag-map`` beats a fixture's own ``context.tag_groups`` beats the
    built-in map. The fixture rung matters: a fake world describes a fake forum
    with fake tag names, and checking the real 12 against it would be a
    meaningless failure.
    """
    path = getattr(args, "tag_map", None)
    if path:
        return cfg.load_tag_groups(path), str(path)
    if world is not None and world.context.tag_groups:
        return dict(world.context.tag_groups), f"{args.fixture} (fixture)"
    return dict(cfg.TAG_GROUPS), f"built in ({cfg.TAG_MAP_PATH})"


# --------------------------------------------------------------------------
# Store access
# --------------------------------------------------------------------------
def read_only_view(db_path: str | None) -> StoreView:
    """A view of the store that never brings the store into existence.

    ``plan`` must write nothing at all, and creating a database file counts.
    """
    if not db_path or not Path(db_path).exists():
        return StoreView.empty()
    with Store(db_path) as store:
        return store.view()


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str          # "ok" | "warn" | "fail" | "skip"
    detail: str = ""

    def line(self) -> str:
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skip": "--  "}[self.status]
        return f"[{mark}] {self.name}: {self.detail}"


REQUIRED_ENV = (
    cfg.ENV_DISCORD_BOT_TOKEN,
    cfg.ENV_DISCORD_GUILD_ID,
    # Loop-prevention layer one: messages authored by this id are skipped. It
    # is required, not optional — unset, it defaults to "" and the layer
    # quietly does nothing, which is the worst of the three outcomes.
    cfg.ENV_DISCORD_BOT_USER_ID,
    cfg.ENV_DISCORD_BUGS_FORUM_ID,
    cfg.ENV_DISCORD_FEATURES_FORUM_ID,
    cfg.ENV_ROADMAP_BASE_URL,
    cfg.ENV_ROADMAP_USER,
    cfg.ENV_ROADMAP_PASSWORD,
)

#: Env vars whose value must never be printed, even to the operator's terminal.
SECRET_ENV = frozenset({cfg.ENV_DISCORD_BOT_TOKEN, cfg.ENV_ROADMAP_PASSWORD})


def check_env(env: Mapping[str, str]) -> list[Check]:
    missing = [name for name in REQUIRED_ENV if not (env.get(name) or "").strip()]
    present = [name for name in REQUIRED_ENV if name not in missing]
    checks = [Check("env", "fail" if missing else "ok",
                    f"missing: {', '.join(missing)}" if missing
                    else f"all {len(present)} variables set "
                         f"(secrets not printed: {', '.join(sorted(SECRET_ENV))})")]
    dry = (env.get(cfg.ENV_NWNBOT_DRY_RUN) or "1").strip()
    checks.append(Check("dry-run", "ok" if dry != "0" else "warn",
                        f"{cfg.ENV_NWNBOT_DRY_RUN}={dry}"
                        + ("" if dry != "0" else " — writes are ARMED")))
    return checks


def check_store(db_path: str | None) -> Check:
    if not db_path:
        return Check("store", "warn", "no database path (set NWNBOT_DB or pass --db)")
    try:
        with Store(db_path) as store:
            view = store.view()
    except Exception as exc:
        return Check("store", "fail", f"{db_path}: {exc!r}")
    return Check("store", "ok",
                 f"{db_path}: {len(view.links)} thread link(s), "
                 f"{len(view.hashes)} content hash(es), "
                 f"{len(view.reviewed)} review entr(ies)")


def check_groups(snapshot: Snapshot | None) -> Check:
    if snapshot is None:
        return Check("groups", "skip",
                     "no snapshot — pass --fixture, or --check-roadmap to fetch one")
    problems = cfg.group_problems(snapshot.vocab.get("groups") or ())
    if problems:
        return Check("groups", "fail", "; ".join(problems))
    return Check("groups", "ok", f"all {len(cfg.GROUP_IDS)} group ids present in vocab")


def check_tag_map(mapping: Mapping[str, str] | None,
                  forum: ForumSnapshot | None,
                  source: str = "") -> Check:
    """``[b5-config]``'s acceptance: 12 tags to 12 groups, or a loud failure.

    The rules themselves live in :func:`nwnbot.config.tag_map_problems`, which
    is also what the live path validates with — one implementation, two
    presentations, so ``doctor`` and ``serve`` cannot disagree about what a
    valid mapping is.
    """
    if mapping is None:
        return Check("tag-map", "fail", "no mapping supplied")
    where = f" [{source}]" if source else ""
    problems = cfg.tag_map_problems(
        mapping,
        available_tags=(forum.available_tags if forum is not None else None))
    if problems:
        return Check("tag-map", "fail", "; ".join(problems) + where)
    return Check("tag-map", "ok",
                 f"{len(mapping)} tag(s) mapped one-to-one onto all "
                 f"{len(cfg.GROUP_IDS)} groups{where}")


def check_players(path: str | Path | None) -> Check:
    """The player identity map: how many ids are resolvable, and how many are not.

    Never a hard failure — an unresolved author is a review item at plan time,
    not a reason to refuse to start.
    """
    target = Path(path or cfg.DEFAULT_PLAYERS_PATH)
    if not target.exists():
        return Check("players", "warn",
                     f"{target} does not exist — every Discord author will be "
                     f"queued for review. Seed it with "
                     f"`doctor --fixture … --seed-players {target}`.")
    try:
        players = cfg.PlayerMap.load(target)
    except cfg.ConfigError as exc:
        return Check("players", "fail", str(exc))
    pending = len(players.unresolved_candidates)
    status = "ok" if players.ids else "warn"
    return Check("players", status,
                 f"{target}: {len(players.ids)} discord id(s) mapped, "
                 f"{pending} roster name(s) still unmatched "
                 f"(a display name is never matched automatically)")


async def _fetch_snapshot(env: Mapping[str, str]) -> Snapshot:  # pragma: no cover
    """Only ever called when the operator passed ``--check-roadmap``."""
    from nwnbot.roadmap import RoadmapClient

    async with RoadmapClient.from_env(env) as client:
        await client.login()
        return await client.fetch()


def _seed_players(snapshot: Snapshot | None, path: str) -> Check:
    """Write a `players.json` skeleton from the roadmap's own ``players:`` list.

    Be clear about what this can do: the roster holds player names and, in
    eight of nineteen cases, a parenthetical that *looks* like a Discord
    display name (and in one case is a role, "Server Admin"). A Discord user id
    is a snowflake and appears nowhere in ``roadmap.yaml``, so seeding produces
    an **empty** ``discord_ids`` map plus a candidate list for a human to
    resolve. It saves typing, not identification. Existing ids are preserved.
    """
    if snapshot is None:
        return Check("seed-players", "fail",
                     "--seed-players needs a roadmap snapshot: pass --fixture "
                     "or --check-roadmap")
    roster = sorted(snapshot.vocab.get("players") or ())
    if not roster:
        return Check("seed-players", "fail", "the snapshot's vocab has no players")
    doc = cfg.write_players_seed(path, roster)
    with_alias = sum(1 for c in doc["candidates"] if c["alias"])
    return Check("seed-players", "ok",
                 f"wrote {path}: {len(doc['discord_ids'])} discord id(s) kept, "
                 f"{len(roster)} roster name(s) listed as candidates "
                 f"({with_alias} carry a parenthetical, {len(roster) - with_alias} "
                 f"do not). No id was guessed; fill discord_ids by hand.")


def cmd_doctor(args: argparse.Namespace, env: Mapping[str, str],
               out: Any) -> int:
    world = load_fixture(args.fixture) if args.fixture else None
    snapshot = world.roadmap if world else None
    forum = world.forum if world else None

    checks = check_env(env)

    if args.check_roadmap:  # pragma: no cover - deliberately not exercised offline
        try:
            snapshot = asyncio.run(_fetch_snapshot(env))
            checks.append(Check("roadmap", "ok",
                                f"login + fetch ok, {len(snapshot.ideas)} idea(s), "
                                f"version {snapshot.version}"))
        except (RoadmapError, OSError) as exc:
            checks.append(Check("roadmap", "fail", repr(exc)))
    else:
        checks.append(Check("roadmap", "skip",
                            "not contacted — pass --check-roadmap to log in and fetch"))

    checks.append(Check("discord", "skip",
                        "not contacted — `doctor` never opens a gateway; "
                        "`serve` is where the guild is touched"))
    checks.append(check_store(args.db))
    checks.append(check_groups(snapshot))
    mapping, source = tag_map_for(args, world)
    checks.append(check_tag_map(mapping, forum, source))
    checks.append(check_players(getattr(args, "players", None)))

    seed_to = getattr(args, "seed_players", None)
    if seed_to:
        checks.append(_seed_players(snapshot, seed_to))

    for check in checks:
        print(check.line(), file=out)
    failed = [c for c in checks if c.status == "fail"]
    warned = [c for c in checks if c.status == "warn"]
    print(f"\n{len(checks)} check(s): {len(failed)} failed, {len(warned)} warning(s)",
          file=out)
    return EXIT_FAIL if failed else EXIT_OK


# --------------------------------------------------------------------------
# Engine construction
# --------------------------------------------------------------------------
def engine_for_world(world: World, *, store: Any = None,
                     roadmap_client: Any = None, dry_run: bool = True,
                     forum_writer: Any = None, cap: int | None = None) -> SyncEngine:
    context = world.context
    if cap is not None:
        context = PlanContext(
            tag_groups=context.tag_groups, channel_types=context.channel_types,
            players=context.players, bot_user_id=context.bot_user_id,
            action_cap=cap, editor_url=context.editor_url,
            thread_url_template=context.thread_url_template)
    return SyncEngine(
        StaticSource(world.roadmap, world.forum), context,
        store=store if store is not None else world.view,
        roadmap_client=roadmap_client,
        forum_writer=forum_writer if forum_writer is not None else RecordingForumWriter(),
        dry_run=dry_run)


def print_report(report: RunReport, out: Any, *, show_actions: bool = True) -> None:
    for plan in report.plans:
        print(f"\n== {plan.direction} ==", file=out)
        if not plan.actions:
            print("  (nothing to do)", file=out)
        elif show_actions:
            for action in plan.actions:
                print(f"  - {action.describe()}", file=out)
    print("", file=out)
    for line in report.summary():
        print(line, file=out)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def cmd_plan(args: argparse.Namespace, env: Mapping[str, str], out: Any) -> int:
    if not args.fixture:  # pragma: no cover - the admin's live invocation
        return _live(args, env, out, dry_run=True)
    world = load_fixture(args.fixture)
    view = world.view if args.db is None else read_only_view(args.db)
    engine = engine_for_world(world, store=view, dry_run=True, cap=args.cap)
    report = asyncio.run(engine.cycle(reason="plan"))
    print_report(report, out)
    print("plan is read-only: no Discord call, no roadmap call, no database write.",
          file=out)
    return EXIT_FAIL if report.aborted else EXIT_OK


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def apply_allowed(args: argparse.Namespace, env: Mapping[str, str]) -> bool:
    """Both keys, together. Either one alone is not consent."""
    return bool(getattr(args, "yes", False)) and \
        (env.get(cfg.ENV_NWNBOT_DRY_RUN) or "1").strip() == "0"


def cmd_apply(args: argparse.Namespace, env: Mapping[str, str], out: Any) -> int:
    if not apply_allowed(args, env):
        print(APPLY_REFUSAL, file=out)
        have_yes = bool(getattr(args, "yes", False))
        have_env = (env.get(cfg.ENV_NWNBOT_DRY_RUN) or "1").strip() == "0"
        print(f"  (--yes: {'given' if have_yes else 'missing'}; "
              f"{cfg.ENV_NWNBOT_DRY_RUN}=0: "
              f"{'set' if have_env else 'not set'})", file=out)
        return EXIT_REFUSED

    if not args.fixture:  # pragma: no cover - the admin's live invocation
        return _live(args, env, out, dry_run=False)

    world = load_fixture(args.fixture)
    store = Store(args.db) if args.db else None
    try:
        engine = engine_for_world(world, store=store, dry_run=False,
                                  roadmap_client=args.roadmap_client,
                                  forum_writer=args.forum_writer, cap=args.cap)
        report = asyncio.run(engine.cycle(reason="apply"))
    finally:
        if store is not None:
            store.close()
    print_report(report, out)
    return EXIT_OK if report.ok else EXIT_FAIL


# --------------------------------------------------------------------------
# backfill — report only; the batch is [b8-backfill], blocked on [r4]
# --------------------------------------------------------------------------
def render_backfill(plan_actions: Sequence[CreateThread], *,
                    context: PlanContext) -> str:
    """The report, grouped by tag, with a total. Local file, never committed."""
    by_tag: dict[str, list[CreateThread]] = {}
    for action in plan_actions:
        tag = ", ".join(action.tag_names) or "(untagged)"
        by_tag.setdefault(tag, []).append(action)
    lines = [
        "# Backfill plan — editor to Discord",
        "",
        "Generated by `python -m nwnbot backfill`. **Nothing has been created.**",
        "",
        "Running the batch is `[b8-backfill]`, which is blocked on review item",
        "`[r4]` (backfill approval gate): a live run would open one forum thread",
        "per item below, on a player-facing forum, with no undo. `backfill --yes`",
        "is refused by this build.",
        "",
        f"**Total: {len(plan_actions)} thread(s) would be created.**",
        "",
    ]
    for tag in sorted(by_tag):
        actions = by_tag[tag]
        lines.append(f"## {tag} — {len(actions)}")
        lines.append("")
        for action in actions:
            url = context.idea_url(action.idea_id)
            lines.append(f"- `{action.idea_id}` — {action.title}"
                         + (f" ({url})" if url else ""))
        lines.append("")
    return "\n".join(lines)


def cmd_backfill(args: argparse.Namespace, env: Mapping[str, str], out: Any) -> int:
    if getattr(args, "yes", False):
        print(BACKFILL_REFUSAL, file=out)
        return EXIT_REFUSED
    if not args.fixture:  # pragma: no cover - the admin's live invocation
        return _live(args, env, out, dry_run=True, backfill_to=args.out)

    world = load_fixture(args.fixture)
    view = world.view if args.db is None else read_only_view(args.db)
    engine = engine_for_world(world, store=view, dry_run=True, cap=args.cap)
    report = asyncio.run(engine.cycle(reason="backfill"))
    return _write_backfill(report, world.context, args.out, out)


def _write_backfill(report: RunReport, context: PlanContext, path: str,
                    out: Any) -> int:
    creates = [a for a in report.actions if isinstance(a, CreateThread)]
    Path(path).write_text(render_backfill(creates, context=context), encoding="utf-8")
    print(f"wrote {path}: {len(creates)} thread(s) would be created.", file=out)
    print("Nothing was created. Read the report; the live batch is [b8]/[r4].",
          file=out)
    return EXIT_OK


# --------------------------------------------------------------------------
# serve, and the live path — never exercised by the test suite
# --------------------------------------------------------------------------
def _channel_types(env: Mapping[str, str]) -> dict[str, str]:
    """Forum channel id -> item type: bugs => Defect, features => Enhancement.

    The ids themselves only ever come from the environment.
    """
    return cfg.channel_types(env)


def _live_context(args: argparse.Namespace,
                  env: Mapping[str, str]) -> PlanContext:  # pragma: no cover
    mapping = load_tag_map(getattr(args, "tag_map", None))
    # Startup validation, structural half: the mapping against the 12 known
    # group ids. The other half — the mapping against the forums' real
    # available_tags, and against the editor's own vocab — needs live snapshots
    # and runs in `validate_against_world` on the first cycle.
    cfg.validate_tag_map(mapping)
    settings = cfg.Settings.from_env(env)
    players = cfg.PlayerMap.load(getattr(args, "players", None)
                                 or settings.players_path)
    return PlanContext(
        tag_groups=mapping, channel_types=settings.channel_types(),
        players=dict(players.ids),
        bot_user_id=settings.discord_bot_user_id,
        action_cap=args.cap if args.cap is not None else DEFAULT_ACTION_CAP,
        editor_url=(env.get(cfg.ENV_ROADMAP_BASE_URL) or "").rstrip("/"),
        thread_url_template=("https://discord.com/channels/"
                             f"{env.get(cfg.ENV_DISCORD_GUILD_ID, '')}/{{thread_id}}"))


def _live(args: argparse.Namespace, env: Mapping[str, str], out: Any, *,
          dry_run: bool, backfill_to: str | None = None) -> int:  # pragma: no cover
    """One-shot live run. The admin's to invoke; autopilot never does.

    Deliberately HTTP-only: it logs in to Discord but does **not** open a
    gateway, so nothing arrives while it works and a `plan` cannot become a
    long-lived listener by accident. ``serve`` is where the gateway lives.
    """
    from nwnbot.bot import DiscordForumWriter, LiveSource
    from nwnbot.roadmap import RoadmapClient

    context = _live_context(args, env)
    token = env.get(cfg.ENV_DISCORD_BOT_TOKEN) or ""
    if not token:
        raise SystemExit(f"{cfg.ENV_DISCORD_BOT_TOKEN} is not set")

    async def go() -> RunReport:
        import discord

        from nwnbot.bot import make_intents

        store = Store(args.db) if (args.db and not dry_run) else None
        view = store if store is not None else read_only_view(args.db)
        async with RoadmapClient.from_env(env) as roadmap_client:
            await roadmap_client.login()
            client = discord.Client(intents=make_intents())
            await client.login(token)
            try:
                source = LiveSource(roadmap_client, client,
                                    tuple(_channel_types(env)),
                                    context.bot_user_id)
                engine = SyncEngine(
                    source, context, store=view, roadmap_client=roadmap_client,
                    forum_writer=(DiscordForumWriter(client) if not dry_run
                                  else RecordingForumWriter()),
                    dry_run=dry_run, strict_config=True)
                return await engine.cycle(reason="cli")
            finally:
                await client.close()
                if store is not None:
                    store.close()

    report = asyncio.run(go())
    if backfill_to:
        return _write_backfill(report, context, backfill_to, out)
    print_report(report, out)
    return EXIT_OK if report.ok else EXIT_FAIL


def cmd_serve(args: argparse.Namespace, env: Mapping[str, str],
              out: Any) -> int:  # pragma: no cover - the runtime
    from nwnbot.bot import DiscordForumWriter, LiveSource, make_client
    from nwnbot.roadmap import RoadmapClient

    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    dry_run = (env.get(cfg.ENV_NWNBOT_DRY_RUN) or "1").strip() != "0"
    context = _live_context(args, env)
    token = env.get(cfg.ENV_DISCORD_BOT_TOKEN) or ""
    if not token:
        raise SystemExit(f"{cfg.ENV_DISCORD_BOT_TOKEN} is not set")
    print(f"serve: dry_run={dry_run}, reconcile every "
          f"{RECONCILE_INTERVAL_SECONDS}s", file=out)

    async def go() -> None:
        store = Store(args.db) if args.db else None
        async with RoadmapClient.from_env(env) as roadmap_client:
            await roadmap_client.login()
            source = LiveSource(roadmap_client, None, tuple(_channel_types(env)),
                                context.bot_user_id)
            engine = SyncEngine(source, context, store=store,
                                roadmap_client=roadmap_client, dry_run=dry_run,
                                strict_config=True)
            client = make_client(engine)
            source.discord_client = client
            engine.forum_writer = (DiscordForumWriter(client) if not dry_run
                                   else RecordingForumWriter())
            try:
                await client.start(token)
            finally:
                if store is not None:
                    store.close()

    asyncio.run(go())
    return EXIT_OK


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nwnbot",
        description="Sync the Discord forums and the roadmap editor.")
    subs = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--fixture", metavar="PATH",
                         help="run offline against a JSON fake world "
                              "(no token, no socket, no credentials)")
        sub.add_argument("--db", metavar="PATH", default=None,
                         help="local state database (default: $NWNBOT_DB)")
        sub.add_argument("--tag-map", metavar="PATH", default=None,
                         help="JSON forum-tag-name -> roadmap group-id mapping; "
                              f"overrides the built-in map ({cfg.TAG_MAP_PATH})")
        sub.add_argument("--players", metavar="PATH", default=None,
                         help="player identity map "
                              f"(default {cfg.DEFAULT_PLAYERS_PATH}); an author "
                              "with no entry is queued for review, never guessed")
        sub.add_argument("--cap", type=int, default=None, metavar="N",
                         help=f"per-run action cap (default {DEFAULT_ACTION_CAP}); "
                              "over it, the run aborts and reports")

    doctor = subs.add_parser("doctor", help="check the environment and configuration")
    common(doctor)
    doctor.add_argument("--check-roadmap", action="store_true",
                        help="also log in to the roadmap and fetch (off by default: "
                             "doctor contacts nothing unless asked)")
    doctor.add_argument("--seed-players", metavar="PATH", default=None,
                        help="write a players.json skeleton from the snapshot's "
                             "players: roster. Fills in NO discord ids — they are "
                             "not derivable from the roadmap — and never "
                             "overwrites ids already there")

    plan = subs.add_parser("plan", help="print the action list; write nothing")
    common(plan)

    apply_ = subs.add_parser("apply", help="execute the action list (needs --yes)")
    common(apply_)
    apply_.add_argument("--yes", action="store_true",
                        help="confirm live writes; also needs NWNBOT_DRY_RUN=0")

    backfill = subs.add_parser(
        "backfill", help="write backfill-plan.md and stop (report only)")
    common(backfill)
    backfill.add_argument("--out", metavar="PATH", default=DEFAULT_BACKFILL_PATH,
                          help=f"report path (default {DEFAULT_BACKFILL_PATH})")
    backfill.add_argument("--yes", action="store_true",
                          help="refused: the live batch is [b8-backfill], "
                               "blocked on review item [r4]")

    serve = subs.add_parser("serve", help="run the long-lived Discord runtime")
    common(serve)

    return parser


HANDLERS = {
    "doctor": cmd_doctor,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "backfill": cmd_backfill,
    "serve": cmd_serve,
}


def main(argv: Sequence[str] | None = None, *,
         env: Mapping[str, str] | None = None, out: Any = None) -> int:
    """Parse and dispatch. Returns the exit code; never calls ``sys.exit``.

    ``env`` is injected so a test can prove that ``apply`` refuses without
    ``NWNBOT_DRY_RUN=0`` without touching the real environment — and so nothing
    here ever loads ``.env``, which holds real credentials. The environment
    arrives from the shell or from the systemd unit's ``EnvironmentFile=``.
    """
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.db is None and args.command != "plan" and args.command != "backfill":
        args.db = env.get(cfg.ENV_NWNBOT_DB) or cfg.DEFAULT_DB_PATH
    if getattr(args, "players", None) is None:
        args.players = env.get(cfg.ENV_NWNBOT_PLAYERS) or cfg.DEFAULT_PLAYERS_PATH
    # Injection points for the offline `apply` tests; never set from the CLI.
    args.roadmap_client = getattr(args, "roadmap_client", None)
    args.forum_writer = getattr(args, "forum_writer", None)
    return HANDLERS[args.command](args, env, out)


__all__ = [
    "APPLY_REFUSAL",
    "BACKFILL_REFUSAL",
    "COMMANDS",
    "Check",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "World",
    "apply_allowed",
    "build_parser",
    "check_env",
    "check_groups",
    "check_store",
    "check_players",
    "check_tag_map",
    "load_fixture",
    "load_tag_map",
    "tag_map_for",
    "main",
    "read_only_view",
    "render_backfill",
]
