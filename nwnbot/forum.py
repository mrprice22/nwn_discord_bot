"""Discord forum access: snapshots in, planned actions out.

This module owns the **read side** for ``[b6-sync]``: plain-data dataclasses
describing a forum channel (threads, their tags, authors, first post and
replies) that the pure planners in :mod:`nwnbot.sync` consume. There is
deliberately **no discord.py import here** — everything below is data, so the
planner tests build snapshots by hand and never touch a socket.

The write side (the executor that applies the planner's action list: create
thread, post message, edit tags, archive/lock, rate-limited with exponential
backoff on 429) arrives with ``[b7-cli-runtime]`` and will adapt a live
``discord.py`` channel into :class:`ForumSnapshot` via :func:`ForumThread.of`
-shaped constructors.

Two forums, two item types: ``#bugs`` implies ``Defect`` and
``#feature-requests`` implies ``Enhancement`` (whether ``Exploit`` can come
from a tag is open review item ``r3``). Channel ids and tag names come from the
environment / ``[b5-config]``; none are hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping

# Minimum delay between thread creations during a batch (seconds), per the
# backfill rate limit in plan.md.
THREAD_CREATE_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class ForumMessage:
    """One message in a forum thread.

    ``author_id`` is the Discord snowflake as a string — the planners compare it
    against the bot's own id (loop-prevention layer one) and look it up in the
    player identity map, so it is never a display name.
    """

    id: str
    author_id: str
    content: str = ""
    author_name: str = ""
    created_at: str = ""          # ISO 8601, as Discord hands it over
    is_starter: bool = False
    edited: bool = False

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ForumMessage needs an id")
        if not self.author_id:
            raise ValueError(f"message {self.id}: author_id is required")


@dataclass(frozen=True)
class ForumThread:
    """One forum post (a thread) and everything the planners need about it."""

    id: str
    channel_id: str
    title: str
    author_id: str = ""
    author_name: str = ""
    created_at: str = ""
    tag_names: tuple[str, ...] = ()
    archived: bool = False
    locked: bool = False
    starter: ForumMessage | None = None
    messages: tuple[ForumMessage, ...] = ()   # replies, oldest first
    url: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ForumThread needs an id")
        if not self.channel_id:
            raise ValueError(f"thread {self.id}: channel_id is required")
        object.__setattr__(self, "tag_names", tuple(self.tag_names))
        object.__setattr__(self, "messages", tuple(self.messages))

    @property
    def all_messages(self) -> tuple[ForumMessage, ...]:
        """Starter first (when present), then the replies in order."""
        return ((self.starter,) if self.starter is not None else ()) + self.messages

    def replies_excluding(self, author_id: str) -> tuple[ForumMessage, ...]:
        """Replies not authored by ``author_id`` — loop-prevention layer one."""
        return tuple(m for m in self.messages if m.author_id != author_id)

    @property
    def body(self) -> str:
        """The opening post's text, or ``""`` when the snapshot omitted it."""
        return self.starter.content if self.starter is not None else ""


@dataclass(frozen=True)
class ForumSnapshot:
    """Everything read out of the forums in one pass.

    ``bot_user_id`` is the bot's own Discord id: the first loop-prevention
    layer skips anything it authored. It is read from the environment, never
    hard-coded.
    """

    threads: tuple[ForumThread, ...] = ()
    bot_user_id: str = ""
    available_tags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "threads", tuple(self.threads))
        object.__setattr__(self, "available_tags",
                           {k: tuple(v) for k, v in dict(self.available_tags).items()})
        seen: set[str] = set()
        for t in self.threads:
            if t.id in seen:
                raise ValueError(f"duplicate thread id in snapshot: {t.id}")
            seen.add(t.id)

    def __iter__(self) -> Iterator[ForumThread]:
        return iter(self.threads)

    def __len__(self) -> int:
        return len(self.threads)

    @property
    def by_id(self) -> dict[str, ForumThread]:
        return {t.id: t for t in self.threads}

    def in_channel(self, channel_id: str) -> tuple[ForumThread, ...]:
        return tuple(t for t in self.threads if t.channel_id == channel_id)

    def with_threads(self, threads: Iterable[ForumThread]) -> "ForumSnapshot":
        """A copy carrying different threads (used by dry-run simulation)."""
        return ForumSnapshot(tuple(threads), self.bot_user_id, self.available_tags)


__all__ = [
    "THREAD_CREATE_DELAY_SECONDS",
    "ForumMessage",
    "ForumSnapshot",
    "ForumThread",
]
