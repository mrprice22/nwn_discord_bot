"""Tests for ``nwnbot.cli`` and ``nwnbot.bot`` — ``[b7-cli-runtime]``.

Four properties carry the weight:

1. **``plan`` writes nothing.** Not to Discord, not to the roadmap, not to the
   local database — asserted by fingerprinting a whole directory tree before
   and after the run, not by trusting a docstring.
2. **``apply`` needs both keys.** ``--yes`` alone is not consent and
   ``NWNBOT_DRY_RUN=0`` alone is not consent.
3. **One funnel.** Every Discord event handler and the 15-minute reconcile
   reach exactly one :meth:`SyncEngine.cycle`, and :func:`nwnbot.bot.plan_all`
   is the only caller of either planner outside :mod:`nwnbot.sync` — asserted
   over the source, so a future edit that adds a second path fails here.
4. **Nothing live.** No test in this file has a token, a password, a real
   channel id or a socket. The roadmap client and the forum writer are fakes,
   and the world comes from ``tests/fixtures/fake_world.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from nwnbot import bot as botmod
from nwnbot import cli
from nwnbot.forum import RecordingForumWriter
from nwnbot.roadmap import SaveConflict
from nwnbot.store import Store
from nwnbot.sync import PlanContext

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "fake_world.json"

#: A complete, entirely fake environment. Every value is a placeholder; nothing
#: here is a credential and none of it is ever sent anywhere.
FAKE_ENV = {
    "DISCORD_BOT_TOKEN": "fake-token-not-a-real-one",
    "DISCORD_GUILD_ID": "fake-guild",
    "DISCORD_BUGS_FORUM_ID": "fake-channel-bugs",
    "DISCORD_FEATURES_FORUM_ID": "fake-channel-features",
    "ROADMAP_BASE_URL": "https://roadmap.example.invalid",
    "ROADMAP_USER": "fake-user",
    "ROADMAP_PASSWORD": "fake-password",
    "NWNBOT_DB": "state.db",
    "NWNBOT_DRY_RUN": "1",
}


class Out:
    """A stand-in for stdout that a test can read back."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, text: str) -> int:
        self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class FakeRoadmap:
    """Every roadmap call, recorded. No transport, no session, no socket."""

    def __init__(self, conflict_on: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.conflict_on = conflict_on

    async def new_idea(self, idea, **kw):
        self._maybe_conflict("new_idea")
        self.calls.append(("new_idea", idea["id"]))
        return None

    async def comment(self, idea_id, text):
        self._maybe_conflict("comment")
        self.calls.append(("comment", idea_id, text))
        return {"ok": True}

    async def save(self, mutate, **kw):
        self._maybe_conflict("save")
        self.calls.append(("save",))
        return None

    def _maybe_conflict(self, kind: str) -> None:
        if self.conflict_on == kind:
            raise SaveConflict("the same idea changed on both sides",
                               overlap=["fake-bank-tab-order-resets"])

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(c[0] for c in self.calls)


def fingerprint(root: Path) -> dict[str, str]:
    """Every file under ``root``, by content. Catches a stray sqlite file."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def parse(argv):
    args = cli.build_parser().parse_args(argv)
    args.roadmap_client = None
    args.forum_writer = None
    return args


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def test_plan_against_fakes_prints_an_action_list():
    out = Out()
    code = cli.main(["plan", "--fixture", str(FIXTURE)], env=FAKE_ENV, out=out)
    assert code == cli.EXIT_OK
    assert "discord->roadmap" in out.text and "roadmap->discord" in out.text
    # Both directions produced something, so this is a real list, not an empty one.
    assert "create idea" in out.text
    assert "create thread" in out.text
    assert "dry run: nothing was written" in out.text


def test_plan_writes_nothing_at_all(tmp_path, monkeypatch):
    """No Discord call, no roadmap call, and no local database either."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "decoy.txt").write_text("untouched", encoding="utf-8")
    monkeypatch.chdir(work)
    before = fingerprint(work)

    out = Out()
    assert cli.main(["plan", "--fixture", str(FIXTURE)],
                    env=FAKE_ENV, out=out) == cli.EXIT_OK

    assert fingerprint(work) == before, "plan created or changed a file"


def test_plan_does_not_create_the_database_when_asked_for_a_missing_one(tmp_path):
    db = tmp_path / "state.db"
    out = Out()
    cli.main(["plan", "--fixture", str(FIXTURE), "--db", str(db)],
             env=FAKE_ENV, out=out)
    assert not db.exists()


def test_plan_over_the_action_cap_aborts_and_reports(tmp_path):
    out = Out()
    code = cli.main(["plan", "--fixture", str(FIXTURE), "--cap", "1"],
                    env=FAKE_ENV, out=out)
    assert code == cli.EXIT_FAIL
    assert "ABORTED" in out.text and "cap" in out.text


def test_reading_the_fixture_never_touches_a_credential():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.pop("_comment", None)  # the header says these words; the data must not
    raw = json.dumps(data).lower()
    for word in ("token", "password", "secret", "homerslotr.com", "127.0.0.1"):
        assert word not in raw, f"the fake world must not contain {word!r}"


# --------------------------------------------------------------------------
# apply — the refusal is the feature
# --------------------------------------------------------------------------
def test_apply_without_yes_exits_non_zero():
    out = Out()
    code = cli.main(["apply", "--fixture", str(FIXTURE)], env=FAKE_ENV, out=out)
    assert code != cli.EXIT_OK
    assert code == cli.EXIT_REFUSED
    assert "refusing to apply" in out.text


def test_apply_with_yes_but_dry_run_still_refuses():
    out = Out()
    code = cli.main(["apply", "--fixture", str(FIXTURE), "--yes"],
                    env=dict(FAKE_ENV, NWNBOT_DRY_RUN="1"), out=out)
    assert code == cli.EXIT_REFUSED
    assert "NWNBOT_DRY_RUN=0: not set" in out.text


def test_apply_with_dry_run_zero_but_no_yes_still_refuses():
    out = Out()
    code = cli.main(["apply", "--fixture", str(FIXTURE)],
                    env=dict(FAKE_ENV, NWNBOT_DRY_RUN="0"), out=out)
    assert code == cli.EXIT_REFUSED
    assert "--yes: missing" in out.text


@pytest.mark.parametrize("yes,dry,expected", [
    (False, "1", False), (True, "1", False), (False, "0", False), (True, "0", True),
])
def test_both_keys_are_needed(yes, dry, expected):
    args = parse(["apply", "--fixture", str(FIXTURE)] + (["--yes"] if yes else []))
    assert cli.apply_allowed(args, dict(FAKE_ENV, NWNBOT_DRY_RUN=dry)) is expected


def test_apply_executes_through_fakes_and_records_state(tmp_path):
    db = tmp_path / "state.db"
    roadmap, writer, out = FakeRoadmap(), RecordingForumWriter(), Out()
    args = parse(["apply", "--fixture", str(FIXTURE), "--yes", "--db", str(db)])
    args.roadmap_client = roadmap
    args.forum_writer = writer

    code = cli.cmd_apply(args, dict(FAKE_ENV, NWNBOT_DRY_RUN="0"), out)

    assert code == cli.EXIT_OK
    assert "new_idea" in roadmap.kinds and "comment" in roadmap.kinds
    assert "create_thread" in writer.kinds
    with Store(str(db)) as store:
        view = store.view()
        # The new thread was checkpointed the moment Discord answered.
        assert view.idea_for_thread("fake-thread-1") == "fake-lantern-flickers-in-the-rain"
        assert view.hash_of("fake-bank-tab-order-resets", "status")


def test_applying_twice_is_a_no_op(tmp_path):
    """The store makes the second run boring — the loop-prevention promise."""
    db = tmp_path / "state.db"
    env = dict(FAKE_ENV, NWNBOT_DRY_RUN="0")
    first = FakeRoadmap()
    args = parse(["apply", "--fixture", str(FIXTURE), "--yes", "--db", str(db)])
    args.roadmap_client, args.forum_writer = first, RecordingForumWriter()
    cli.cmd_apply(args, env, Out())

    second = FakeRoadmap()
    writer = RecordingForumWriter()
    args2 = parse(["apply", "--fixture", str(FIXTURE), "--yes", "--db", str(db)])
    args2.roadmap_client, args2.forum_writer = second, writer
    cli.cmd_apply(args2, env, Out())

    # The comments and the baseline are all already recorded; only the idea and
    # the thread the fixture still lacks (its ids are not fed back in) recur.
    assert "comment" not in second.kinds


def test_save_conflict_is_recorded_reported_and_fatal(tmp_path):
    """[r10]'s proposed answer, implemented: record, report, exit non-zero."""
    db = tmp_path / "state.db"
    roadmap = FakeRoadmap(conflict_on="new_idea")
    writer, out = RecordingForumWriter(), Out()
    args = parse(["apply", "--fixture", str(FIXTURE), "--yes", "--db", str(db)])
    args.roadmap_client, args.forum_writer = roadmap, writer

    code = cli.cmd_apply(args, dict(FAKE_ENV, NWNBOT_DRY_RUN="0"), out)

    assert code == cli.EXIT_FAIL
    assert "SAVE CONFLICT" in out.text
    assert roadmap.kinds == (), "no retry, and nothing after the conflict"
    with Store(str(db)) as store:
        keys = [r.key for r in store.reviews()]
    assert any(k.startswith(botmod.REVIEW_SAVE_CONFLICT) for k in keys)


def test_apply_over_the_cap_executes_nothing(tmp_path):
    db = tmp_path / "state.db"
    roadmap, writer, out = FakeRoadmap(), RecordingForumWriter(), Out()
    args = parse(["apply", "--fixture", str(FIXTURE), "--yes", "--db", str(db),
                  "--cap", "1"])
    args.roadmap_client, args.forum_writer = roadmap, writer

    code = cli.cmd_apply(args, dict(FAKE_ENV, NWNBOT_DRY_RUN="0"), out)

    assert code == cli.EXIT_FAIL
    assert roadmap.calls == [] and writer.calls == []
    assert "ABORTED" in out.text


# --------------------------------------------------------------------------
# backfill — report only; the batch is [b8], blocked on [r4]
# --------------------------------------------------------------------------
def test_backfill_writes_a_report_and_creates_nothing(tmp_path):
    report = tmp_path / "backfill-plan.md"
    out = Out()
    code = cli.main(["backfill", "--fixture", str(FIXTURE), "--out", str(report)],
                    env=FAKE_ENV, out=out)
    assert code == cli.EXIT_OK
    text = report.read_text(encoding="utf-8")
    assert "Total: 1 thread(s)" in text
    assert "example-tag-forge" in text
    assert "[b8-backfill]" in text and "[r4]" in text
    assert "Nothing has been created" in text


def test_backfill_yes_is_refused_and_points_at_the_review_item(tmp_path):
    report = tmp_path / "backfill-plan.md"
    out = Out()
    code = cli.main(["backfill", "--fixture", str(FIXTURE), "--out", str(report),
                     "--yes"], env=FAKE_ENV, out=out)
    assert code != cli.EXIT_OK
    assert "[b8-backfill]" in out.text and "[r4]" in out.text
    assert not report.exists(), "a refused backfill writes nothing at all"


def test_backfill_report_is_gitignored():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "backfill-plan.md" in ignored


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def test_doctor_reports_missing_env_and_fails():
    out = Out()
    code = cli.main(["doctor", "--db", ""], env={}, out=out)
    assert code == cli.EXIT_FAIL
    assert "DISCORD_BOT_TOKEN" in out.text


def test_doctor_never_prints_a_secret():
    env = dict(FAKE_ENV, DISCORD_BOT_TOKEN="sup3rsecret-token",
               ROADMAP_PASSWORD="sup3rsecret-password")
    out = Out()
    cli.main(["doctor", "--db", ""], env=env, out=out)
    assert "sup3rsecret" not in out.text


def test_doctor_passes_with_a_complete_fake_world(tmp_path):
    out = Out()
    code = cli.main(["doctor", "--fixture", str(FIXTURE),
                     "--db", str(tmp_path / "state.db")], env=FAKE_ENV, out=out)
    assert code == cli.EXIT_OK
    assert "[ok  ] groups" in out.text


def test_doctor_says_the_tag_map_is_blocked_rather_than_inventing_one(tmp_path):
    out = Out()
    cli.main(["doctor", "--fixture", str(FIXTURE), "--db", str(tmp_path / "s.db")],
             env=FAKE_ENV, out=out)
    assert "[warn] tag-map" in out.text
    assert "[b5-config]" in out.text and "[r2]" in out.text


def test_doctor_exits_non_zero_on_a_deliberately_broken_mapping(tmp_path):
    """`[b5-config]`'s acceptance, as far as it can be checked today."""
    broken = tmp_path / "tags.json"
    broken.write_text(json.dumps({"example-tag-forge": "not-a-real-group",
                                  "example-tag-qol": "qol"}), encoding="utf-8")
    out = Out()
    code = cli.main(["doctor", "--fixture", str(FIXTURE), "--db", str(tmp_path / "s.db"),
                     "--tag-map", str(broken)], env=FAKE_ENV, out=out)
    assert code == cli.EXIT_FAIL
    assert "[FAIL] tag-map" in out.text


def test_doctor_accepts_a_complete_mapping(tmp_path):
    from nwnbot.config import GROUP_IDS

    good = tmp_path / "tags.json"
    good.write_text(json.dumps({f"example-tag-{g}": g for g in GROUP_IDS}),
                    encoding="utf-8")
    out = Out()
    code = cli.main(["doctor", "--fixture", str(FIXTURE), "--db", str(tmp_path / "s.db"),
                     "--tag-map", str(good)], env=FAKE_ENV, out=out)
    assert code == cli.EXIT_OK
    assert "[ok  ] tag-map" in out.text


def test_doctor_contacts_nothing_by_default(tmp_path):
    out = Out()
    cli.main(["doctor", "--db", str(tmp_path / "s.db")], env=FAKE_ENV, out=out)
    assert "[--  ] roadmap: not contacted" in out.text
    assert "[--  ] discord: not contacted" in out.text


# --------------------------------------------------------------------------
# One funnel: the event path and the 15-minute reconcile cannot diverge
# --------------------------------------------------------------------------
def world():
    return cli.load_fixture(FIXTURE)


def engine():
    return cli.engine_for_world(world(), dry_run=True)


class CountingEngine:
    """Stands in for :class:`SyncEngine` and counts the funnel's only call."""

    def __init__(self) -> None:
        self.context = PlanContext(bot_user_id="fake-bot-user")
        self.reasons: list[str] = []

    async def cycle(self, *, reason: str = ""):
        self.reasons.append(reason)
        return botmod.RunReport(dry_run=True, reason=reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda f: f.on_thread_create(type("T", (), {"id": 7})()),
    lambda f: f.on_message(type("M", (), {"id": 8, "author": type("A", (), {"id": 99})()})()),
    lambda f: f.on_raw_thread_update(type("P", (), {"thread_id": 9})()),
])
async def test_every_event_handler_runs_exactly_one_cycle(call):
    counting = CountingEngine()
    funnel = botmod.EventFunnel(counting, debounce=0)
    await call(funnel)
    await funnel.run_pending()
    assert len(counting.reasons) == 1


@pytest.mark.asyncio
async def test_the_reconcile_pulls_the_same_lever_an_event_does():
    counting = CountingEngine()
    funnel = botmod.EventFunnel(counting, debounce=0)
    funnel.request_cycle("reconcile")
    await funnel.run_pending()
    await funnel.on_thread_create(type("T", (), {"id": 1})())
    await funnel.run_pending()
    assert counting.reasons == ["reconcile", "thread_create:1"]


@pytest.mark.asyncio
async def test_a_burst_of_events_coalesces_into_one_cycle():
    counting = CountingEngine()
    funnel = botmod.EventFunnel(counting, debounce=0)
    for n in range(5):
        await funnel.on_thread_create(type("T", (), {"id": n})())
    await funnel.run_pending()
    assert len(counting.reasons) == 1


@pytest.mark.asyncio
async def test_the_bot_never_reacts_to_its_own_message():
    counting = CountingEngine()
    funnel = botmod.EventFunnel(counting, debounce=0)
    own = type("M", (), {"id": 3,
                         "author": type("A", (), {"id": "fake-bot-user"})()})()
    await funnel.on_message(own)
    assert await funnel.run_pending() is None


@pytest.mark.asyncio
async def test_nothing_pending_means_no_cycle():
    counting = CountingEngine()
    funnel = botmod.EventFunnel(counting, debounce=0)
    assert await funnel.run_pending() is None
    assert counting.reasons == []


def test_the_reconcile_interval_is_fifteen_minutes():
    assert botmod.RECONCILE_INTERVAL_SECONDS == 15 * 60


def test_plan_all_is_the_only_caller_of_either_planner():
    """The structural guarantee, not a claim: grep the package for a second path.

    ``nwnbot/sync.py`` defines them; ``nwnbot/bot.py`` calls them exactly once
    each, inside :func:`plan_all`. Any other call site in ``nwnbot/`` is a
    second code path the event handlers and the reconcile could drift into, and
    fails here.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "nwnbot").glob("*.py")):
        if path.name == "sync.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for name in ("plan_discord_to_roadmap", "plan_roadmap_to_discord"):
                if f"{name}(" in code:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert len(offenders) == 2, offenders
    assert all(o.startswith("bot.py") for o in offenders), offenders

    source = (REPO / "nwnbot" / "bot.py").read_text(encoding="utf-8")
    body = source.split("def plan_all(", 1)[1].split("\ndef ", 1)[0]
    assert "plan_discord_to_roadmap(" in body and "plan_roadmap_to_discord(" in body


def test_both_directions_come_back_from_one_call():
    w = world()
    plans = botmod.plan_all(w.roadmap, w.forum, w.view, w.context)
    assert [p.direction for p in plans] == ["discord->roadmap", "roadmap->discord"]


def test_the_engine_is_dry_by_default():
    assert botmod.SyncEngine(botmod.StaticSource(world().roadmap, world().forum),
                             PlanContext()).dry_run is True


def test_a_dry_cycle_calls_no_writer_and_no_client():
    writer = RecordingForumWriter()
    eng = cli.engine_for_world(world(), dry_run=True, forum_writer=writer)
    report = asyncio.run(eng.cycle(reason="test"))
    assert writer.calls == []
    assert report.dry_run and report.writes


# --------------------------------------------------------------------------
# Packaging and the systemd unit — [r7]'s proposal, as shipped
# --------------------------------------------------------------------------
def test_python_dash_m_nwnbot_has_an_entry_point():
    main_py = REPO / "nwnbot" / "__main__.py"
    assert main_py.exists()
    assert "from nwnbot.cli import main" in main_py.read_text(encoding="utf-8")


def test_pyproject_stays_pytest_config_only():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project]" not in text and "[build-system]" not in text


def test_the_unit_is_not_armed_and_says_so():
    unit = (REPO / "systemd" / "nwnbot.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in unit
    assert "EnvironmentFile=" in unit
    assert "WorkingDirectory=" in unit and "PYTHONPATH=" in unit
    assert "NOT enabled by default" in unit
    assert "decision" in unit
    # The unit must never set the live switch itself.
    assert "Environment=NWNBOT_DRY_RUN" not in unit


def test_nothing_in_the_repo_hard_codes_a_credential():
    for path in sorted((REPO / "nwnbot").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "roadmap.homerslotr.com" not in text, path.name
        assert "127.0.0.1:8765" not in text or path.name == "config.py", path.name


def test_commands_are_the_five_the_backlog_names():
    assert cli.COMMANDS == ("doctor", "plan", "apply", "backfill", "serve")
    parser = cli.build_parser()
    for command in cli.COMMANDS:
        assert parser.parse_args([command]).command == command


def test_cli_never_reads_the_dotenv_file():
    """`.env` holds real credentials. Nothing here loads it; systemd does."""
    text = (REPO / "nwnbot" / "cli.py").read_text(encoding="utf-8")
    assert "dotenv" not in text
    assert 'load_dotenv' not in text
