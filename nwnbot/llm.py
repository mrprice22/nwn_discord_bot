"""The local model, asked one question: are these two items the same work?

A thin OpenAI-compatible client for the llama.cpp server on this box. It exists
because token overlap is good at restatements and blind to synonyms — the
measurements are in ``future-llm-dupe-matching.md`` and are not re-argued here.

Four things are deliberate, and three of them cost a real bug each if dropped:

**Thinking is off.** Qwen3.6 is a reasoning model. With thinking on, the answer
arrives in ``reasoning_content`` and ``content`` comes back *empty* — a parser
reading ``content`` sees "" for every pair, scores each as a negative, and looks
plausible while measuring nothing at all. That happened during the first
measurement here. It is also twice as fast off: ~2.0s per pair rather than ~4.5.

**There is an explicit timeout.** Nothing else in this package sets one, because
nothing else talks to a dependency that can hang. A model loading 19GB, or
swapping, can hang for minutes.

**A failure is not an error.** :meth:`LlmClient.judge` returns ``None`` when the
model cannot answer, and every caller treats ``None`` as "no opinion" and falls
back to the token scorer. The bot must never stop syncing Discord because a
local model is down.

**The call happens outside the planners.** They are pure and synchronous, and a
network call inside one would end that — see :mod:`nwnbot.sync`. Verdicts are
computed up front and handed in as data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

log = logging.getLogger("nwnbot")

#: What the judge is asked. Deliberately narrow: one word first, so a truncated
#: or rambling answer still parses, and the reasoning is a bonus rather than the
#: payload. "Work that one fix would complete" is the admin's own test — the
#: same area is not the same item.
JUDGE_SYSTEM = (
    "You judge whether two game-roadmap items describe the SAME underlying "
    "issue. The same symptom, or work that one fix would complete, is "
    "DUPLICATE. The same area, feature or theme but separate pieces of work is "
    "NOT. Reply with exactly one word first: DUPLICATE or NOT. Then one short "
    "sentence of reasoning."
)

#: Asked when matching a Discord thread to an existing roadmap item. A narrower
#: question than duplicate-hunting: these are the SAME report, one written in a
#: forum and one already logged, so titles diverge more than wording usually
#: does and the bar is "is this the same piece of work", not "is this similar".
LINK_SYSTEM = (
    "A player reported something in a Discord thread. You are given the thread "
    "title and an existing roadmap item. Decide whether the roadmap item is "
    "ALREADY tracking that report. Reply with exactly one word first: SAME or "
    "DIFFERENT. Then one short sentence of reasoning."
)

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 100


class LlmUnavailable(Exception):
    """The model could not be reached or could not answer."""


@dataclass(frozen=True)
class Verdict:
    """One judgement. ``same`` is the answer; ``why`` is for the human."""

    same: bool
    why: str = ""
    model: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.same


class LlmClient:
    """Talks to an OpenAI-compatible ``/v1`` endpoint.

    ``transport`` is injectable for the same reason :class:`RoadmapClient`'s is:
    the tests must never open a socket, and a fake keeps the judging logic
    testable without a 19GB model.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, model: str = "", *,
                 timeout: float = DEFAULT_TIMEOUT, transport: Any = None,
                 api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self._api_key = api_key

    def __repr__(self) -> str:
        return (f"LlmClient(base_url={self.base_url!r}, model={self.model!r}, "
                f"api_key={'<set>' if self._api_key else '<none>'})")

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, payload: Mapping[str, Any]) -> dict:
        if self._transport is not None:
            return self._transport(path, payload)
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(self.base_url + path,
                                     data=json.dumps(payload).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmUnavailable(f"{self.base_url}{path}: {exc}") from exc

    def models(self) -> list[str]:
        """Which models the server is serving. Used by ``doctor``."""
        if self._transport is not None:
            data = self._transport("/v1/models", None)
        else:
            import urllib.error
            import urllib.request
            try:
                with urllib.request.urlopen(self.base_url + "/v1/models",
                                            timeout=min(self.timeout, 10)) as r:
                    data = json.load(r)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise LlmUnavailable(f"{self.base_url}/v1/models: {exc}") from exc
        return [str(m.get("id") or "") for m in (data.get("data") or [])]

    # -- judging -----------------------------------------------------------
    def _ask(self, system: str, user: str, positive: str) -> Verdict | None:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": DEFAULT_MAX_TOKENS,
            # See the module docstring: without this the answer lands in
            # `reasoning_content` and `content` is empty.
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        try:
            data = self._post("/v1/chat/completions", payload)
        except LlmUnavailable as exc:
            log.warning("llm unavailable, falling back to the token scorer: %s", exc)
            return None
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            log.warning("llm returned an unreadable response")
            return None
        text = (message.get("content") or message.get("reasoning_content") or "")
        text = " ".join(str(text).split())
        if not text:
            log.warning("llm returned an empty answer")
            return None
        same = text.upper().lstrip().startswith(positive)
        return Verdict(same=same, why=text, model=str(data.get("model") or self.model))

    def judge_duplicate(self, a_title: str, a_body: str,
                        b_title: str, b_body: str = "") -> Verdict | None:
        """Are these two roadmap items the same work? ``None`` = no opinion."""
        user = (f"A: {a_title}\n{(a_body or '')[:600]}\n\n"
                f"B: {b_title}\n{(b_body or '')[:600]}")
        return self._ask(JUDGE_SYSTEM, user, "DUPLICATE")

    def judge_link(self, thread_title: str, thread_body: str,
                   idea_title: str, idea_body: str = "") -> Verdict | None:
        """Is this roadmap item already tracking this Discord thread?"""
        user = (f"Discord thread: {thread_title}\n{(thread_body or '')[:600]}\n\n"
                f"Roadmap item: {idea_title}\n{(idea_body or '')[:600]}")
        return self._ask(LINK_SYSTEM, user, "SAME")


def from_env(env: Mapping[str, str]) -> LlmClient | None:
    """Build a client from the environment, or ``None`` when not configured.

    ``None`` is a supported state: duplicate scoring falls back to the token
    scorer and the run summary says so. Unlike R2, a partial configuration is
    not an error here — a base url alone is enough, because the server reports
    its own model and there is no credential to get half-right.
    """
    from nwnbot import config as cfg

    base = (env.get(cfg.ENV_LLM_BASE_URL) or "").strip()
    if not base:
        return None
    return LlmClient(base_url=base,
                     model=(env.get(cfg.ENV_LLM_MODEL) or "").strip(),
                     api_key=(env.get(cfg.ENV_LLM_API_KEY) or "").strip(),
                     timeout=float(env.get(cfg.ENV_LLM_TIMEOUT) or DEFAULT_TIMEOUT))


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_TIMEOUT", "JUDGE_SYSTEM", "LINK_SYSTEM",
           "LlmClient", "LlmUnavailable", "Verdict", "from_env"]
