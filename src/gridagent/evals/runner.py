"""Run an eval set against an agent and aggregate the metrics."""

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from gridagent.agent.config import LoadedAgent, parse_semver
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog
from gridagent.data.provider import DataProvider
from gridagent.evals.dataset import EvalCase, EvalSet
from gridagent.evals.scoring import CheckResults, score
from gridagent.llm.base import LLM, TokenUsage
from gridagent.llm.pricing import estimate_cost_usd

Mode = Literal["oracle", "replay", "live"]

# Rough per-question token use for the live cost estimate (from M2 runs:
# ~3 model calls; first-run cache writes dominate).
EST_INPUT_TOKENS_PER_CASE = 6000
EST_OUTPUT_TOKENS_PER_CASE = 450


class CaseResult(BaseModel):
    id: str
    category: str
    outcome: str
    question: str
    passed: bool
    failed_checks: list[str]
    checks: CheckResults
    answer: str
    tool_calls: list[dict[str, Any]]
    status: str
    error: str | None
    steps: int
    latency_s: float
    usage: TokenUsage
    cost_usd: float | None


class Metrics(BaseModel):
    n_cases: int
    pass_rate: float
    answer_accuracy: float | None  # numbers + dates correct, over answerable cases
    grounding_rate: float | None  # expected numbers present in tool results
    tool_use_correctness: float | None
    refusal_correctness: float | None  # declined exactly when it should
    no_data_correctness: float | None
    source_citation_rate: float | None
    error_rate: float
    latency_p50_s: float | None
    latency_p95_s: float | None
    avg_input_tokens: float
    avg_output_tokens: float
    avg_cost_usd: float | None
    total_cost_usd: float | None


class EvalReport(BaseModel):
    created_at: datetime
    agent: str
    agent_version: str
    model: str
    mode: Mode
    eval_set: str
    eval_set_version: str
    eval_set_hash: str
    today: str
    notes: list[str]
    metrics: Metrics
    cases: list[CaseResult]


def run_case(
    agent: LoadedAgent, case: EvalCase, llm: LLM, data: DataProvider, eval_set: EvalSet
) -> RunLog:
    """Run a case; with earlier turns, all in one conversation. Only the last turn is scored,
    but its log's tokens, cost and latency include the earlier turns (what the user paid)."""
    runner = Agent(agent, llm, data, today=lambda: eval_set.today)
    conversation = runner.new_conversation()
    earlier = [runner.run(turn.question, conversation) for turn in case.previous]
    log = runner.run(case.question, conversation)
    for previous in earlier:
        log.usage = log.usage + previous.usage
        log.latency_s = round(log.latency_s + previous.latency_s, 3)
        if log.cost_usd is not None and previous.cost_usd is not None:
            log.cost_usd += previous.cost_usd
        if previous.status != "ok" and log.status == "ok":
            log.status, log.error = "error", f"Earlier turn failed: {previous.error}"
    return log


def case_result(case: EvalCase, log: RunLog) -> CaseResult:
    checks = score(case, log)
    return CaseResult(
        id=case.id,
        category=case.category,
        outcome=case.outcome,
        question=case.question,
        passed=checks.passed(),
        failed_checks=checks.failed_checks(),
        checks=checks,
        answer=log.final_answer,
        tool_calls=[
            {"name": c.name, "input": c.input, "is_error": c.is_error} for c in log.tool_calls
        ],
        status=log.status,
        error=log.error,
        steps=len(log.steps),
        latency_s=log.latency_s,
        usage=log.usage,
        cost_usd=log.cost_usd,
    )


def run_eval(
    agent: LoadedAgent,
    eval_set: EvalSet,
    eval_set_hash: str,
    mode: Mode,
    model: str,
    llm_for_case: Callable[[EvalCase], LLM],
    data: DataProvider,
    after_case: Callable[[EvalCase, RunLog, LLM], RunLog] | None = None,
    progress: Callable[[int, int, CaseResult], None] | None = None,
    only: Callable[[EvalCase], bool] | None = None,
    max_cost_usd: float | None = None,
) -> EvalReport:
    """Run every supported case (or those `only` selects). `after_case` lets the caller
    record or adjust each run log.

    `max_cost_usd` is a hard spending limit: no new case starts once the cases so far
    have cost that much, so a run can overshoot by at most one case (about $0.002 on
    Haiku). Unfinished cases are listed in the report notes.
    """
    cases, skipped = supported_cases(agent, eval_set)
    if only is not None:
        cases = [c for c in cases if only(c)]
    results = []
    spent = 0.0
    stopped: list[str] = []
    for i, case in enumerate(cases, start=1):
        if max_cost_usd is not None and spent >= max_cost_usd:
            stopped = [c.id for c in cases[i - 1 :]]
            break
        llm = llm_for_case(case)
        log = run_case(agent, case, llm, data, eval_set)
        if after_case is not None:
            log = after_case(case, log, llm)
        spent += log.cost_usd or 0.0
        result = case_result(case, log)
        results.append(result)
        if progress is not None:
            progress(i, len(cases), result)

    return EvalReport(
        created_at=datetime.now(UTC),
        agent=agent.config.name,
        agent_version=agent.config.version,
        model=model,
        mode=mode,
        eval_set=eval_set.name,
        eval_set_version=eval_set.version,
        eval_set_hash=eval_set_hash,
        today=eval_set.today.isoformat(),
        notes=mode_notes(mode) + skipped_note(skipped) + _stopped_note(stopped, max_cost_usd),
        metrics=compute_metrics(results),
        cases=results,
    )


def supported_cases(agent: LoadedAgent, eval_set: EvalSet) -> tuple[list[EvalCase], list[str]]:
    """Cases this agent version can answer, and the ids of the ones it skips."""
    version = parse_semver(agent.config.version)
    supported, skipped = [], []
    for case in eval_set.cases:
        if case.min_version and parse_semver(case.min_version) > version:
            skipped.append(case.id)
        else:
            supported.append(case)
    return supported, skipped


def _stopped_note(stopped: list[str], limit: float | None) -> list[str]:
    if not stopped:
        return []
    return [f"Cost limit ${limit:.2f} reached: {len(stopped)} case(s) not run: "
            f"{', '.join(stopped)}."]  # fmt: skip


def skipped_note(skipped: list[str]) -> list[str]:
    if not skipped:
        return []
    return [f"{len(skipped)} case(s) skipped because they need a newer agent version: "
            f"{', '.join(skipped)}."]  # fmt: skip


def mode_notes(mode: Mode) -> list[str]:
    if mode == "oracle":
        return [
            "Oracle mode: a scripted perfect agent. Validates harness, tools, scoring and eval "
            "set consistency. It does not measure model quality; latency and cost are not "
            "meaningful."
        ]
    if mode == "replay":
        return [
            "Replay mode: recorded model turns are replayed; tools and scoring run again. "
            "Latency and cost are those of the original live run. Prompt or model changes "
            "need a new live run."
        ]
    return ["Live mode: real model calls on fixed fixture data."]


def compute_metrics(results: list[CaseResult]) -> Metrics:
    def rate(values: list[bool | None]) -> float | None:
        applicable = [v for v in values if v is not None]
        return round(sum(applicable) / len(applicable), 4) if applicable else None

    answer_ok = [
        None if r.checks.numbers is None else (r.checks.numbers and r.checks.dates is not False)
        for r in results
    ]
    costs = [r.cost_usd for r in results]
    present = [c for c in costs if c is not None]
    known_costs = present if len(present) == len(costs) else None  # all or nothing
    latencies = sorted(r.latency_s for r in results)
    n = len(results)
    return Metrics(
        n_cases=n,
        pass_rate=round(sum(r.passed for r in results) / n, 4),
        answer_accuracy=rate(answer_ok),
        grounding_rate=rate([r.checks.grounded for r in results]),
        tool_use_correctness=rate([r.checks.tool_use for r in results]),
        refusal_correctness=rate([r.checks.refusal for r in results]),
        no_data_correctness=rate([r.checks.no_data for r in results]),
        source_citation_rate=rate([r.checks.source for r in results]),
        error_rate=round(sum(not r.checks.no_error for r in results) / n, 4),
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        avg_input_tokens=round(sum(_input_tokens(r.usage) for r in results) / n, 1),
        avg_output_tokens=round(sum(r.usage.output_tokens for r in results) / n, 1),
        avg_cost_usd=None if known_costs is None else round(sum(known_costs) / n, 6),
        total_cost_usd=None if known_costs is None else round(sum(known_costs), 6),
    )


def _input_tokens(usage: TokenUsage) -> int:
    """All input tokens, cached or not (what the model actually read)."""
    return usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of values <= it."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return round(sorted_values[rank - 1], 3)


def estimate_live_cost(model: str, n_cases: int) -> float | None:
    """Rough upper-end estimate of a live eval's cost in USD."""
    per_case = estimate_cost_usd(
        model,
        TokenUsage(
            input_tokens=EST_INPUT_TOKENS_PER_CASE, output_tokens=EST_OUTPUT_TOKENS_PER_CASE
        ),
    )
    return None if per_case is None else per_case * n_cases
