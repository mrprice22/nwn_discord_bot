"""Tests for the LLM judge. No socket is ever opened.

The important ones are the failure modes: this is an optional dependency that
must degrade to silence, and a reasoning model that answers in the wrong field.
"""

import pytest

from nwnbot import config as cfg
from nwnbot import llm


def fake(content=None, *, reasoning=None, model="test-model", raise_=None,
         payload=None, record=None):
    """A transport returning one canned chat response."""
    def transport(path, body):
        if record is not None:
            record.append((path, body))
        if raise_ is not None:
            raise raise_
        if path == "/v1/models":
            return {"data": [{"id": model}]}
        message = {}
        if content is not None:
            message["content"] = content
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        return payload if payload is not None else {
            "model": model, "choices": [{"message": message}]}
    return transport


def client(**kw):
    return llm.LlmClient(model="test-model", transport=fake(**kw))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_duplicate_is_read_from_the_first_word():
    v = client(content="DUPLICATE Both describe the same crash.").judge_duplicate(
        "a", "", "b")
    assert v.same is True
    assert "same crash" in v.why


def test_not_is_read_from_the_first_word():
    assert client(content="NOT Different areas entirely.").judge_duplicate(
        "a", "", "b").same is False


def test_the_answer_is_case_insensitive_and_tolerates_leading_space():
    assert client(content="   duplicate, clearly").judge_duplicate(
        "a", "", "b").same is True


def test_a_rambling_answer_still_parses_on_its_first_word():
    # The one-word-first instruction exists so a truncated answer is still usable.
    v = client(content="NOT because the first concerns the forge and the "
                       "second concerns").judge_duplicate("a", "", "b")
    assert v.same is False


def test_the_model_id_is_returned_for_provenance():
    # So a dupe_candidates row can record what judged it.
    assert client(content="NOT", model="qwen-x").judge_duplicate(
        "a", "", "b").model == "qwen-x"


def test_judge_link_reads_SAME_rather_than_DUPLICATE():
    assert client(content="SAME already tracked").judge_link("t", "", "i").same is True
    assert client(content="DIFFERENT").judge_link("t", "", "i").same is False


# --------------------------------------------------------------------------
# The reasoning-model trap
# --------------------------------------------------------------------------

def test_thinking_is_disabled_on_every_request():
    # THE bug this guards: with thinking on, Qwen3 puts the answer in
    # reasoning_content and leaves content empty, so every verdict parses as a
    # negative and the whole feature looks like it works while measuring nothing.
    record = []
    llm.LlmClient(model="m", transport=fake(content="NOT", record=record)) \
        .judge_duplicate("a", "", "b")
    _, body = record[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_an_answer_that_arrives_only_in_reasoning_content_is_still_read():
    # Belt and braces for a server that ignores the flag.
    v = client(content="", reasoning="DUPLICATE same underlying fault").judge_duplicate(
        "a", "", "b")
    assert v.same is True


def test_temperature_is_zero_so_a_replay_is_deterministic():
    record = []
    llm.LlmClient(model="m", transport=fake(content="NOT", record=record)) \
        .judge_duplicate("a", "", "b")
    assert record[0][1]["temperature"] == 0


# --------------------------------------------------------------------------
# Degradation — none of these may raise
# --------------------------------------------------------------------------

def test_an_unreachable_model_is_no_opinion_not_an_error():
    # The bot must never stop syncing Discord because a local model is down.
    assert client(raise_=llm.LlmUnavailable("connection refused")).judge_duplicate(
        "a", "", "b") is None


def test_an_empty_answer_is_no_opinion():
    assert client(content="").judge_duplicate("a", "", "b") is None


def test_a_malformed_response_is_no_opinion():
    assert client(payload={"nonsense": True}).judge_duplicate("a", "", "b") is None


def test_an_empty_choices_list_is_no_opinion():
    assert client(payload={"choices": []}).judge_duplicate("a", "", "b") is None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def test_no_base_url_means_the_feature_is_simply_off():
    assert llm.from_env({}) is None


def test_a_base_url_is_enough():
    c = llm.from_env({cfg.ENV_LLM_BASE_URL: "http://127.0.0.1:8080"})
    assert c.base_url == "http://127.0.0.1:8080"


def test_a_trailing_slash_is_trimmed():
    assert llm.from_env({cfg.ENV_LLM_BASE_URL: "http://x:8080/"}).base_url == "http://x:8080"


def test_the_api_key_is_not_in_the_repr():
    c = llm.LlmClient(base_url="http://x", api_key="SUPERSECRETVALUE")
    assert "SUPERSECRETVALUE" not in repr(c)
    assert "<set>" in repr(c)


def test_models_lists_what_the_server_serves():
    assert client(model="qwen-x").models() == ["qwen-x"]


def test_models_raises_rather_than_returning_nothing():
    # doctor wants to distinguish "no models" from "cannot reach the server".
    with pytest.raises(llm.LlmUnavailable):
        client(raise_=llm.LlmUnavailable("refused")).models()
