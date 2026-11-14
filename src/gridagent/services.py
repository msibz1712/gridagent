"""Application services shared by the CLI and the web UI.

Keeping this logic here (not in the UI file) means it is written once and
tested without Streamlit; the UI only handles layout and user input.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gridagent.agent.config import AgentConfig, LoadedAgent
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient
from gridagent.data.fixture_source import DataProvider, FixtureDataSource
from gridagent.evals.dataset import EvalCase
from gridagent.evals.offline import oracle_llm
from gridagent.evals.report import load_report
from gridagent.evals.runner import EvalReport, percentile
from gridagent.garden.registry import discover
from gridagent.llm.claude import ClaudeLLM


def build_llm(config: AgentConfig, model: str | None = None) -> ClaudeLLM:
    """The real Claude model, configured from agent.yaml (model can be overridden)."""
    return ClaudeLLM(
        model_name=model or config.model.name,
        max_tokens=config.model.max_tokens,
        effort=config.model.effort,
        refusal_fallback=config.model.refusal_fallback,
        timeout_seconds=config.limits.model_timeout_seconds,
        max_retries=config.limits.model_max_retries,
    )


def build_data(use_fixtures: bool, fixture_dir: Path, cache_dir: Path) -> DataProvider:
    """Recorded fixtures (offline, reproducible) or the live Energy-Charts API (cached)."""
    if use_fixtures:
        return FixtureDataSource(fixture_dir)
    return EnergyChartsClient(cache=ResponseCache(cache_dir))


def ask(
    agent: LoadedAgent,
    question: str,
    model: str | None,
    data: DataProvider,
    today: date,
    log_path: Path | None,
) -> RunLog:
    """Run one question with the real model and append the run to the log."""
    log = Agent(agent, build_llm(agent.config, model), data, today=lambda: today).run(question)
    if log_path is not None:
        append_run_log(log_path, log)
    return log


def oracle_demo(agent: LoadedAgent, case: EvalCase, fixture_dir: Path, today: date) -> RunLog:
    """Free demo: the scripted oracle answers an eval question with real tools on fixtures."""
    runner = Agent(agent, oracle_llm(case), FixtureDataSource(fixture_dir), today=lambda: today)
    return runner.run(case.question)


def valid_specs(agents_dir: Path) -> list[str]:
    """All valid 'name@version' specs, newest version of each agent first."""
    specs = []
    for entry in discover(agents_dir):
        specs += [f"{entry.name}@{v.version}" for v in reversed(entry.versions) if v.agent]
    return specs


def trace_rows(log: RunLog) -> list[dict[str, Any]]:
    """One row per model step and per tool call, for a trace table."""
    rows = []
    for step in log.steps:
        u = step.usage
        tokens_in = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        rows.append({
            "step": step.step, "kind": "model", "name": step.model,
            "detail": f"stop_reason={step.stop_reason}", "seconds": step.latency_s,
            "tokens in/out": f"{tokens_in}/{u.output_tokens}",
            "cost_usd": step.cost_usd, "error": False,
        })  # fmt: skip
        for call in step.tool_calls:
            rows.append({
                "step": step.step, "kind": "tool", "name": call.name,
                "detail": json.dumps(call.input), "seconds": call.latency_s,
                "tokens in/out": "", "cost_usd": None, "error": call.is_error,
            })  # fmt: skip
    return rows


# ---------- run history (monitoring) ----------


def load_runs(log_path: Path, limit: int = 200) -> list[RunLog]:
    """The most recent runs, newest first. Unreadable lines are skipped."""
    if not log_path.is_file():
        return []
    runs = []
    for line in log_path.read_text().splitlines()[-limit:]:
        try:
            runs.append(RunLog.model_validate_json(line))
        except ValueError:
            continue
    return list(reversed(runs))


class RunSummary(BaseModel):
    n_runs: int
    ok_rate: float | None
    error_rate: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    total_cost_usd: float


def summarize_runs(runs: list[RunLog]) -> RunSummary:
    n = len(runs)
    latencies = sorted(r.latency_s for r in runs)
    return RunSummary(
        n_runs=n,
        ok_rate=sum(r.status == "ok" for r in runs) / n if n else None,
        error_rate=sum(r.status in ("error", "timeout", "max_steps") for r in runs) / n
        if n
        else None,
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        total_cost_usd=round(sum(r.cost_usd or 0 for r in runs), 6),
    )


def run_row(run: RunLog) -> dict[str, Any]:
    return {
        "time (UTC)": run.started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "agent": f"{run.agent}@{run.agent_version}",
        "model": run.model,
        "status": run.status,
        "question": run.question,
        "steps": len(run.steps),
        "seconds": run.latency_s,
        "cost_usd": run.cost_usd,
        "run_id": run.run_id,
    }


# ---------- eval reports ----------


def list_reports(reports_dir: Path) -> list[Path]:
    """Eval report JSON files, newest first."""
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("*.json"), reverse=True)


def load_reports(paths: list[Path]) -> dict[str, EvalReport]:
    """Readable reports keyed by file name (broken files are skipped)."""
    reports = {}
    for path in paths:
        try:
            reports[path.name] = load_report(path)
        except ValueError:
            continue
    return reports


# ---------- spending guard ----------


class Budget(BaseModel):
    """A simple per-session spending limit for live model calls."""

    limit_usd: float
    spent_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd, 0.0)

    def allows_another_call(self, worst_case_usd: float) -> bool:
        return self.spent_usd + worst_case_usd <= self.limit_usd

    def add(self, cost_usd: float | None) -> None:
        self.spent_usd += cost_usd or 0.0
