"""Pricing and the conversion of real SDK responses into ModelResponse."""

import json

import anthropic
import httpx2 as httpx  # the HTTP client the anthropic SDK is built on
import pytest
from anthropic.types import Message

from gridagent.llm.base import LLMError, TokenUsage
from gridagent.llm.claude import ClaudeLLM, to_model_response
from gridagent.llm.pricing import estimate_cost_usd


def test_cost_sonnet() -> None:
    usage = TokenUsage(
        input_tokens=10_000,
        output_tokens=1_000,
        cache_read_input_tokens=20_000,
        cache_creation_input_tokens=4_000,
    )
    # 10k*2 + 1k*10 + 20k*0.10 + 4k*2.5, per million
    assert estimate_cost_usd("claude-sonnet-5-5", usage) == pytest.approx(0.042)


def test_haiku_is_twenty_times_cheaper_than_sonnet() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=1000)
    sonnet = estimate_cost_usd("claude-sonnet-5-5", usage)
    assert estimate_cost_usd("claude-haiku-5-5", usage) == pytest.approx(sonnet / 20)


def test_unknown_model_cost_is_none() -> None:
    assert estimate_cost_usd("some-new-model", TokenUsage(input_tokens=1)) is None


def test_to_model_response_from_sdk_message() -> None:
    message = Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "content": [
                {"type": "text", "text": "Let me fetch that."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "compute_stats",
                    "input": {"ref": "gen_1"},
                },
            ],
            "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5},
        }
    )
    response = to_model_response(message)
    assert response.stop_reason == "tool_use"
    assert response.text() == "Let me fetch that."
    assert response.tool_uses()[0] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "compute_stats",
        "input": {"ref": "gen_1"},
    }
    assert response.usage == TokenUsage(
        input_tokens=12, output_tokens=34, cache_read_input_tokens=5
    )


def test_fallback_only_enabled_for_supported_models() -> None:
    assert ClaudeLLM("claude-sonnet-5-5", client=object()).use_fallback is True
    assert ClaudeLLM("claude-haiku-5-5", client=object()).use_fallback is False
    assert (
        ClaudeLLM("claude-sonnet-5-5", refusal_fallback=False, client=object()).use_fallback
        is False
    )


# ---------- create(): the real SDK over a fake HTTP transport (no network, no cost) ----------


REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
    "content": [{"type": "text", "text": "Hello."},
                {"type": "tool_use", "id": "tu_1", "name": "get_price", "input": {"day": "x"}}],
    "stop_reason": "tool_use", "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 100,
              "cache_creation_input_tokens": 0},
}  # fmt: skip


def llm_with(handler, model: str = "claude-haiku-5-5", **kwargs) -> tuple[ClaudeLLM, list]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    client = anthropic.Anthropic(
        api_key="test-key", max_retries=0, http_client=httpx.Client(transport=transport)
    )
    return ClaudeLLM(model, client=client, **kwargs), seen


def test_create_sends_caching_and_effort_and_parses_the_reply() -> None:
    llm, seen = llm_with(lambda r: httpx.Response(200, json=REPLY), effort="low")
    tools = [{"name": "get_price", "description": "d", "input_schema": {"type": "object"}}]
    response = llm.create("You are terse.", tools, [{"role": "user", "content": "Hi"}])

    body = json.loads(seen[0].content)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # tools + system cached
    assert body["cache_control"] == {"type": "ephemeral"}  # and the growing conversation
    assert body["output_config"] == {"effort": "low"} and "thinking" not in body
    assert response.stop_reason == "tool_use" and response.text() == "Hello."
    assert response.tool_uses()[0]["input"] == {"day": "x"}
    assert response.usage.cache_read_input_tokens == 100


def test_refusal_fallback_uses_the_beta_endpoint_on_supported_models() -> None:
    llm, seen = llm_with(lambda r: httpx.Response(200, json=REPLY), model="claude-sonnet-5-5")
    llm.create("s", [], [{"role": "user", "content": "Hi"}])
    assert "server-side-fallback" in seen[0].headers.get("anthropic-beta", "")
    assert json.loads(seen[0].content)["fallbacks"] == "default"


@pytest.mark.parametrize(("status", "message"), [
    (401, "Authentication failed"),
    (400, "Bad request"),
    (429, "Rate limited"),
    (529, "HTTP 529"),
])  # fmt: skip
def test_api_errors_become_one_clear_llm_error(status: int, message: str) -> None:
    error = {"type": "error", "error": {"type": "x", "message": "nope"}}
    llm, _ = llm_with(lambda r: httpx.Response(status, json=error))
    with pytest.raises(LLMError, match=message):
        llm.create("s", [], [{"role": "user", "content": "Hi"}])


def test_an_unreachable_api_is_an_llm_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    llm, _ = llm_with(down)
    with pytest.raises(LLMError, match="Could not reach"):
        llm.create("s", [], [{"role": "user", "content": "Hi"}])
