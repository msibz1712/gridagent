"""Structured run logs: one JSON line per agent run.

JSON Lines (one object per line) is easy to append to, to grep, and to load
later for analysis (e.g. `pandas.read_json(path, lines=True)`) or to feed a
monitoring system. Everything an evaluation or a post-mortem needs is here.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from gridagent.llm.base import TokenUsage

RunStatus = Literal["ok", "refused", "max_steps", "timeout", "error"]
RESULT_PREVIEW_CHARS = 20_000  # enough for a 31-day per_day result


class ToolCallLog(BaseModel):
    name: str
    input: dict[str, Any]
    is_error: bool
    latency_s: float
    result: str  # what the model saw, truncated to RESULT_PREVIEW_CHARS


class StepLog(BaseModel):
    """One model call and the tools it requested."""

    step: int
    model: str  # model that served this call
    stop_reason: str
    latency_s: float  # model call only
    usage: TokenUsage
    cost_usd: float | None
    tool_calls: list[ToolCallLog] = []


class RunLog(BaseModel):
    run_id: str
    started_at: datetime
    agent: str
    agent_version: str
    model: str  # requested model
    question: str
    today: str  # the date the agent treated as "today"
    conversation_id: str | None = None  # same id for all questions of one conversation
    turn: int = 1  # 1 = first question, 2 = first follow-up, ...
    status: RunStatus = "ok"
    final_answer: str = ""
    error: str | None = None
    # Numbers in the answer that no tool result or question backs (grounding.py):
    # [] = every number is traceable to data; None = not checked (failed run, old log).
    unsupported_numbers: list[str] | None = None
    steps: list[StepLog] = []
    latency_s: float = 0.0
    usage: TokenUsage = TokenUsage()
    cost_usd: float | None = 0.0

    @property
    def tool_calls(self) -> list[ToolCallLog]:
        return [call for step in self.steps for call in step.tool_calls]


def append_run_log(path: Path, log: RunLog) -> None:
    """Append one run as a single JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(log.model_dump_json() + "\n")
