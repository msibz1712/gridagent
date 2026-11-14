"""A scripted stand-in for Claude, used in tests and offline evaluations.

You give it the assistant turns it should return, in order. It also records
every request, so tests can check what the loop sent (e.g. tool results).

    fake = FakeLLM([
        [tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"})],
        [text("The average price was ...")],
    ])
"""

from typing import Any

from gridagent.llm.base import LLMError, ModelResponse, TokenUsage


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, tool_input: dict[str, Any], tool_id: str | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id or f"toolu_{name}", "name": name, "input": tool_input}


class FakeLLM:
    """Returns scripted turns; stop_reason is derived from the blocks."""

    def __init__(
        self,
        turns: list[list[dict[str, Any]] | ModelResponse | Exception],
        model_name: str = "fake-model",
        usage: TokenUsage | None = None,
    ) -> None:
        self.turns = list(turns)
        self.model_name = model_name
        self.usage = usage or TokenUsage(input_tokens=100, output_tokens=20)
        self.requests: list[dict[str, Any]] = []

    def create(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelResponse:
        # Copy messages: the loop keeps appending to the same list afterwards.
        self.requests.append({"system": system, "tools": tools, "messages": list(messages)})
        if not self.turns:
            raise LLMError("FakeLLM script exhausted")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        if isinstance(turn, ModelResponse):
            return turn
        has_tool_use = any(b["type"] == "tool_use" for b in turn)
        return ModelResponse(
            content=turn,
            stop_reason="tool_use" if has_tool_use else "end_turn",
            usage=self.usage,
            model=self.model_name,
        )
