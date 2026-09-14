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
              live batch is ``[b8-backfill]``: it needs --yes, NWNBOT_DRY_RUN=0
              and a --cap naming the thread count.
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
import logging
import os
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger("nwnbot")
from typing import Any, Mapping, Sequence

from nwnbot import config as cfg
from nwnbot import dupes
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

COMMANDS = ("doctor", "plan", "apply", "backfill", "serve", "review", "dupes")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2

DEFAULT_BACKFILL_PATH = "backfill-plan.md"

# The message `apply` prints when it has not been given both keys. Operator-
# facing only, no blast radius; approved as written by review item [r12].2.
APPLY_REFUSAL = (
    "refusing to apply: this writes to the live Discord guild and the live roadmap.\n"
    "  It needs both keys turned at once:\n"
    "    * pass --yes on the command line, and\n"
    "    * set NWNBOT_DRY_RUN=0 in the environment.\n"
    "  Run `python -m nwnbot plan` first and read the action list."
)

# `backfill --yes` is not merely unset — it is blocked. Approved as written by
# review item [r12].2.
BACKFILL_REFUSAL = (
    "refusing to run the backfill batch: it opens a forum thread per eligible\n"
    "  roadmap item, in one go, on a live player forum.\n"
    "  It needs --yes AND NWNBOT_DRY_RUN=0 AND --cap at least the planned count,\n"
    "  so the number of threads is something you typed, not something you\n"
    "  inherited. Run it without --yes first and read backfill-plan.md."
)

#: Printed when the batch is armed but the cap is below the planned count. The
#: cap is the confirmation: [r4] settled that the live run is always the
#: admin's, and naming the number is how they take it.
BACKFILL_CAP_REFUSAL = (
    "refusing: {n} thread(s) planned but --cap is {cap}.\n"
    "  Read {path} first, then re-run with --cap {n} to confirm that number."
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
        thread_url_template=ctx_raw.get("thread_url_template") or "",
        # [b9-dupes]. Absent from a fixture, these stay 0.0 and the scorer never
        # runs, so every fixture written before b9 plans exactly what it did.
        dupe_low=float(ctx_raw.get("dupe_low", 0.0)),
        dupe_high=float(ctx_raw.get("dupe_high", 0.0)),
        dupe_title_weight=float(ctx_raw.get("dupe_title_weight",
                                            cfg.DUPE_TITLE_WEIGHT)),
        dupe_notes_max=int(ctx_raw.get("dupe_notes_max", cfg.DUPE_NOTES_MAX_CHARS)),
        dupe_candidates=int(ctx_raw.get("dupe_candidates", cfg.DUPE_CANDIDATE_LIMIT)),
        dupe_post_in_thread=bool(ctx_raw.get("dupe_post_in_thread",
                                             cfg.DUPE_POST_IN_THREAD)),
        # [b8]. Absent from a fixture this is empty, meaning "no staff", so every
        # fixture written before the policy plans exactly what it did before.
        staff_players=frozenset(ctx_raw.get("staff_players") or ()),
        staff_thread_statuses=frozenset(ctx_raw.get("staff_thread_statuses")
                                        or cfg.STAFF_THREAD_STATUSES))
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
    # [b9-dupes]. A band that is empty or inverted silently changes which of the
    # three outcomes every new thread gets, so it must not pass a health check.
    try:
        settings = cfg.Settings.from_env(env)
    except cfg.ConfigError as exc:
        checks.append(Check("dupes", "fail", str(exc)))
    else:
        gate = "on" if cfg.DUPE_POST_IN_THREAD else "off"
        checks.append(Check("dupes", "ok",
                            f"low={settings.dupe_low} high={settings.dupe_high}, "
                            f"posting in threads is {gate}; the bot never writes "
                            f"dupe_of ([r6])"))
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


def check_images(env: Mapping[str, str]) -> Check:
    """Whether Discord screenshots will be kept, and loudly if they will not.

    A warning rather than a failure: the bot is useful without rehosting, and a
    report is worth more than its screenshot. But it must SAY so, because the
    alternative to rehosting is not "store the Discord link" — that link is
    signed and dies within a day — it is "the image only exists in Discord".
    """
    from nwnbot import r2

    try:
        store = r2.from_env(env)
    except cfg.ConfigError as exc:
        return Check("images", "fail", str(exc))
    if store is None:
        return Check("images", "warn",
                     "R2 is not configured — Discord screenshots will NOT be "
                     "kept, and will be recorded as present-but-not-kept. Set "
                     f"{cfg.ENV_R2_ACCOUNT_ID} and the other R2_* variables "
                     "to turn rehosting on.")
    return Check("images", "ok",
                 f"rehosting to {store.bucket!r}, served from "
                 f"{store.public_base_url}")


def check_llm(env: Mapping[str, str]) -> Check:
    """Whether the duplicate judge will answer, and which model answered.

    A warning, never a failure: the bot is useful without it and falls back to
    the token scorer. But it names the model, because a swapped or moved model
    is otherwise a silent change in how duplicates are judged -- and it is the
    difference between a one-line fix and a debugging session.
    """
    from nwnbot import llm as _llm

    client = _llm.from_env(env)
    if client is None:
        return Check("llm", "warn",
                     f"no {cfg.ENV_LLM_BASE_URL} — duplicate judging falls back "
                     f"to the token scorer alone")
    try:
        models = client.models()
    except _llm.LlmUnavailable as exc:
        return Check("llm", "warn",
                     f"{client.base_url} did not answer ({exc}); the token "
                     f"scorer will be used alone. Start it with the "
                     f"'Bots - start' shortcut.")
    if not models:
        return Check("llm", "warn", f"{client.base_url} is up but serving no model")
    pinned = client.model
    if pinned and pinned not in models:
        return Check("llm", "fail",
                     f"{cfg.ENV_LLM_MODEL} is {pinned!r} but the server is "
                     f"serving {models}. Judging would silently use a different "
                     f"model than the one recorded on every suggestion.")
    return Check("llm", "ok", f"{client.base_url} answering as {models[0]}")


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
    checks.append(check_images(env))
    checks.append(check_llm(env))

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
        # `replace`, not a field-by-field rebuild: the rebuild silently dropped
        # every field added to PlanContext after it was written, which is how
        # [b9-dupes]'s thresholds would have arrived at the planner as zero.
        context = replace(context, action_cap=cap)
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
# review — read the queue, and resolve an entry
#
# The review queue is where every judgement call the bot refuses to make ends
# up, and until now nothing could read it outside a `plan` summary. It matters
# most for [b9-dupes]: **resolving a `possible_dupe` entry is how you reject a
# duplicate suggestion.** `Store.view()` loads reviews of every status, so
# `_review` never re-raises one that has been resolved — the rejection is
# permanent and needs no separate "no" to be recorded anywhere.
# --------------------------------------------------------------------------
def cmd_review(args: argparse.Namespace, env: Mapping[str, str], out: Any) -> int:
    path = args.db or env.get(cfg.ENV_NWNBOT_DB) or cfg.DEFAULT_DB_PATH
    if not Path(path).exists():
        print(f"no state database at {path}: nothing has been planned yet.", file=out)
        return EXIT_OK
    with Store(path) as store:
        if args.resolve:
            known = {e.key for e in store.reviews(None)}
            missing = [k for k in args.resolve if k not in known]
            if missing:
                # Refuse the whole batch: a typo'd key would otherwise be
                # reported as resolved and the entry would keep coming back.
                for key in missing:
                    print(f"no review entry with key {key!r}", file=out)
                return EXIT_FAIL
            for key in args.resolve:
                store.resolve_review(key)
                print(f"resolved {key}", file=out)
            return EXIT_OK

        entries = store.reviews(None if args.all else "open")
        if not entries:
            print("no open review entries." if not args.all
                  else "the review queue is empty.", file=out)
            return EXIT_OK
        for entry in entries:
            mark = " " if entry.status == "open" else "x"
            print(f"[{mark}] {entry.kind}  {entry.key}", file=out)
            if entry.detail:
                print(f"      {entry.detail}", file=out)
        print(f"\n{len(entries)} entr(ies). "
              f"Resolve one with: python -m nwnbot review --resolve <key>", file=out)
    return EXIT_OK


# --------------------------------------------------------------------------
# dupes --calibrate
#
# The two thresholds in config.py are the numbers review item [r6] proposed,
# not numbers anyone measured. This scores every existing idea against every
# other and prints the ranked pairs, so they can be re-picked from real data
# before `serve` is ever armed (review item [r15]).
#
# Read-only: no store write, no Discord call. With --fixture it does not even
# reach the roadmap.
# --------------------------------------------------------------------------
def cmd_dupes(args: argparse.Namespace, env: Mapping[str, str], out: Any) -> int:
    if not args.calibrate:
        print("nothing to do: pass --calibrate.", file=out)
        return EXIT_OK
    if args.roadmap_yaml:
        # The honest way to calibrate: roadmap.yaml on disk is the same data the
        # API would return, and reading it needs no account, no tunnel and no
        # network. Nothing is written back — this file is the admin's, and the
        # bot never hand-edits it.
        import yaml

        with open(args.roadmap_yaml, encoding="utf-8") as handle:
            ideas = (yaml.safe_load(handle) or {}).get("ideas") or []
    elif args.fixture:
        ideas = load_fixture(args.fixture).roadmap.ideas
    else:  # pragma: no cover - the admin's live invocation
        ideas = asyncio.run(_fetch_snapshot(env)).ideas

    prepared = dupes.prepare(ideas, notes_max=cfg.DUPE_NOTES_MAX_CHARS)
    print(f"scoring {len(prepared)} non-dupe ideas "
          f"({len(prepared) * (len(prepared) - 1) // 2} pairs)", file=out)

    pairs: list[tuple[float, str, str, str, str]] = []
    siblings = 0
    for i, left in enumerate(prepared):
        for right in prepared[i + 1:]:
            if dupes.are_siblings(left, right):
                # Declared related work, not the same idea twice. Skipped
                # outright rather than scored and shown: a suggestion a human
                # has already answered structurally is noise, and this is the
                # sweep that fills the /dupes queue.
                siblings += 1
                continue
            drop = (dupes.STOPWORDS | dupes.group_words(left.group)
                    | dupes.group_words(right.group))
            value = dupes.score(left.title, left.body, right.title, right.body,
                                drop=drop, title_weight=cfg.DUPE_TITLE_WEIGHT)
            if value >= args.floor:
                pairs.append((value, left.idea_id, right.idea_id,
                              left.title, right.title))
    pairs.sort(key=lambda row: (-row[0], row[1], row[2]))
    if siblings:
        print(f"skipped {siblings} pair(s) linked by depends_on — related work, "
              f"never the same idea twice", file=out)

    low, high = cfg.DUPE_LOW_THRESHOLD, cfg.DUPE_HIGH_THRESHOLD
    for value, a_id, b_id, a_title, b_title in pairs[:args.top]:
        band = "HIGH" if value >= high else ("low " if value >= low else "    ")
        print(f"{value:.3f} {band}  {a_id}  |  {b_id}", file=out)
        print(f"              {a_title}", file=out)
        print(f"              {b_title}", file=out)
    above_high = sum(1 for row in pairs if row[0] >= high)
    in_band = sum(1 for row in pairs if low <= row[0] < high)
    gate = "on" if cfg.DUPE_POST_IN_THREAD else "OFF"
    print(f"\nwith the shipped thresholds (low={low}, high={high}): "
          f"{in_band} pair(s) would file a review entry, {above_high} would also "
          f"reach the high band — where posting in the thread is {gate} "
          f"(cfg.DUPE_POST_IN_THREAD).", file=out)
    print("These numbers were picked from a run like this one against the real "
          "roadmap; see cfg.DUPE_POST_IN_THREAD for what it measured. Re-run it "
          "and re-check them before arming `serve` — that is [r15].", file=out)
    return EXIT_OK


def cmd_link(args: argparse.Namespace, env: Mapping[str, str],
             out: Any) -> int:  # pragma: no cover - needs a gateway
    """Propose which roadmap idea each existing Discord thread already is.

    A migration, run once and then occasionally: neither side knew about the
    other before the bot existed, so not one idea carries a `discord` link. Any
    thread that already has an idea would otherwise be duplicated in BOTH
    directions -- a backfill opening a second thread, and an inbound sync
    minting a second idea.

    This writes proposals and links nothing. A wrong link would post one
    player's status updates and merit announcements into another player's
    thread, so the confirming is the admin's, in the editor.
    """
    import asyncio as _asyncio
    import json as _json

    from nwnbot import linking, llm as _llm
    from nwnbot.bot import build_forum_snapshot, make_intents
    from nwnbot.roadmap import RoadmapClient

    token = env.get(cfg.ENV_DISCORD_BOT_TOKEN) or ""
    if not token:
        raise SystemExit(f"{cfg.ENV_DISCORD_BOT_TOKEN} is not set")
    judge = None if args.no_llm else _llm.from_env(env)

    async def go():
        import discord

        client = discord.Client(intents=make_intents())
        await client.login(token)
        try:
            forum = await build_forum_snapshot(
                client, tuple(_channel_types(env)),
                (env.get(cfg.ENV_DISCORD_BOT_USER_ID) or ""))
            marks = await _thread_reactions(client, forum)
        finally:
            await client.close()
        async with RoadmapClient.from_env(env) as roadmap_client:
            await roadmap_client.login()
            snapshot = await roadmap_client.fetch()
        return forum, marks, snapshot

    forum, marks, snapshot = _asyncio.run(go())
    players = cfg.PlayerMap.load(getattr(args, "players", None)
                                 or cfg.Settings.from_env(env).players_path)
    threads = [linking.ThreadRef(
        id=t.id, channel_id=t.channel_id, title=t.title,
        body=(t.starter.content if t.starter else ""),
        url=t.url, archived=t.archived, reactions=marks.get(t.id, ()),
        # Every message, so a "moved to <link>" note left when an idea was
        # moved between the two forums is found. See ThreadRef.superseded_by.
        messages=tuple(m.content for m in t.all_messages),
        author_id=t.author_id,
        author=(players.ids.get(t.author_id) or t.author_name or ""))
        for t in forum.threads]

    proposals = linking.propose(threads, list(snapshot.ideas), judge,
                                scorer_id=cfg.DUPE_SCORER_ID)
    summary = linking.summarise(proposals)
    payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "model": (judge.model if judge else ""),
               "proposals": [p.as_dict() for p in proposals]}
    Path(args.out).write_text(_json.dumps(payload, indent=2, ensure_ascii=False),
                              encoding="utf-8")

    print(f"{summary['threads']} unlinked thread(s); the model agrees with "
          f"{summary['model_agrees']}, you have marked "
          f"{summary['marked_by_admin']}, {summary['no_candidates']} have no "
          f"candidate at all.", file=out)
    print(f"wrote {args.out}. Nothing was linked.", file=out)
    if not judge:
        print("The model was not asked; these are token scores alone.", file=out)
    if args.upload:
        code = _upload_links(env, payload, out)
        if code != EXIT_OK:
            return code
        print("Uploaded. Review them in the editor's Thread links tab.", file=out)
    else:
        print("Pass --upload to send them to the editor for review.", file=out)
    return EXIT_OK


async def _thread_reactions(client: Any, forum: Any
                            ) -> dict:  # pragma: no cover - needs a gateway
    """The reactions on each thread's opening post.

    Not part of the sync snapshot: the planners have no use for reactions, and
    this is the only thing that does. The admin's convention is a salute when
    an idea was created for a thread and a check when it shipped -- only 7 of
    37 threads carry either, so it corroborates a match and never makes one.
    """
    out: dict[str, tuple] = {}
    for thread in forum.threads:
        try:
            channel = (client.get_channel(int(thread.id))
                       or await client.fetch_channel(int(thread.id)))
            message = await channel.fetch_message(int(thread.id))
            out[thread.id] = tuple(str(r.emoji) for r in message.reactions)
        except Exception as exc:
            log.warning("no reactions for thread %s: %s", thread.id, exc)
            out[thread.id] = ()
    return out


def _upload_links(env: Mapping[str, str], payload: Mapping[str, Any],
                  out: Any) -> int:  # pragma: no cover - needs the editor
    """POST the proposals to the editor so its review tab can read them."""
    import asyncio as _asyncio

    from nwnbot.roadmap import RoadmapClient, RoadmapError

    async def go():
        async with RoadmapClient.from_env(env) as client:
            await client.login()
            return await client.post_json("/api/thread-links", dict(payload))

    try:
        _asyncio.run(go())
    except (RoadmapError, OSError) as exc:
        print(f"upload failed: {exc}", file=out)
        return EXIT_FAIL
    return EXIT_OK


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
        "A live run opens one forum thread per item below, on a player-facing",
        "forum, with no undo. Read this list first — that is what it is for.",
        "",
        f"**Total: {len(plan_actions)} thread(s) would be created.**",
        "",
        "Eligibility (`[b8-backfill]`): a player's item earns a thread at any open",
        "status; a staff item earns one only once it is `soon` or beyond. Hidden",
        "items, duplicate rows, and anything already paid or terminal are never",
        "included, and an item that already has a thread is not re-opened.",
        "",
        "To run it — the number is the confirmation, so it has to be typed:",
        "",
        "```",
        f"NWNBOT_DRY_RUN=0 python -m nwnbot backfill --yes --cap {len(plan_actions)}",
        "```",
        "",
        f"Paced at {cfg.BACKFILL_MIN_INTERVAL:g}s between threads, so expect about",
        f"{max(1, round(len(plan_actions) * cfg.BACKFILL_MIN_INTERVAL / 60))} minute(s).",
        "Ctrl-C is safe: every thread is checkpointed as it is made, and re-running",
        "resumes rather than opening a second thread for the same item.",
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
    armed = apply_allowed(args, env)
    if getattr(args, "yes", False) and not armed:
        print(BACKFILL_REFUSAL, file=out)
        return EXIT_REFUSED
    if not args.fixture:  # pragma: no cover - the admin's live invocation
        return _live(args, env, out, dry_run=not armed, backfill_to=args.out)

    world = load_fixture(args.fixture)
    # Plan with the cap lifted so the *report* can state the true number even
    # when it is over the cap; the cap is then re-applied as the arming gate
    # below. A report that said "aborted, over cap" would hide the one number
    # the admin has to confirm.
    view = world.view if args.db is None else read_only_view(args.db)
    survey = engine_for_world(world, store=view, dry_run=True, cap=-1)
    report = asyncio.run(survey.cycle(reason="backfill"))
    planned = [a for a in report.actions if isinstance(a, CreateThread)]
    if not armed:
        return _write_backfill(report, world.context, args.out, out)

    cap = args.cap if args.cap is not None else DEFAULT_ACTION_CAP
    if cap < len(planned):
        _write_backfill(report, world.context, args.out, out)
        print(BACKFILL_CAP_REFUSAL.format(n=len(planned), cap=cap, path=args.out),
              file=out)
        return EXIT_REFUSED
    return _run_backfill(args, world, planned, out)


def _run_backfill(args: argparse.Namespace, world: World, planned: list,
                  out: Any) -> int:
    """Execute the batch, paced, checkpointing as it goes.

    The store is opened for *writing* here — unlike every other path in this
    module — because the checkpoint after each created thread is the whole
    resume story: an interruption leaves the threads already made recorded on
    both sides, so a re-run plans only what is left rather than opening a second
    thread for the same item.
    """
    store = Store(args.db) if args.db else None
    try:
        engine = engine_for_world(
            world, store=store if store is not None else world.view,
            roadmap_client=args.roadmap_client, forum_writer=args.forum_writer,
            dry_run=False, cap=len(planned))
        engine.pace = cfg.BACKFILL_MIN_INTERVAL if args.pace is None else args.pace
        print(f"opening {len(planned)} thread(s), "
              f"{engine.pace:g}s apart. Ctrl-C is safe: every thread is "
              f"checkpointed as it is made.", file=out)
        report = asyncio.run(engine.cycle(reason="backfill --yes"))
    except KeyboardInterrupt:  # pragma: no cover - the admin's Ctrl-C
        print("\ninterrupted. Re-run to resume; nothing will be created twice.",
              file=out)
        return EXIT_FAIL
    finally:
        if store is not None:
            store.close()
    print_report(report, out)
    return EXIT_FAIL if (report.failures or report.conflicts) else EXIT_OK


def _write_backfill(report: RunReport, context: PlanContext, path: str,
                    out: Any) -> int:
    creates = [a for a in report.actions if isinstance(a, CreateThread)]
    Path(path).write_text(render_backfill(creates, context=context), encoding="utf-8")
    print(f"wrote {path}: {len(creates)} thread(s) would be created.", file=out)
    print(f"Nothing was created. Read it, then: NWNBOT_DRY_RUN=0 "
          f"python -m nwnbot backfill --yes --cap {len(creates)}", file=out)
    return EXIT_OK


# --------------------------------------------------------------------------
# serve, and the live path — never exercised by the test suite
# --------------------------------------------------------------------------
def _image_store(env: Mapping[str, str]):  # pragma: no cover - thin wiring
    """Where rehosted screenshots go, or ``None`` when R2 is not configured.

    Rehosting is a read of Discord and a write to object storage; it is NOT
    gated on NWNBOT_DRY_RUN, which governs writes to the guild and the roadmap.
    A dry run that could not rehost would print a plan claiming every image was
    lost, which is not what applying it would do — the plan would be a lie.
    Objects are content-addressed, so a dry run and the apply that follows write
    the same object once.
    """
    from nwnbot import r2

    return r2.from_env(env)


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
        only=frozenset(getattr(args, "only", None) or ()),
        editor_url=(env.get(cfg.ENV_ROADMAP_BASE_URL) or "").rstrip("/"),
        thread_url_template=("https://discord.com/channels/"
                             f"{env.get(cfg.ENV_DISCORD_GUILD_ID, '')}/{{thread_id}}"),
        dupe_low=settings.dupe_low, dupe_high=settings.dupe_high,
        dupe_title_weight=cfg.DUPE_TITLE_WEIGHT,
        dupe_notes_max=cfg.DUPE_NOTES_MAX_CHARS,
        dupe_candidates=cfg.DUPE_CANDIDATE_LIMIT,
        dupe_post_in_thread=cfg.DUPE_POST_IN_THREAD,
        staff_players=cfg.STAFF_PLAYERS,
        staff_thread_statuses=cfg.STAFF_THREAD_STATUSES)


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
                                    context.bot_user_id,
                                    image_store=_image_store(env))
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
                                context.bot_user_id,
                                image_store=_image_store(env))
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
        sub.add_argument("--only", metavar="IDEA_ID", action="append", default=None,
                         help="restrict the run to this roadmap idea id; repeat "
                              "for several. Use it to try ONE item live and look "
                              "at the result before running a batch — the cap "
                              "aborts a big plan rather than trimming it, so it "
                              "cannot do this on its own")

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
                          help="execute the batch; also needs NWNBOT_DRY_RUN=0 "
                               "and --cap at least the planned thread count")
    backfill.add_argument("--pace", type=float, default=None, metavar="SECONDS",
                          help=f"seconds between thread creations "
                               f"(default {cfg.BACKFILL_MIN_INTERVAL:g})")

    serve = subs.add_parser("serve", help="run the long-lived Discord runtime")
    common(serve)

    review = subs.add_parser(
        "review", help="list the review queue, or resolve an entry")
    common(review)
    review.add_argument("--all", action="store_true",
                        help="include entries already resolved")
    review.add_argument("--resolve", metavar="KEY", nargs="+", default=None,
                        help="mark entries resolved. For a possible_dupe entry "
                             "this IS the rejection: a resolved review is never "
                             "raised again")

    dupes_ = subs.add_parser(
        "dupes", help="score the roadmap against itself to pick the thresholds")
    common(dupes_)
    dupes_.add_argument("--roadmap-yaml", metavar="PATH", default=None,
                        help="score a roadmap.yaml on disk instead of fetching; "
                             "read-only, and needs no account or network")
    dupes_.add_argument("--calibrate", action="store_true",
                        help="score every idea pair and print the ranking")
    dupes_.add_argument("--top", type=int, default=100, metavar="N",
                        help="how many pairs to print (default 100)")
    dupes_.add_argument("--floor", type=float, default=0.3, metavar="X",
                        help="ignore pairs below this score (default 0.3)")

    link = subs.add_parser(
        "link", help="match existing Discord threads to roadmap ideas they "
                     "already describe (proposes; never links)")
    common(link)
    link.add_argument("--out", metavar="PATH", default="thread-links.json",
                      help="where to write the proposals "
                           "(default thread-links.json)")
    link.add_argument("--upload", action="store_true",
                      help="also POST the proposals to the roadmap editor, so "
                           "its Thread links tab can review them")
    link.add_argument("--no-llm", action="store_true",
                      help="token scorer only; do not ask the model")

    return parser


HANDLERS = {
    "doctor": cmd_doctor,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "backfill": cmd_backfill,
    "serve": cmd_serve,
    "review": cmd_review,
    "dupes": cmd_dupes,
    "link": cmd_link,
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
    if args.db is None:
        # `plan` and `backfill` resolve this too. They were once excluded to
        # keep them from CREATING a database, but read_only_view already
        # refuses to: it returns an empty view for a path that does not exist.
        # Excluding them only stopped them READING an existing store, which
        # made `plan` report actions `apply` would skip -- baselines already
        # recorded, comments already carried over. A preview that overstates
        # what will happen is worse than no preview.
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
    "cmd_dupes",
    "cmd_review",
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
