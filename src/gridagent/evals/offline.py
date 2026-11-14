"""Offline eval modes: the oracle and record/replay.

oracle  A scripted "perfect agent": it makes exactly the tool calls listed in
        the eval case and gives `oracle_answer`. The tools really run on the
        fixtures, so this checks that the harness, the tools, the scoring and
        the eval set agree with each other. It says NOTHING about model quality.

replay  Real model turns recorded during a `--live --record` run are played
        back. Tools and scoring run again, for free. Because the model's turns
        are fixed, replay catches regressions in tools, loop and scoring, not
        the effect of a prompt change (that needs a new live run).
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gridagent.evals.dataset import EvalCase
from gridagent.llm.base import LLM, ModelResponse, TokenUsage
from gridagent.llm.fake import FakeLLM, text, tool_use
from gridagent.tools.definitions import REF_PREFIX

ORACLE_USAGE = TokenUsage()  # the oracle costs nothing
STATS_DEFAULTS = {
    "source": None, "relative_to": None, "per_day": False,
    "threshold": None, "weight_ref": None, "weight_source": None,
}  # fmt: skip


# ---------- oracle ----------


def oracle_llm(case: EvalCase) -> FakeLLM:
    """One tool call per turn (in eval-set order), then the reference answer.

    compute_stats gets the most recent ref unless the eval case names one; refs
    are numbered like the ToolBox does (gen_1, price_2, ...) in fetch order.
    """
    turns: list[list[dict[str, Any]]] = []
    fetches = 0
    last_ref = None
    for i, call in enumerate(case.tool_calls, start=1):
        args = dict(call.args)
        if call.name == "compute_stats":
            args = {"ref": last_ref, **STATS_DEFAULTS, **args}
        elif call.name in REF_PREFIX:
            fetches += 1
            last_ref = f"{REF_PREFIX[call.name]}_{fetches}"
            if call.name == "get_generation":
                args.setdefault("sources", None)
                args.setdefault("scope", None)
        elif call.name == "get_installed_capacity":
            args.setdefault("technologies", None)
        elif call.name == "get_reference_statistics":
            args.setdefault("period", None)
        turns.append([tool_use(call.name, args, tool_id=f"oracle_{i}")])
    turns.append([text(case.oracle_answer)])
    return FakeLLM(turns, model_name="oracle", usage=ORACLE_USAGE)


# ---------- recording ----------


class CaseRecording(BaseModel):
    question: str
    responses: list[ModelResponse]
    latency_s: float  # the original live run's latency


class Recording(BaseModel):
    """All recorded model turns for one agent version + model."""

    agent: str
    agent_version: str
    model: str
    eval_set_hash: str
    recorded_at: datetime
    cases: dict[str, CaseRecording] = {}


class RecordingLLM:
    """Wraps a real LLM and remembers every response it returns."""

    def __init__(self, inner: LLM) -> None:
        self.inner = inner
        self.model_name = inner.model_name
        self.responses: list[ModelResponse] = []

    def create(
        self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelResponse:
        response = self.inner.create(system, tools, messages)
        self.responses.append(response)
        return response


def recording_path(agent_version_dir: Path, model: str) -> Path:
    return agent_version_dir / "recordings" / f"{model}.json"


def load_recording(path: Path) -> Recording | None:
    if not path.is_file():
        return None
    return Recording.model_validate(json.loads(path.read_text()))


def save_recording(path: Path, recording: Recording) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(recording.model_dump_json(indent=1) + "\n")


def new_recording(agent: str, version: str, model: str, eval_set_hash: str) -> Recording:
    return Recording(
        agent=agent,
        agent_version=version,
        model=model,
        eval_set_hash=eval_set_hash,
        recorded_at=datetime.now(UTC),
    )


def replay_llm(case_recording: CaseRecording) -> FakeLLM:
    model = case_recording.responses[0].model if case_recording.responses else "replay"
    return FakeLLM(list(case_recording.responses), model_name=model)
