"""Tests for ``nwnbot.roadmap`` — [b3-roadmap-client].

Everything runs over :class:`FakeSession`, an in-memory stand-in for
``aiohttp.ClientSession`` with the same ``request(...)`` async-context-manager
shape. **No test here opens a socket**, and none of them may ever be pointed at
the live editor or the live tunnel.

The shapes the fake answers with are the editor's real ones, read out of
``nwn_homers_lotr/bin/roadmap-editor.py``:

- a conflict is ``HTTP 200`` with ``{"ok": false, "conflict": true, "overlap":
  [...], "version": …}`` (``:3157`` / ``:3170``), *not* a 409;
- a validation failure is ``HTTP 200`` with ``{"ok": false, "errors": [...]}``
  and no ``conflict`` key (``:3193``);
- a bad login is ``HTTP 401 {"ok": false, "message": …}`` (``:3007``).
"""

from __future__ import annotations

import pytest

from nwnbot.roadmap import (
    API_DATA,
    API_IDEA_COMMENT,
    API_LOGIN,
    API_SAVE,
    FORBIDDEN_BLOCKS,
    FORBIDDEN_STATUSES,
    ForbiddenWrite,
    JSON_CONTENT_TYPE,
    RoadmapAuthError,
    RoadmapClient,
    RoadmapPermissionError,
    SAVE_PAYLOAD_KEYS,
    SaveConflict,
    SaveRejected,
    Snapshot,
    assert_ideas_writable,
)


# --------------------------------------------------------------------------
# Fake transport
# --------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):  # noqa: ARG002 - mirrors aiohttp
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records every request and answers from a queued script per path."""

    def __init__(self, responses: dict[str, list[tuple[int, dict]]] | None = None):
        self.responses: dict[str, list[tuple[int, dict]]] = responses or {}
        self.calls: list[dict] = []
        self.closed = False

    def queue(self, path: str, status: int, body: dict) -> "FakeSession":
        self.responses.setdefault(path, []).append((status, body))
        return self

    def request(self, method, url, *, json=None, headers=None):
        path = url.split("https://roadmap.invalid", 1)[-1]
        self.calls.append({"method": method, "url": url, "path": path,
                           "json": json, "headers": dict(headers or {})})
        script = self.responses.get(path)
        if not script:
            raise AssertionError(f"unscripted request: {method} {path}")
        status, body = script.pop(0)
        return FakeResponse(status, body)

    async def close(self):
        self.closed = True

    # convenience
    def saves(self):
        return [c for c in self.calls if c["path"] == API_SAVE]


BASE = "https://roadmap.invalid"

IDEAS = [
    {"id": "forge-tempering", "title": "Tempering costs too much",
     "group": "forge", "status": "planned", "type": "Defect", "player": "Sync"},
    {"id": "bank-tabs", "title": "Bank tabs", "group": "banking",
     "status": "confirmed", "type": "Enhancement", "player": "Balendin"},
    # A real document carries shipped items with merit already paid. They ride
    # along in every posted array; touching them is what must raise.
    {"id": "old-shipped", "title": "Already paid", "group": "qol",
     "status": "awarded", "merit_awarded": True, "player": "Sync"},
]

DATA = {
    "ideas": IDEAS,
    "vocab": {"players": ["community", "Sync", "Balendin"],
              "groups": [{"id": "forge", "title": "Forge"}],
              "ids": [i["id"] for i in IDEAS]},
    "environments": {"by_id": {}},
    "base_hashes": {"forge-tempering": "aaa1", "bank-tabs": "bbb2",
                    "old-shipped": "ccc3"},
    "base_vocab": {"players": "pfp"},
    "version": "v-server-1",
    "me": {"username": "nwnbot", "role": "admin"},
}


def data_payload(**over):
    payload = {k: (v.copy() if isinstance(v, (dict, list)) else v)
               for k, v in DATA.items()}
    payload["ideas"] = [dict(i) for i in DATA["ideas"]]
    payload.update(over)
    return payload


def make_client(session=None):
    session = session or FakeSession()
    return RoadmapClient(BASE, "nwnbot", "hunter2", session=session), session


def snapshot():
    return Snapshot.from_payload(data_payload())


# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_posts_json_and_records_me():
    client, session = make_client()
    session.queue(API_LOGIN, 200, {"ok": True, "me": {"username": "nwnbot",
                                                      "role": "admin"}})
    me = await client.login()
    assert me["username"] == "nwnbot"
    call = session.calls[0]
    assert call["method"] == "POST" and call["path"] == API_LOGIN
    assert call["json"] == {"username": "nwnbot", "password": "hunter2"}
    # _csrf_ok() refuses anything that is not declared JSON.
    assert call["headers"]["Content-Type"] == JSON_CONTENT_TYPE


@pytest.mark.asyncio
async def test_login_rejected_raises_auth_error_without_the_password():
    client, session = make_client()
    session.queue(API_LOGIN, 401, {"ok": False,
                                   "message": "Incorrect username or password."})
    with pytest.raises(RoadmapAuthError) as exc:
        await client.login()
    assert "hunter2" not in str(exc.value)
    assert "hunter2" not in repr(exc.value)


@pytest.mark.asyncio
async def test_login_ok_false_without_401_still_raises():
    # The bootstrap case: HTTP 503 {"ok": false, "setup": true}.
    client, session = make_client()
    session.queue(API_LOGIN, 200, {"ok": False, "setup": True,
                                   "message": "no accounts yet"})
    with pytest.raises(RoadmapAuthError):
        await client.login()


def test_password_never_appears_in_repr():
    client, _ = make_client()
    assert "hunter2" not in repr(client)
    assert "hunter2" not in repr(client.__dict__["_password"])
    assert "hunter2" not in str(client.__dict__["_password"])


def test_from_env_requires_every_credential(monkeypatch):
    from nwnbot.roadmap import RoadmapError

    with pytest.raises(RoadmapError):
        RoadmapClient.from_env({"ROADMAP_BASE_URL": BASE, "ROADMAP_USER": "nwnbot"})
    client = RoadmapClient.from_env({"ROADMAP_BASE_URL": BASE + "/",
                                     "ROADMAP_USER": "NWNBot",
                                     "ROADMAP_PASSWORD": "hunter2"})
    assert client.base_url == BASE and client.username == "nwnbot"
    assert "hunter2" not in repr(client)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_keeps_the_servers_baseline_verbatim():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    snap = await client.fetch()
    assert snap.version == "v-server-1"
    assert snap.base_hashes == DATA["base_hashes"]
    assert set(snap.by_id) == {"forge-tempering", "bank-tabs", "old-shipped"}
    assert session.calls[0]["method"] == "GET"


@pytest.mark.asyncio
async def test_expired_session_is_an_auth_error():
    client, session = make_client()
    session.queue(API_DATA, 401, {"ok": False, "auth": True,
                                  "errors": ["Your session has expired"]})
    with pytest.raises(RoadmapAuthError):
        await client.fetch()


@pytest.mark.asyncio
async def test_role_refusal_is_a_permission_error():
    # What a `bot` role lacking `edit` (or a tester) gets from /api/save.
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 403, {"ok": False,
                                  "message": "Your role does not have access"})
    with pytest.raises(RoadmapPermissionError):
        await client.save(lambda ideas: ideas[0].update(status="wip"))


# --------------------------------------------------------------------------
# save
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_echoes_base_version_and_hashes_and_sends_nothing_else():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v-server-2",
                                  "warnings": [], "message": "Saved roadmap.yaml."})
    result = await client.save(lambda ideas: ideas[0].update(status="wip"))
    assert result.version == "v-server-2" and result.attempts == 1

    posted = session.saves()[0]["json"]
    assert set(posted) == set(SAVE_PAYLOAD_KEYS)
    assert posted["base_version"] == "v-server-1"
    assert posted["base_hashes"] == DATA["base_hashes"]  # verbatim, never computed
    assert posted["ideas"][0]["status"] == "wip"
    assert session.saves()[0]["headers"]["Content-Type"] == JSON_CONTENT_TYPE
    # groups/players/epics are absent, which is what makes write_document()
    # leave those blocks alone.
    assert not (set(posted) & FORBIDDEN_BLOCKS)


@pytest.mark.asyncio
async def test_save_does_not_mutate_the_snapshot():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2"})
    snap = await client.fetch()
    await client.save(lambda ideas: ideas[0].update(status="wip"), snapshot=snap)
    assert snap.ideas[0]["status"] == "planned"


@pytest.mark.asyncio
async def test_save_rejected_reports_the_servers_errors():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": False,
                                  "errors": ["'bank-tabs': unknown group 'nope'"],
                                  "warnings": ["w"]})
    with pytest.raises(SaveRejected) as exc:
        await client.save(lambda ideas: ideas[1].update(group="banking"))
    assert exc.value.errors == ["'bank-tabs': unknown group 'nope'"]


# --------------------------------------------------------------------------
# conflict: re-fetch, retry once, then queue for review
# --------------------------------------------------------------------------
CONFLICT = {"ok": False, "conflict": True, "version": "v-server-9",
            "overlap": ["bank-tabs"],
            "message": "roadmap.yaml changed on disk and the same item(s) were "
                       "edited on both sides: bank-tabs."}


@pytest.mark.asyncio
async def test_conflict_refetches_and_retries_once():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, dict(CONFLICT))
    session.queue(API_DATA, 200, data_payload(version="v-server-9",
                                              base_hashes={"forge-tempering": "zzz9",
                                                           "bank-tabs": "bbb2",
                                                           "old-shipped": "ccc3"}))
    session.queue(API_SAVE, 200, {"ok": True, "version": "v-server-10"})

    result = await client.save(lambda ideas: ideas[0].update(status="wip"))
    assert result.attempts == 2 and result.version == "v-server-10"

    first, second = session.saves()
    assert first["json"]["base_version"] == "v-server-1"
    # The retry rebases on the FRESH server baseline, verbatim.
    assert second["json"]["base_version"] == "v-server-9"
    assert second["json"]["base_hashes"]["forge-tempering"] == "zzz9"


@pytest.mark.asyncio
async def test_second_conflict_is_queued_for_review_never_forced():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, dict(CONFLICT))
    session.queue(API_DATA, 200, data_payload(version="v-server-9"))
    session.queue(API_SAVE, 200, dict(CONFLICT))

    with pytest.raises(SaveConflict) as exc:
        await client.save(lambda ideas: ideas[1].update(status="wip"))
    assert exc.value.needs_review is True
    assert exc.value.overlap == ["bank-tabs"]
    assert exc.value.attempts == 2
    assert len(session.saves()) == 2                      # exactly one retry
    assert all("force" not in c["json"] for c in session.saves())


@pytest.mark.asyncio
async def test_conflict_without_a_baseline_is_still_a_conflict():
    # The no-base_hashes shape (roadmap-editor.py:3157) carries no `overlap`.
    client, session = make_client()
    bare = {"ok": False, "conflict": True, "version": "v9",
            "message": "roadmap.yaml changed on disk since you opened it"}
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, dict(bare))
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, dict(bare))
    with pytest.raises(SaveConflict) as exc:
        await client.save(lambda ideas: ideas[0].update(status="wip"))
    assert exc.value.overlap == []


# --------------------------------------------------------------------------
# The hard rules — each must raise BEFORE any HTTP request
# --------------------------------------------------------------------------
def _assert_no_request(session, from_call: int = 0):
    assert [c for c in session.calls if c["path"] == API_SAVE][from_call:] == []


@pytest.mark.parametrize("status", sorted(FORBIDDEN_STATUSES))
@pytest.mark.asyncio
async def test_forbidden_status_raises_before_any_request(status):
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite) as exc:
        await client.save(lambda ideas: ideas[0].update(status=status))
    assert status in str(exc.value)
    _assert_no_request(session)


@pytest.mark.asyncio
async def test_allowed_statuses_are_not_blocked():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2"})
    await client.save(lambda ideas: ideas[0].update(status="confirmed"))
    assert len(session.saves()) == 1


@pytest.mark.asyncio
async def test_untouched_shipped_item_rides_along_unharmed():
    # `old-shipped` is already awarded with merit paid; posting it back
    # unchanged must be fine, or the bot could never save anything at all.
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2"})
    await client.save(lambda ideas: ideas[0].update(status="soon"))
    posted = session.saves()[0]["json"]["ideas"]
    assert posted[2] == DATA["ideas"][2]


@pytest.mark.asyncio
async def test_writing_merit_awarded_raises_before_any_request():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite) as exc:
        await client.save(lambda ideas: ideas[0].update(merit_awarded=True))
    assert "merit_awarded" in str(exc.value)
    _assert_no_request(session)


def _drop_merit_flag(ideas):
    """A mutator returning ``None``, so the array itself is what is checked."""
    ideas[2].pop("merit_awarded")


@pytest.mark.asyncio
async def test_revoking_merit_awarded_raises_too():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite):
        await client.save(_drop_merit_flag)
    _assert_no_request(session)


@pytest.mark.asyncio
async def test_demoting_a_shipped_item_out_of_awarded_is_allowed_but_re_awarding_is_not():
    # Moving *into* a forbidden status is what raises; the client does not stop
    # a caller from setting a normal status.
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite):
        await client.save(lambda ideas: ideas[1].update(status="awarded"))
    _assert_no_request(session)


@pytest.mark.parametrize("block", sorted(FORBIDDEN_BLOCKS))
@pytest.mark.asyncio
async def test_document_blocks_never_reach_the_payload(block):
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite) as exc:
        await client.save(lambda ideas: ideas[0].update({block: ["whatever"]}))
    assert block in str(exc.value)
    _assert_no_request(session)


def test_assert_save_payload_refuses_extra_keys_and_force():
    from nwnbot.roadmap import assert_save_payload

    good = {"ideas": [], "base_version": "v", "base_hashes": {}}
    assert_save_payload(good)  # no raise
    for bad in ({**good, "force": True}, {**good, "players": ["Nobody"]},
                {**good, "groups": []}, {"ideas": [], "base_version": "v"}):
        with pytest.raises(ForbiddenWrite):
            assert_save_payload(bad)
    with pytest.raises(ForbiddenWrite):
        assert_save_payload({**good, "base_hashes": "not-a-map"})


@pytest.mark.asyncio
async def test_new_player_name_raises_before_any_request():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite) as exc:
        await client.save(lambda ideas: ideas[0].update(player="BrandNewPlayer"))
    assert "review item" in str(exc.value)
    _assert_no_request(session)


@pytest.mark.asyncio
async def test_a_known_player_may_be_credited():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2"})
    await client.save(lambda ideas: ideas[0].update(player="Balendin"))
    assert len(session.saves()) == 1


@pytest.mark.asyncio
async def test_dropping_an_idea_raises_before_any_request():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite) as exc:
        await client.save(lambda ideas: [i for i in ideas if i["id"] != "bank-tabs"])
    assert "bank-tabs" in str(exc.value)
    _assert_no_request(session)


def test_assert_ideas_writable_rejects_malformed_arrays():
    baseline = {}
    for bad in ("not-a-list", [["nope"]], [{"title": "no id"}],
                [{"id": "x", "status": "wip"}, {"id": "x", "status": "wip"}]):
        with pytest.raises(ForbiddenWrite):
            assert_ideas_writable(baseline, bad)


# --------------------------------------------------------------------------
# comment
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_comment_posts_id_and_text_only():
    client, session = make_client()
    session.queue(API_IDEA_COMMENT, 200, {"ok": True, "message": "Added your note"})
    await client.comment("bank-tabs", "  reported again in #bugs  ")
    call = session.calls[0]
    assert call["path"] == API_IDEA_COMMENT
    # author/date are stamped server-side; posting them would be ignored anyway.
    assert call["json"] == {"id": "bank-tabs", "text": "reported again in #bugs"}
    assert call["headers"]["Content-Type"] == JSON_CONTENT_TYPE


@pytest.mark.asyncio
async def test_empty_comment_never_reaches_the_server():
    client, session = make_client()
    with pytest.raises(ForbiddenWrite):
        await client.comment("bank-tabs", "   ")
    with pytest.raises(ForbiddenWrite):
        await client.comment("", "text")
    assert session.calls == []


# --------------------------------------------------------------------------
# new_idea
# --------------------------------------------------------------------------
NEW = {"id": "forge-anvil-sound", "title": "Anvil has no sound", "group": "forge",
       "status": "planned", "type": "Defect", "player": "Sync", "hidden": True}


@pytest.mark.asyncio
async def test_new_idea_appends_and_posts_the_whole_array():
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2"})
    await client.new_idea(dict(NEW))
    posted = session.saves()[0]["json"]["ideas"]
    assert len(posted) == len(IDEAS) + 1
    assert posted[-1]["id"] == "forge-anvil-sound"


@pytest.mark.asyncio
async def test_new_idea_tolerates_the_discord_field_before_b2_lands():
    # `discord` is not in IDEA_FIELDS yet; gen-roadmap.py:308 only *warns* about
    # an unrecognised key and write_document round-trips it, so this works today
    # and stops warning once [b2-roadmap-schema] lands.
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    session.queue(API_SAVE, 200, {"ok": True, "version": "v2",
                                  "warnings": ["[warn] unrecognised field 'discord'"]})
    result = await client.new_idea({**NEW, "discord": {"thread_id": "123"}})
    assert session.saves()[0]["json"]["ideas"][-1]["discord"] == {"thread_id": "123"}
    assert result.warnings  # the warning is surfaced, not swallowed


@pytest.mark.asyncio
async def test_new_idea_refuses_missing_fields_and_duplicate_ids():
    client, session = make_client()
    with pytest.raises(ForbiddenWrite):
        await client.new_idea({"id": "x", "title": "t"})
    assert session.calls == []
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite):
        await client.new_idea({**NEW, "id": "bank-tabs"})
    _assert_no_request(session)


@pytest.mark.parametrize("bad", [
    {"status": "awarded"},
    {"merit_awarded": True},
    {"player": "SomeoneNew"},
])
@pytest.mark.asyncio
async def test_new_idea_cannot_smuggle_a_forbidden_write(bad):
    client, session = make_client()
    session.queue(API_DATA, 200, data_payload())
    with pytest.raises(ForbiddenWrite):
        await client.new_idea({**NEW, **bad})
    _assert_no_request(session)


# --------------------------------------------------------------------------
# transport plumbing
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_session_is_an_error_not_a_socket():
    client = RoadmapClient(BASE, "nwnbot", "hunter2")
    from nwnbot.roadmap import RoadmapError

    with pytest.raises(RoadmapError):
        await client.fetch()


@pytest.mark.asyncio
async def test_injected_session_is_not_closed_by_the_client():
    client, session = make_client()
    await client.close()
    assert session.closed is False


def test_endpoint_constants_match_the_editors_routes():
    assert API_LOGIN == "/api/login"      # PUBLIC_ROUTES, roadmap-editor.py:1976
    assert API_DATA == "/api/data"
    assert API_SAVE == "/api/save"
    assert API_IDEA_COMMENT == "/api/idea-comment"
    assert "status" not in FORBIDDEN_BLOCKS
    assert FORBIDDEN_STATUSES == {"awarded", "implemented", "manual"}


# --------------------------------------------------------------------------
# `[r3]`: `type` is written once, at creation — never on an existing idea
# --------------------------------------------------------------------------
def test_changing_type_on_an_existing_idea_is_refused_on_the_wire():
    """A promoted Exploit is worth 3 merit; a demotion to Defect costs 2.

    The planner already cannot construct such an update
    (`sync.CREATION_ONLY_FIELDS`); this is the second line, at the byte that
    would go over the wire, so a hand-built save cannot do it either.
    """
    baseline = {"exploit-thing": {"id": "exploit-thing", "type": "Exploit",
                                  "status": "wip"}}
    with pytest.raises(ForbiddenWrite) as exc:
        assert_ideas_writable(baseline, [{"id": "exploit-thing", "type": "Defect",
                                          "status": "wip"}])
    assert "Exploit" in str(exc.value) and "type" in str(exc.value)


def test_leaving_type_alone_on_an_existing_idea_is_fine():
    baseline = {"exploit-thing": {"id": "exploit-thing", "type": "Exploit",
                                  "group": "forge"}}
    assert_ideas_writable(baseline, [{"id": "exploit-thing", "type": "Exploit",
                                      "group": "bosses"}])
    # An idea the bot never touches carries no `type` key at all in the diff.
    assert_ideas_writable(baseline, [{"id": "exploit-thing", "group": "bosses"}])


def test_a_brand_new_idea_may_still_set_its_type():
    assert_ideas_writable({}, [{"id": "new-thing", "type": "Defect", "hidden": True}])
