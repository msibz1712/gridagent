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
RESULT_PREVIEW_CHARS = 2000


class ToolCallLog(BaseModel):
    name: str
    input: dict[str, Any]
    is_error: bool
    latency_s: float
    result: str  # truncated to RESULT_PREVIEW_CHARS


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
    status: RunStatus = "ok"
    final_answer: str = ""
    error: str | None = None
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
