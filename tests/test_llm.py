"""Pricing and the conversion of real SDK responses into ModelResponse."""

import pytest
from anthropic.types import Message

from gridagent.llm.base import TokenUsage
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
