"""Services shared by CLI and website: model/data builders, run history, budget,
plain-language presentation and charts."""

from datetime import date
from pathlib import Path

import pytest

from gridagent import services
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import ToolCallLog, append_run_log
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.sources import source_series
from gridagent.evals.offline import oracle_llm
from gridagent.evals.session import load_agent_eval_set
from gridagent.garden.registry import resolve

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
agent = resolve(AGENTS, "energy_qa@0.1.0")
eval_set, _ = load_agent_eval_set(agent)


def oracle_run(case_id: str):
    """A realistic run log: the scripted oracle answering one eval question on fixtures."""
    case = next(c for c in eval_set.cases if c.id == case_id)
    runner = Agent(
        agent, oracle_llm(case), FixtureDataSource(FIXTURE_DIR), today=lambda: eval_set.today
    )
    return runner.run(case.question)


def call(name: str, is_error: bool = False, **tool_input) -> ToolCallLog:
    return ToolCallLog(name=name, input=tool_input, is_error=is_error, latency_s=0, result="")


# ---------- builders ----------


def test_build_data_picks_source(tmp_path: Path) -> None:
    from gridagent.data.client import EnergyChartsClient

    assert isinstance(services.build_data(True, FIXTURE_DIR, tmp_path), FixtureDataSource)
    assert isinstance(services.build_data(False, FIXTURE_DIR, tmp_path), EnergyChartsClient)


def test_build_llm_uses_agent_config() -> None:
    llm = services.build_llm(agent.config)
    assert llm.model_name == "claude-sonnet-5-5"
    assert llm.effort == agent.config.model.effort
    assert services.build_llm(agent.config, "claude-haiku-5-5").model_name == "claude-haiku-5-5"


def test_latest_energy_qa_uses_haiku_for_cost() -> None:
    """0.2.0 switched the default model to Haiku 5.5 (no server-side refusal fallback)."""
    latest = resolve(AGENTS, "energy_qa")
    assert latest.spec == "energy_qa@0.2.0"
    llm = services.build_llm(latest.config)
    assert llm.model_name == "claude-haiku-5-5"
    assert llm.use_fallback is False


def test_ask_logs_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gridagent.llm.fake import FakeLLM, text

    monkeypatch.setattr(services, "build_llm", lambda cfg, model=None: FakeLLM([[text("hi")]]))
    log_path = tmp_path / "runs.jsonl"
    data = services.build_data(True, FIXTURE_DIR, tmp_path)
    log = services.ask(agent, "q", None, data, date(2026, 11, 14), log_path)
    assert log.final_answer == "hi"
    assert services.load_runs(log_path)[0].run_id == log.run_id


# ---------- run history ----------


def test_runs_roundtrip_and_summary(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    first, second = oracle_run("wind_share_last_week"), oracle_run("price_mean_yesterday")
    second.status, second.latency_s, second.cost_usd = "error", 3.0, 0.01
    append_run_log(path, first)
    append_run_log(path, second)
    with path.open("a") as f:
        f.write("not json\n")  # broken lines are skipped
    runs = services.load_runs(path)
    assert [r.run_id for r in runs] == [second.run_id, first.run_id]  # newest first
    summary = services.summarize_runs(runs)
    assert summary.n_runs == 2
    assert summary.ok_rate == 0.5 and summary.error_rate == 0.5
    assert summary.total_cost_usd == pytest.approx(0.01)


def test_no_runs() -> None:
    assert services.load_runs(Path("does/not/exist.jsonl")) == []
    assert services.summarize_runs([]).ok_rate is None


# ---------- budget ----------


def test_budget() -> None:
    budget = services.Budget(limit_usd=0.05)
    assert budget.allows_another_call(0.02)
    budget.add(0.04)
    assert not budget.allows_another_call(0.02)
    assert budget.remaining_usd == pytest.approx(0.01)
    budget.add(None)  # unknown cost doesn't crash
    assert budget.spent_usd == pytest.approx(0.04)


# ---------- plain-language presentation ----------


def test_format_period() -> None:
    assert services.format_period("2026-11-02", "2026-11-08") == "2 Nov 2026 – 8 Nov 2026"
    assert services.format_period("2026-11-13", "2026-11-13") == "13 Nov 2026"


def test_method_steps_read_like_a_methods_section() -> None:
    steps = services.method_steps(oracle_run("wind_share_last_week"))
    assert steps == [
        "Retrieved electricity generation and demand data for Germany in 15-minute "
        "resolution for 2 Nov 2026 – 8 Nov 2026",
        "Calculated the share of wind (onshore + offshore) in total public net generation",
    ]


@pytest.mark.parametrize(
    ("tool_call", "expected"),
    [
        (
            call("compute_stats", operation="mean", source=None),
            "Calculated the average of the price",
        ),
        (
            call("compute_stats", operation="energy", source="solar", per_day=True),
            "Calculated the total energy from solar for each day",
        ),
        (
            call("compute_stats", operation="max", source="solar"),
            "Found the highest value of solar and when it occurred",
        ),
        (
            call("compute_stats", operation="share", source="renewable", relative_to="load"),
            "Calculated the share of renewables in electricity demand (load)",
        ),
        (
            call("get_day_ahead_price", is_error=True, start="2026-12-01", end="2026-12-01"),
            "Looked for day-ahead electricity prices (Germany–Luxembourg) for 1 Dec 2026: "
            "no data available",
        ),
    ],
)
def test_method_step_wording(tool_call: ToolCallLog, expected: str) -> None:
    assert services.method_step(tool_call) == expected


def test_no_tool_or_model_vocabulary_in_steps() -> None:
    for case in eval_set.cases:
        for step in services.method_steps(oracle_run(case.id)):
            for jargon in ("compute_stats", "get_", "ref", "tool", "model", "gen_1", "price_1"):
                assert jargon not in step, (case.id, step)


def test_sources_only_for_data_actually_used() -> None:
    assert services.sources_for_run(oracle_run("wind_share_last_week")) == [
        services.GENERATION_SOURCE
    ]
    assert services.sources_for_run(oracle_run("price_mean_yesterday")) == [services.PRICE_SOURCE]
    assert services.sources_for_run(oracle_run("refuse_investment_advice")) == []
    assert services.sources_for_run(oracle_run("missing_price_day")) == []  # fetch failed


# ---------- charts ----------


def test_charts_recreate_the_analysed_series() -> None:
    data = FixtureDataSource(FIXTURE_DIR)
    [chart] = services.charts_for_run(oracle_run("wind_share_last_week"), data)
    assert chart.title == "Wind (onshore + offshore) generation"
    assert chart.unit == "MW" and chart.period == "2 Nov 2026 – 8 Nov 2026"
    assert chart.resolution_minutes == 30  # 672 quarter-hours averaged to <= 400 points
    assert len(chart.points) <= services.MAX_CHART_POINTS

    [price] = services.charts_for_run(oracle_run("price_mean_yesterday"), data)
    assert price.title.startswith("Day-ahead electricity price")
    assert price.resolution_minutes == 15 and len(price.points) == 96


def test_downsample_keeps_charts_small() -> None:
    october = FixtureDataSource(FIXTURE_DIR).public_power(date(2026, 10, 1), date(2026, 10, 31))
    points = services.downsample(source_series(october, "solar"))
    assert len(points) <= services.MAX_CHART_POINTS
    assert max(p.v for p in points) > 0
