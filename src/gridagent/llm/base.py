"""The interface between the agent loop and any model.

The loop only needs one operation: "given system prompt, tools and the
conversation so far, give me the next assistant message". Hiding the
Anthropic SDK behind this interface lets tests use a scripted FakeLLM,
so the loop can be tested offline, for free and deterministically.

Content blocks are plain dicts in the Messages API shape, e.g.
    {"type": "text", "text": "..."}
    {"type": "tool_use", "id": "toolu_1", "name": "compute_stats", "input": {...}}
They are appended to the history unchanged (including `thinking` blocks).
"""

from typing import Any, Protocol

from pydantic import BaseModel


class TokenUsage(BaseModel):
    """Token counts reported for one model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


class ModelResponse(BaseModel):
    """One assistant turn returned by a model."""

    content: list[dict[str, Any]]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | "refusal" | "pause_turn" | ...
    usage: TokenUsage
    model: str  # the model that actually served the request (may differ after a fallback)
    request_id: str | None = None

    def text(self) -> str:
        """All text blocks joined (thinking blocks are not user-facing)."""
        return "\n".join(b["text"] for b in self.content if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLM(Protocol):
    """Anything that can produce the next assistant turn."""

    model_name: str

    def create(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelResponse: ...


class LLMError(Exception):
    """The model call failed in a way the loop should report, not crash on."""
