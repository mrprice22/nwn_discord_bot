"""Local sqlite state: thread<->idea links, content hashes, review queue.

Owned by ``[b6-sync]``. It holds:

- the thread id <-> idea id link (the mirror of the idea's ``discord`` field,
  kept locally so a planner can run without a round-trip);
- a content hash per ``(idea_id, field)``, the second of the three
  loop-prevention layers: an unchanged value plans no action;
- the review queue (unmatched authors, renamed threads, conflicting saves,
  duplicate proposals) that the CLI reports;
- decisions the admin made in the editor that the bot must not undo, e.g. a
  ``dupe_of`` removed by hand and therefore never re-added.

The store is pure local state; it is never the source of truth for anything
the roadmap or Discord already knows.

**Division of labour.** :class:`Store` does sqlite I/O; the planners in
:mod:`nwnbot.sync` do not. They take :class:`StoreView` — an immutable, already
-loaded snapshot of everything above — which is what keeps them pure and their
tests free of a database. Read with :meth:`Store.view`, write back with
:meth:`Store.record` once an action has actually been applied.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from nwnbot.config import DEFAULT_DB_PATH

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    thread_id  TEXT PRIMARY KEY,
    idea_id    TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL DEFAULT '',
    linked_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hashes (
    idea_id    TEXT NOT NULL,
    field      TEXT NOT NULL,
    hash       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (idea_id, field)
);
CREATE TABLE IF NOT EXISTS review (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    value      TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: An admin decision the bot must not undo: a ``dupe_of`` it wrote and a human
#: then removed. ``[b9-dupes]`` reads this before proposing a merge again.
DECISION_DUPE_REMOVED = "dupe_of_removed"


def content_hash(value: Any) -> str:
    """The stable fingerprint of one field value — loop-prevention layer two.

    Whitespace-normalised so a value that only changed by reflow plans nothing.
    Local only: the roadmap's own ``base_hashes`` are the server's business and
    are never recomputed here (see :mod:`nwnbot.roadmap`).
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = " ".join(text.split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReviewEntry:
    """One queued question for the human. ``key`` is its identity."""

    key: str
    kind: str
    subject: str = ""
    detail: str = ""
    status: str = "open"
    created_at: str = ""


@dataclass(frozen=True)
class StoreView:
    """Immutable read-only view of the store, as the planners consume it.

    Everything a planner needs is already loaded: no cursor, no connection, no
    lazy query. Build one with :meth:`Store.view`, or by hand in a test.
    """

    links: Mapping[str, str] = field(default_factory=dict)          # thread -> idea
    hashes: Mapping[tuple[str, str], str] = field(default_factory=dict)
    reviewed: frozenset[str] = frozenset()
    decisions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "links", dict(self.links))
        object.__setattr__(self, "hashes", {tuple(k): v for k, v in dict(self.hashes).items()})
        object.__setattr__(self, "reviewed", frozenset(self.reviewed))
        object.__setattr__(self, "decisions", dict(self.decisions))

    @classmethod
    def empty(cls) -> "StoreView":
        return cls()

    # -- links -------------------------------------------------------------
    def idea_for_thread(self, thread_id: str) -> str | None:
        return self.links.get(thread_id)

    def thread_for_idea(self, idea_id: str) -> str | None:
        for thread_id, iid in self.links.items():
            if iid == idea_id:
                return thread_id
        return None

    # -- hashes ------------------------------------------------------------
    def hash_of(self, idea_id: str, field_name: str) -> str | None:
        return self.hashes.get((idea_id, field_name))

    def unchanged(self, idea_id: str, field_name: str, value: Any) -> bool:
        """True when this field has already been synced at this exact value."""
        return self.hash_of(idea_id, field_name) == content_hash(value)

    def seen(self, idea_id: str, field_name: str) -> bool:
        """True when this field has ever been synced, whatever the value."""
        return (idea_id, field_name) in self.hashes

    # -- review queue and admin decisions ----------------------------------
    def has_review(self, key: str) -> bool:
        return key in self.reviewed

    def decision(self, kind: str, subject: str) -> str | None:
        return self.decisions.get(f"{kind}:{subject}")

    # -- functional update, for dry-run simulation -------------------------
    def evolve(self, *, links: Mapping[str, str] | None = None,
               hashes: Mapping[tuple[str, str], str] | None = None,
               reviewed: Iterable[str] = (),
               decisions: Mapping[str, str] | None = None) -> "StoreView":
        """A copy with extra rows merged in. Pure: nothing is written anywhere."""
        return StoreView(
            links={**self.links, **dict(links or {})},
            hashes={**self.hashes, **dict(hashes or {})},
            reviewed=self.reviewed | frozenset(reviewed),
            decisions={**self.decisions, **dict(decisions or {})},
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """The sqlite side. Never used from inside a planner.

    ``path`` defaults to ``$NWNBOT_DB`` / ``state.db``; tests always pass a
    ``tmp_path``, and ``":memory:"`` works for a throwaway.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<Store {self.path}>"

    # -- links -------------------------------------------------------------
    def link_thread(self, thread_id: str, idea_id: str, channel_id: str = "") -> None:
        if not thread_id or not idea_id:
            raise ValueError("both thread_id and idea_id are required to link")
        self._conn.execute(
            "INSERT INTO links (thread_id, idea_id, channel_id, linked_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET "
            "idea_id=excluded.idea_id, channel_id=excluded.channel_id",
            (thread_id, idea_id, channel_id, _now()))
        self._conn.commit()

    def idea_for_thread(self, thread_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT idea_id FROM links WHERE thread_id = ?", (thread_id,)).fetchone()
        return row["idea_id"] if row else None

    def thread_for_idea(self, idea_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT thread_id FROM links WHERE idea_id = ?", (idea_id,)).fetchone()
        return row["thread_id"] if row else None

    def links(self) -> dict[str, str]:
        return {r["thread_id"]: r["idea_id"]
                for r in self._conn.execute("SELECT thread_id, idea_id FROM links")}

    # -- hashes ------------------------------------------------------------
    def set_hash(self, idea_id: str, field_name: str, value: Any) -> str:
        digest = content_hash(value)
        self._conn.execute(
            "INSERT INTO hashes (idea_id, field, hash, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(idea_id, field) DO UPDATE SET "
            "hash=excluded.hash, updated_at=excluded.updated_at",
            (idea_id, field_name, digest, _now()))
        self._conn.commit()
        return digest

    def get_hash(self, idea_id: str, field_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT hash FROM hashes WHERE idea_id = ? AND field = ?",
            (idea_id, field_name)).fetchone()
        return row["hash"] if row else None

    def hashes(self) -> dict[tuple[str, str], str]:
        return {(r["idea_id"], r["field"]): r["hash"]
                for r in self._conn.execute("SELECT idea_id, field, hash FROM hashes")}

    # -- review queue ------------------------------------------------------
    def queue_review(self, key: str, kind: str, subject: str = "",
                     detail: str = "") -> bool:
        """Record a question for the human. Returns False if already queued."""
        if not key:
            raise ValueError("a review entry needs a key")
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO review (key, kind, subject, detail, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)", (key, kind, subject, detail, _now()))
        self._conn.commit()
        return cur.rowcount > 0

    def resolve_review(self, key: str) -> None:
        self._conn.execute("UPDATE review SET status = 'resolved' WHERE key = ?", (key,))
        self._conn.commit()

    def reviews(self, status: str | None = "open") -> list[ReviewEntry]:
        sql = "SELECT key, kind, subject, detail, status, created_at FROM review"
        args: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY created_at, key"
        return [ReviewEntry(**dict(r)) for r in self._conn.execute(sql, args)]

    def review_keys(self, status: str | None = "open") -> frozenset[str]:
        return frozenset(e.key for e in self.reviews(status))

    # -- admin decisions ---------------------------------------------------
    def record_decision(self, kind: str, subject: str, value: str = "") -> None:
        """Remember something the admin did that the bot must not undo."""
        self._conn.execute(
            "INSERT INTO decisions (key, kind, subject, value, decided_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, decided_at=excluded.decided_at",
            (f"{kind}:{subject}", kind, subject, value, _now()))
        self._conn.commit()

    def decision(self, kind: str, subject: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM decisions WHERE key = ?", (f"{kind}:{subject}",)).fetchone()
        return row["value"] if row else None

    def decisions(self) -> dict[str, str]:
        return {r["key"]: r["value"]
                for r in self._conn.execute("SELECT key, value FROM decisions")}

    # -- the planner boundary ---------------------------------------------
    def view(self) -> StoreView:
        """Load everything the planners read, in one go, as immutable data."""
        return StoreView(links=self.links(), hashes=self.hashes(),
                         reviewed=self.review_keys(None), decisions=self.decisions())

    def record(self, action: Any) -> None:
        """Persist the state one *applied* action leaves behind.

        Called by the executor after an action really happened — never by a
        planner. It reads the action's own ``effects()`` (duck-typed, so this
        module never imports :mod:`nwnbot.sync`), which means a new action type
        needs no change here and the dry-run simulation and the real run can
        never drift.
        """
        effects = getattr(action, "effects", None)
        if not callable(effects):
            return
        eff = effects()
        for thread_id, (idea_id, channel_id) in dict(eff.links).items():
            self.link_thread(thread_id, idea_id, channel_id)
        for (idea_id, field_name), value in dict(eff.hashes).items():
            self.set_hash(idea_id, field_name, value)
        for item in eff.reviews:
            self.queue_review(item.review_key, item.kind, item.subject, item.detail)

    def record_all(self, actions: Iterable[Any]) -> None:
        for action in actions:
            self.record(action)


__all__ = [
    "DECISION_DUPE_REMOVED",
    "DEFAULT_DB_PATH",
    "ReviewEntry",
    "SCHEMA_VERSION",
    "Store",
    "StoreView",
    "content_hash",
]
