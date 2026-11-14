"""Eval set format (YAML), validated with pydantic like agent.yaml.

See agents/energy_qa/evals.yaml for the documented format.
"""

import hashlib
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gridagent.agent.config import SEMVER_PATTERN, AgentConfigError, format_validation_error
from gridagent.tools.definitions import ALL_TOOLS

Outcome = Literal["answer", "refuse", "no_data", "refuse_or_no_data"]
Category = Literal["factual", "tool_use", "ambiguous", "missing_data", "future", "refusal"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedNumber(_Strict):
    """A number the answer must contain; any of `values` counts (e.g. GWh or TWh)."""

    label: str
    values: list[float] = Field(min_length=1)
    tolerance_pct: float = Field(1.0, ge=0)
    tolerance_abs: float = Field(0.0, ge=0)


class ExpectedToolCall(_Strict):
    """A call the agent should make. Only the listed args are compared."""

    name: str
    args: dict[str, Any] = {}

    @model_validator(mode="after")
    def tool_must_exist(self) -> "ExpectedToolCall":
        if self.name not in ALL_TOOLS:
            raise ValueError(f"unknown tool '{self.name}'")
        return self


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    category: Category
    question: str = Field(min_length=3)
    outcome: Outcome
    numbers: list[ExpectedNumber] = []
    dates: list[date] = []
    mentions: list[list[str]] = []  # each inner list: at least one must appear
    tool_calls: list[ExpectedToolCall] = []
    oracle_answer: str
    # First agent version that has the tools for this case; older versions skip it.
    min_version: str | None = Field(None, pattern=SEMVER_PATTERN)

    @model_validator(mode="after")
    def consistent_expectations(self) -> "EvalCase":
        if self.outcome != "answer" and self.numbers:
            raise ValueError("only outcome 'answer' can expect numbers")
        if self.outcome == "refuse" and self.tool_calls:
            raise ValueError("a 'refuse' case must not expect tool calls")
        return self


class EvalSet(_Strict):
    name: str
    version: str
    today: date
    cases: list[EvalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> "EvalSet":
        ids = [c.id for c in self.cases]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate case ids: {duplicates}")
        return self


def load_eval_set(path: Path) -> tuple[EvalSet, str]:
    """Load and validate an eval set. Returns it with a short content hash.

    The hash goes into every report, so results from different versions of
    the eval set are never compared by accident.
    """
    try:
        text = path.read_text()
        eval_set = EvalSet.model_validate(yaml.safe_load(text))
    except FileNotFoundError as exc:
        raise AgentConfigError(f"{path}: eval set not found") from exc
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{path}: invalid YAML: {exc}") from exc
    except ValidationError as exc:
        raise AgentConfigError(f"{path}:\n{format_validation_error(exc)}") from exc
    return eval_set, hashlib.sha256(text.encode()).hexdigest()[:12]
