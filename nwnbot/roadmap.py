"""Async HTTP client for the roadmap editor's API — ``[b3-roadmap-client]``.

The editor is ``bin/roadmap-editor.py`` in ``nwn_homers_lotr``: a stdlib
``ThreadingHTTPServer`` published through a Cloudflare tunnel. Everything below
was read out of that file rather than guessed; the citations are file:line in
that repo.

Wire contract
-------------

``POST /api/login`` (``:2976``, routed at ``:3042``, public per ``PUBLIC_ROUTES``
at ``:1976``) takes ``{"username", "password"}`` and answers ``{"ok": true,
"me": {...}}`` with a ``Set-Cookie: roadmap_session=…`` (``:2329``). A failure is
``401`` with ``{"ok": false, "message": "Incorrect username or password."}``;
a throttled caller gets ``429``; an editor with no accounts yet gets ``503``
with ``"setup": true``.

``GET /api/data`` (``:2878``) answers ``{ideas, vocab, environments,
base_hashes, base_vocab, version, me}``. ``base_hashes`` is computed
**server-side** (``fingerprints()``, ``:272``) and the client's only job is to
store it and echo it back — this module therefore never hashes anything.

``POST /api/save`` (``:3220``) takes the whole ``ideas`` array plus
``base_version`` and ``base_hashes``. ``groups``/``players``/``epics`` are
optional and ``write_document()`` (``:1039``) leaves each block untouched when
its key is absent, so the way to "never touch" them is simply never to send
them. Success is ``200 {"ok": true, "version": …, "warnings": [...]}``.

**A conflict is not an HTTP error.** It comes back as ``200`` with
``{"ok": false, "conflict": true, "version": …, "overlap": [ids], "message":
…}`` (``:3157`` for the no-baseline case, ``:3170`` for a genuine same-idea
collision out of ``merge_ideas()``, ``:289``). A validation failure is a
different ``200`` shape: ``{"ok": false, "errors": [...]}`` (``:3193``) with no
``conflict`` key. ``force: true`` exists in that payload and this client never
sends it.

``POST /api/idea-comment`` (``:2665``) takes ``{"id", "text"}`` and appends
``{author, date, text}`` to the idea's internal, append-only ``comments`` list,
stamping author and date server-side. Text is truncated to
``MAX_COMMENT_LEN = 3000`` (``:2049``) by the server.

Every write route is gated on ``_csrf_ok()`` (``:2948``), which requires
``Content-Type: application/json`` — so every request here sends it, and
``_request`` asserts it did.

Schema today
------------

``[b2-roadmap-schema]`` has **not** landed, so there is no ``discord`` field in
``IDEA_FIELDS`` yet. That is fine: an unrecognised idea key is a *warning*, not
an error (``bin/gen-roadmap.py:308``), and ``write_document`` round-trips it, so
writing ``discord:`` works today and stops warning once b2 lands. Nothing here
depends on b2.

The hard rules are assertions in the write path, not comments: every one of
them raises :class:`ForbiddenWrite` **before** any HTTP request is made.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

# Endpoint paths, so callers do not spell them by hand.
API_DATA = "/api/data"
API_SAVE = "/api/save"
API_IDEA_COMMENT = "/api/idea-comment"
API_LOGIN = "/api/login"

SESSION_COOKIE = "roadmap_session"
JSON_CONTENT_TYPE = "application/json"
JSON_HEADERS = {"Content-Type": JSON_CONTENT_TYPE, "Accept": JSON_CONTENT_TYPE}

#: Server-side cap on one comment (``roadmap-editor.py:2049``). Mirrored only so
#: a caller can check before posting; the server truncates regardless.
COMMENT_MAX_LEN = 3000

#: Statuses the bot must never write. ``awarded``/``implemented`` are shipping
#: calls and ``manual`` claims a human finishing step exists; all three are the
#: admin's. The full list of ten lives at ``bin/gen-roadmap.py:73``.
FORBIDDEN_STATUSES = frozenset({"awarded", "implemented", "manual"})

#: Fields the bot must never write at all. ``merit_awarded`` records that the
#: in-game merit DB was really credited; only the editor's Award/Revoke buttons
#: may move it (``roadmap-editor.py:76``).
FORBIDDEN_FIELDS = frozenset({"merit_awarded"})

#: Top-level roadmap.yaml blocks the bot must never send. Omitting the key is
#: what makes ``write_document`` leave the block alone.
FORBIDDEN_BLOCKS = frozenset({
    "meta", "groups", "players", "epics", "redemption", "housing",
})

#: The complete set of keys a save payload from this client may carry. Anything
#: else — a stray ``players``, a ``force`` — is a forbidden write.
SAVE_PAYLOAD_KEYS = frozenset({"ideas", "base_version", "base_hashes"})

#: An idea is only well-formed with these (``bin/gen-roadmap.py`` validate()).
REQUIRED_IDEA_FIELDS = ("id", "title", "group", "status")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class RoadmapError(Exception):
    """Base class for every failure this module raises."""


class ForbiddenWrite(RoadmapError):
    """A mutation broke one of the hard rules. Raised before any HTTP request.

    This is a bug in the caller, never a server condition: nothing that raises
    it has touched the network.
    """


class RoadmapAuthError(RoadmapError):
    """Not logged in, session expired, or the login itself was refused (401)."""


class RoadmapPermissionError(RoadmapError):
    """The account's role lacks the capability this route needs (403)."""


class RoadmapHTTPError(RoadmapError):
    """An HTTP status the client does not know how to interpret."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status


class SaveRejected(RoadmapError):
    """``/api/save`` answered ``ok: false`` with validation errors."""

    def __init__(self, errors: Sequence[str], warnings: Sequence[str] = ()) -> None:
        super().__init__("; ".join(errors) or "save rejected")
        self.errors = list(errors)
        self.warnings = list(warnings)


class SaveConflict(RoadmapError):
    """Two conflicting saves in a row: the caller must queue this for review.

    The client re-fetches and retries exactly once. A second conflict means the
    same idea is being edited on both sides right now, and forcing would clobber
    a human's edit — so the client stops and hands the decision back. Never
    resolve this by re-sending with ``force``.
    """

    def __init__(self, message: str, overlap: Sequence[str] = (),
                 version: str | None = None, attempts: int = 2) -> None:
        super().__init__(message or "roadmap.yaml conflict, needs review")
        self.overlap = list(overlap)
        self.version = version
        self.attempts = attempts
        self.needs_review = True


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
class _Secret:
    """A string that cannot be printed.

    ``repr`` and ``str`` both hide it, so a password can never reach a log line,
    a traceback frame summary or an exception message by accident. ``.reveal()``
    is the single, greppable place it comes back out.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = str(value or "")

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<hidden>"

    __str__ = __repr__


@dataclass(frozen=True)
class Snapshot:
    """One ``GET /api/data`` response.

    ``base_hashes``/``version`` are the server's own merge baseline and are kept
    byte-identical for the echo back on save. Nothing here is recomputed.
    """

    ideas: list[dict]
    vocab: dict
    base_hashes: dict
    version: str
    environments: dict = field(default_factory=dict)
    base_vocab: dict = field(default_factory=dict)
    me: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Snapshot":
        return cls(
            ideas=list(payload.get("ideas") or []),
            vocab=dict(payload.get("vocab") or {}),
            base_hashes=payload.get("base_hashes") or {},
            version=payload.get("version") or "",
            environments=dict(payload.get("environments") or {}),
            base_vocab=payload.get("base_vocab") or {},
            me=dict(payload.get("me") or {}),
        )

    @property
    def by_id(self) -> dict[str, dict]:
        return {i["id"]: i for i in self.ideas if isinstance(i, dict) and i.get("id")}

    @property
    def known_players(self) -> frozenset[str]:
        """Names already on the roster or already used by an idea.

        ``vocab()`` (``roadmap-editor.py:337``) unions ``players:`` with every
        name an idea already carries, so writing a name outside this set is
        exactly what "adding a name to ``players:``" looks like from here.
        """
        names = set(self.vocab.get("players") or ())
        names |= {i["player"] for i in self.ideas
                  if isinstance(i, dict) and i.get("player")}
        return frozenset(names)


@dataclass(frozen=True)
class SaveResult:
    """A successful ``/api/save``."""

    version: str
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    attempts: int = 1


# --------------------------------------------------------------------------
# The assertions — every one of these runs before a socket is touched
# --------------------------------------------------------------------------
def _is_true(value: Any) -> bool:
    """The editor's own truthiness for ``merit_awarded`` (``roadmap_auth.py``)."""
    return value is True or str(value).strip().lower() in ("1", "true", "yes")


def assert_ideas_writable(baseline: Mapping[str, dict], ideas: Sequence[dict]) -> None:
    """Raise :class:`ForbiddenWrite` if this ideas array breaks a hard rule.

    ``baseline`` is ``{id: idea}`` as the server last handed it over. The rules
    are about *changes*: the array carries every idea in the document, including
    ones already ``awarded`` with ``merit_awarded: true``, so the test is never
    "does this value appear" but "did the bot move it".
    """
    if not isinstance(ideas, (list, tuple)):
        raise ForbiddenWrite(f"ideas must be a list, got {type(ideas).__name__}")

    seen: set[str] = set()
    for idea in ideas:
        if not isinstance(idea, dict):
            raise ForbiddenWrite(f"idea must be a mapping, got {type(idea).__name__}")
        iid = idea.get("id")
        if not iid or not isinstance(iid, str):
            raise ForbiddenWrite(f"idea has no usable id: {idea.get('title')!r}")
        if iid in seen:
            raise ForbiddenWrite(f"'{iid}': duplicate id in the ideas array")
        seen.add(iid)
        old = baseline.get(iid)

        # Never write status: awarded|implemented|manual.
        status = idea.get("status")
        if status in FORBIDDEN_STATUSES and (old or {}).get("status") != status:
            raise ForbiddenWrite(
                f"'{iid}': refusing to write status {status!r} — shipping and "
                f"merit are the admin's call")

        # Never write merit_awarded (nor introduce it on a new idea).
        for name in FORBIDDEN_FIELDS:
            if _is_true(idea.get(name)) != _is_true((old or {}).get(name)):
                raise ForbiddenWrite(
                    f"'{iid}': refusing to write {name!r} — only the editor's "
                    f"Award/Revoke buttons may move it")

        # Never change `type` on an idea that already exists. There is no
        # Exploit forum tag ([r3]): an exploit is filed as a Defect and the
        # admin promotes it to Exploit (1 merit -> 3) in the editor. Rewriting
        # `type` on an existing idea is exactly how that promotion would be
        # silently undone, so it is refused on the wire as well as at planning
        # time (`sync.CREATION_ONLY_FIELDS`).
        if old is not None and "type" in idea and idea.get("type") != old.get("type"):
            raise ForbiddenWrite(
                f"'{iid}': refusing to change type from {old.get('type')!r} to "
                f"{idea.get('type')!r} — type is written once, at creation. "
                f"Promoting a Defect to an Exploit is the admin's call and the "
                f"bot must never revert it")

        # Never touch a top-level document block by smuggling it onto an idea.
        for name in FORBIDDEN_BLOCKS:
            if name in idea and name not in (old or {}):
                raise ForbiddenWrite(
                    f"'{iid}': refusing to add document block {name!r} to an idea")

    # Never delete: the whole array is posted and what is missing is removed.
    missing = sorted(set(baseline) - seen)
    if missing:
        raise ForbiddenWrite(
            f"refusing to drop {len(missing)} existing idea(s) from the ideas "
            f"array (a missing id is a delete): {', '.join(missing[:5])}")


def assert_players_unchanged(known: Iterable[str], baseline: Mapping[str, dict],
                             ideas: Sequence[dict]) -> None:
    """Raise if any idea would introduce a player name the roster does not have.

    An unrecognised Discord author is a review item, never an automatic roster
    addition — and because ``vocab()`` unions the roster with the names ideas
    use, writing a new name *is* adding one.
    """
    roster = frozenset(known)
    for idea in ideas:
        name = idea.get("player")
        if not name:
            continue
        if name in roster:
            continue
        if name == (baseline.get(idea.get("id")) or {}).get("player"):
            continue  # already on this idea when the server handed it over
        raise ForbiddenWrite(
            f"'{idea.get('id')}': player {name!r} is not on the roster — an "
            f"unrecognised author is a review item, never an automatic add")


def assert_save_payload(payload: Mapping[str, Any]) -> None:
    """Raise unless the payload is exactly ideas + the server's own baseline."""
    extra = sorted(set(payload) - SAVE_PAYLOAD_KEYS)
    if extra:
        blocks = sorted(set(extra) & FORBIDDEN_BLOCKS)
        why = (f"refusing to send document block(s) {', '.join(blocks)}"
               if blocks else f"unexpected save key(s) {', '.join(extra)}")
        raise ForbiddenWrite(f"{why}; a save posts only {sorted(SAVE_PAYLOAD_KEYS)}")
    for name in ("ideas", "base_version", "base_hashes"):
        if name not in payload:
            raise ForbiddenWrite(f"save payload is missing {name!r}")
    if not isinstance(payload["base_hashes"], dict):
        raise ForbiddenWrite(
            "base_hashes must be the server's own map, echoed back verbatim")


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------
class RoadmapClient:
    """Async client for the roadmap editor.

    The transport is injectable: pass any object with aiohttp's
    ``request(method, url, *, json=None, headers=None)`` async-context-manager
    shape as ``session``. Tests inject a fake, so the suite never opens a
    socket; production passes an ``aiohttp.ClientSession`` (created lazily by
    :meth:`__aenter__` if none is given), whose cookie jar carries the
    ``roadmap_session`` cookie automatically.
    """

    def __init__(self, base_url: str, username: str, password: str, *,
                 session: Any = None) -> None:
        if not base_url:
            raise ValueError("base_url is required (ROADMAP_BASE_URL)")
        self.base_url = base_url.rstrip("/")
        self.username = (username or "").strip().lower()
        self._password = _Secret(password)
        self._session = session
        self._owns_session = False
        self._logged_in = False
        self.me: dict = {}

    # -- construction ------------------------------------------------------
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *,
                 session: Any = None) -> "RoadmapClient":
        """Build from the environment. Credentials live in ``.env``, only ever."""
        env = os.environ if env is None else env
        base = env.get("ROADMAP_BASE_URL") or ""
        user = env.get("ROADMAP_USER") or ""
        password = env.get("ROADMAP_PASSWORD") or ""
        missing = [n for n, v in (("ROADMAP_BASE_URL", base),
                                  ("ROADMAP_USER", user),
                                  ("ROADMAP_PASSWORD", password)) if not v]
        if missing:
            raise RoadmapError(f"missing config: {', '.join(missing)}")
        return cls(base, user, password, session=session)

    def __repr__(self) -> str:
        # Never interpolate the password, not even via a field that holds it.
        return (f"<RoadmapClient {self.username}@{self.base_url} "
                f"{'authenticated' if self._logged_in else 'anonymous'}>")

    async def __aenter__(self) -> "RoadmapClient":
        if self._session is None:
            import aiohttp  # imported lazily: injected transports need no aiohttp

            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._owns_session = False

    # -- transport ---------------------------------------------------------
    async def _request(self, method: str, path: str,
                       payload: Mapping[str, Any] | None = None) -> dict:
        if self._session is None:
            raise RoadmapError(
                "no transport: use `async with RoadmapClient(...)` or inject a session")
        headers = dict(JSON_HEADERS)
        # _csrf_ok() (roadmap-editor.py:2948) refuses anything that is not
        # declared application/json, so this is load-bearing, not cosmetic.
        assert headers["Content-Type"] == JSON_CONTENT_TYPE, "JSON content type required"
        url = f"{self.base_url}{path}"
        async with self._session.request(method, url, json=payload,
                                         headers=headers) as resp:
            status = getattr(resp, "status", 200)
            try:
                body = await resp.json(content_type=None)
            except TypeError:  # a fake whose json() takes no kwargs
                body = await resp.json()
            except Exception as exc:  # not JSON at all — a proxy or an outage
                raise RoadmapHTTPError(status, f"non-JSON response: {exc}") from None
        if not isinstance(body, dict):
            raise RoadmapHTTPError(status, "response was not a JSON object")
        return self._check_status(status, body)

    def _check_status(self, status: int, body: Mapping[str, Any]) -> dict:
        message = str(body.get("message") or "; ".join(body.get("errors") or ()) or "")
        if status == 401:
            self._logged_in = False
            raise RoadmapAuthError(message or "not authenticated")
        if status == 403:
            raise RoadmapPermissionError(message or "forbidden")
        if status >= 400:
            raise RoadmapHTTPError(status, message)
        return dict(body)

    # -- API ---------------------------------------------------------------
    async def login(self) -> dict:
        """``POST /api/login`` — exchange the credentials for the session cookie.

        The password reaches exactly one place: this payload. It is never put in
        a message, a repr or a log line, and a failure reports only the server's
        own wording.
        """
        try:
            body = await self._request(
                "POST", API_LOGIN,
                {"username": self.username, "password": self._password.reveal()})
        except RoadmapAuthError:
            raise RoadmapAuthError(
                f"login refused for {self.username!r}: incorrect username or password"
            ) from None
        if not body.get("ok"):
            raise RoadmapAuthError(
                f"login refused for {self.username!r}: "
                f"{body.get('message') or 'unknown reason'}")
        self._logged_in = True
        self.me = dict(body.get("me") or {})
        return self.me

    async def fetch(self) -> Snapshot:
        """``GET /api/data`` — the whole document plus the server's baseline."""
        return Snapshot.from_payload(await self._request("GET", API_DATA))

    async def save(self, mutate: Callable[[list[dict]], Any], *,
                   snapshot: Snapshot | None = None) -> SaveResult:
        """Apply ``mutate`` to a fresh copy of the ideas array and post it.

        ``mutate`` takes the ideas list (a deep copy — the snapshot is never
        mutated) and either edits it in place or returns a replacement.

        The posted ``base_version`` and ``base_hashes`` are the server's own,
        echoed back verbatim; nothing is fingerprinted here. On a conflict the
        snapshot is re-fetched and the mutation re-applied **once**. A second
        conflict raises :class:`SaveConflict` for the caller to queue — this
        client never sends ``force``.
        """
        snap = snapshot if snapshot is not None else await self.fetch()
        for attempt in (1, 2):
            ideas = copy.deepcopy(snap.ideas)
            returned = mutate(ideas)
            if returned is not None:
                ideas = returned

            # Every hard rule, before any request goes out.
            baseline = snap.by_id
            assert_ideas_writable(baseline, ideas)
            assert_players_unchanged(snap.known_players, baseline, ideas)
            payload = {"ideas": list(ideas),
                       "base_version": snap.version,
                       "base_hashes": snap.base_hashes}
            assert_save_payload(payload)

            body = await self._request("POST", API_SAVE, payload)
            if body.get("ok"):
                return SaveResult(version=str(body.get("version") or ""),
                                  warnings=list(body.get("warnings") or ()),
                                  message=str(body.get("message") or ""),
                                  attempts=attempt)
            if body.get("conflict"):
                if attempt == 2:
                    raise SaveConflict(str(body.get("message") or ""),
                                       overlap=body.get("overlap") or (),
                                       version=body.get("version"),
                                       attempts=attempt)
                snap = await self.fetch()
                continue
            raise SaveRejected(body.get("errors") or [body.get("message") or "save rejected"],
                               body.get("warnings") or [])
        raise AssertionError("unreachable")  # pragma: no cover

    async def comment(self, idea_id: str, text: str) -> dict:
        """``POST /api/idea-comment`` — append to the internal ``comments`` list.

        Author and date are stamped server-side and the list is append-only, so
        this can neither rewrite history nor touch ``notes``, which is the
        admin's player-facing field.
        """
        if not idea_id:
            raise ForbiddenWrite("comment needs an idea id")
        body = (text or "").strip()
        if not body:
            raise ForbiddenWrite("refusing to post an empty comment")
        return await self._request("POST", API_IDEA_COMMENT,
                                   {"id": idea_id, "text": body})

    async def new_idea(self, idea: MutableMapping[str, Any], *,
                       snapshot: Snapshot | None = None) -> SaveResult:
        """Append one new idea through :meth:`save`.

        Same assertions as any other save — a new idea may not arrive already
        ``awarded``, already carrying ``merit_awarded``, or crediting a player
        the roster has never heard of.
        """
        if not isinstance(idea, MutableMapping):
            raise ForbiddenWrite(f"idea must be a mapping, got {type(idea).__name__}")
        record = dict(idea)
        missing = [f for f in REQUIRED_IDEA_FIELDS if not record.get(f)]
        if missing:
            raise ForbiddenWrite(f"new idea is missing {', '.join(missing)}")
        snap = snapshot if snapshot is not None else await self.fetch()
        if record["id"] in snap.by_id:
            raise ForbiddenWrite(f"idea id {record['id']!r} already exists")

        def _append(ideas: list[dict]) -> None:
            ideas.append(copy.deepcopy(record))

        return await self.save(_append, snapshot=snap)


__all__ = [
    "API_DATA",
    "API_IDEA_COMMENT",
    "API_LOGIN",
    "API_SAVE",
    "COMMENT_MAX_LEN",
    "FORBIDDEN_BLOCKS",
    "FORBIDDEN_FIELDS",
    "FORBIDDEN_STATUSES",
    "ForbiddenWrite",
    "JSON_CONTENT_TYPE",
    "JSON_HEADERS",
    "REQUIRED_IDEA_FIELDS",
    "RoadmapAuthError",
    "RoadmapClient",
    "RoadmapError",
    "RoadmapHTTPError",
    "RoadmapPermissionError",
    "SAVE_PAYLOAD_KEYS",
    "SESSION_COOKIE",
    "SaveConflict",
    "SaveRejected",
    "SaveResult",
    "Snapshot",
    "assert_ideas_writable",
    "assert_players_unchanged",
    "assert_save_payload",
]
