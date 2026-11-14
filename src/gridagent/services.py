"""Application services shared by the CLI and the web app.

Keeping this logic here (not in the web layer) means it is written once and
tested on its own; the web API only translates HTTP to these functions.

The "presentation" helpers at the bottom turn an agent run into what an end
user needs (answer, period, sources, plain-language method, charts) without
any agent or model vocabulary.
"""

import math
import threading
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from gridagent.agent.config import AgentConfig, LoadedAgent
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, ToolCallLog, append_run_log
from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient, EnergyChartsError
from gridagent.data.fixture_source import DataProvider, FixtureDataSource
from gridagent.data.models import Series
from gridagent.data.sources import source_series
from gridagent.evals.runner import percentile
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


# ---------- run history (monitoring, used by `garden runs`) ----------


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
    failed = sum(r.status in ("error", "timeout", "max_steps") for r in runs)
    return RunSummary(
        n_runs=n,
        ok_rate=sum(r.status == "ok" for r in runs) / n if n else None,
        error_rate=failed / n if n else None,
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        total_cost_usd=round(sum(r.cost_usd or 0 for r in runs), 6),
    )


# ---------- spending guard ----------


class Budget:
    """A spending limit for live model calls (per web-server process).

    A lock makes it safe when several requests run at the same time.
    """

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self._lock = threading.Lock()

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd, 0.0)

    def allows_another_call(self, worst_case_usd: float) -> bool:
        with self._lock:
            return self.spent_usd + worst_case_usd <= self.limit_usd

    def add(self, cost_usd: float | None) -> None:
        with self._lock:
            self.spent_usd += cost_usd or 0.0


# ---------- presentation: plain language for end users ----------

SOURCE_LABELS = {
    "solar": "solar",
    "wind": "wind (onshore + offshore)",
    "wind_onshore": "onshore wind",
    "wind_offshore": "offshore wind",
    "biomass": "biomass",
    "hydro": "hydropower",
    "pumped_storage": "pumped storage",
    "geothermal": "geothermal",
    "lignite": "lignite",
    "hard_coal": "hard coal",
    "gas": "natural gas",
    "oil": "oil",
    "fossil": "fossil fuels",
    "renewable": "renewables",
    "waste": "waste",
    "others": "other sources",
    "load": "electricity demand (load)",
    "total": "total public net generation",
}
GENERATION_SOURCE = {
    "name": "Energy-Charts.info (Fraunhofer ISE)",
    "url": "https://energy-charts.info",
    "license": "CC BY 4.0",
}
PRICE_SOURCE = {
    "name": "Bundesnetzagentur | SMARD.de, via Energy-Charts.info",
    "url": "https://www.smard.de",
    "license": "CC BY 4.0",
}


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "", (source or "").replace("_", " "))


def format_period(start: str, end: str) -> str:
    """'2026-11-02', '2026-11-08' -> '2 Nov 2026 – 8 Nov 2026' (or one date)."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    first = f"{a.day} {a:%b %Y}"
    return first if a == b else f"{first} – {b.day} {b:%b %Y}"


def method_step(call: ToolCallLog) -> str:
    """Describe one tool call the way a methods section would."""
    args = call.input
    if call.name in ("get_generation", "get_day_ahead_price"):
        what = (
            "electricity generation and demand data for Germany"
            if call.name == "get_generation"
            else "day-ahead electricity prices (Germany–Luxembourg)"
        )
        period = format_period(args.get("start", ""), args.get("end", ""))
        if call.is_error:
            return f"Looked for {what} for {period}: no data available"
        return f"Retrieved {what} in 15-minute resolution for {period}"

    subject = source_label(args.get("source")) if args.get("source") else "the price"
    per_day = " for each day" if args.get("per_day") else ""
    operation = args.get("operation")
    if operation == "share":
        whole = source_label(args.get("relative_to") or "total")
        text = f"Calculated the share of {subject} in {whole}"
    elif operation == "energy":
        text = f"Calculated the total energy from {subject}{per_day}"
    elif operation == "mean":
        text = f"Calculated the average of {subject}{per_day}"
    elif operation in ("max", "min"):
        word = "highest" if operation == "max" else "lowest"
        text = f"Found the {word} value of {subject}{per_day} and when it occurred"
    else:
        text = f"Calculated the {operation} of {subject}{per_day}"
    return text + (" (failed)" if call.is_error else "")


def method_steps(log: RunLog) -> list[str]:
    return [method_step(call) for call in log.tool_calls]


def sources_for_run(log: RunLog) -> list[dict[str, str]]:
    """The data sources an answer is based on (only for successful data fetches)."""
    names = {c.name for c in log.tool_calls if not c.is_error}
    sources = []
    if "get_generation" in names:
        sources.append(GENERATION_SOURCE)
    if "get_day_ahead_price" in names:
        sources.append(PRICE_SOURCE)
    return sources


# ---------- charts of the data behind an answer ----------

MAX_CHART_POINTS = 400
MAX_CHARTS = 3


class ChartPoint(BaseModel):
    t: str  # ISO timestamp (start of the interval, German local time)
    v: float | None


class Chart(BaseModel):
    title: str
    unit: str
    period: str
    resolution_minutes: int | None  # after averaging; for labels and CSV
    points: list[ChartPoint]


def downsample(series: Series, max_points: int = MAX_CHART_POINTS) -> list[ChartPoint]:
    """Average consecutive values so a month of 15-minute data fits in a chart."""
    size = bucket_size(series, max_points)
    points = []
    for i in range(0, len(series.points), size):
        bucket = series.points[i : i + size]
        values = [p.value for p in bucket if p.value is not None]
        mean = sum(values) / len(values) if values else None
        points.append(ChartPoint(t=bucket[0].timestamp.isoformat(), v=mean))
    return points


def bucket_size(series: Series, max_points: int = MAX_CHART_POINTS) -> int:
    return max(1, math.ceil(len(series.points) / max_points))


def charts_for_run(log: RunLog, data: DataProvider) -> list[Chart]:
    """Re-create the series the agent analysed, for plotting next to the answer.

    The ToolBox numbers refs in the order of successful fetches (gen_1,
    price_2, ...), so we replay that numbering from the logged tool calls.
    """
    fetches: dict[str, tuple[str, date, date]] = {}
    counter = 0
    wanted: list[tuple[str, str]] = []
    for call in log.tool_calls:
        if call.is_error:
            continue
        if call.name in ("get_generation", "get_day_ahead_price"):
            counter += 1
            prefix = "gen" if call.name == "get_generation" else "price"
            start = date.fromisoformat(call.input["start"])
            end = date.fromisoformat(call.input["end"])
            fetches[f"{prefix}_{counter}"] = (prefix, start, end)
        elif call.name == "compute_stats":
            pair = (call.input.get("ref", ""), call.input.get("source") or "day_ahead_price")
            if pair not in wanted:
                wanted.append(pair)

    charts = []
    for ref, source in wanted[:MAX_CHARTS]:
        if ref not in fetches:
            continue
        kind, start, end = fetches[ref]
        try:
            if kind == "price":
                response = data.day_ahead_price(start, end)
                series = response.get_series("day_ahead_price")
                title = "Day-ahead electricity price, Germany–Luxembourg"
            else:
                response = data.public_power(start, end)
                series = source_series(response, source)
                label = source_label(source)
                title = label[0].upper() + label[1:]
                if source not in ("load", "total"):
                    title += " generation"
        except (EnergyChartsError, KeyError, ValueError):
            continue  # a chart is a nice-to-have; never fail the answer for it
        minutes = series.interval_minutes * bucket_size(series) if series.interval_minutes else None
        charts.append(
            Chart(
                title=title,
                unit=series.unit,
                period=format_period(start.isoformat(), end.isoformat()),
                resolution_minutes=minutes,
                points=downsample(series),
            )
        )
    return charts
