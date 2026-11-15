"""Services shared by CLI and website: model/data builders, run history, budget,
plain-language presentation and charts."""

from datetime import date
from pathlib import Path

import pytest

from gridagent import services
from gridagent.agent.config import parse_semver
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
    from gridagent.data.provider import LiveDataSource

    assert isinstance(services.build_data(True, FIXTURE_DIR, tmp_path), FixtureDataSource)
    assert isinstance(services.build_data(False, FIXTURE_DIR, tmp_path), LiveDataSource)


def test_build_llm_uses_agent_config() -> None:
    llm = services.build_llm(agent.config)
    assert llm.model_name == "claude-sonnet-5-5"
    assert llm.effort == agent.config.model.effort
    assert services.build_llm(agent.config, "claude-haiku-5-5").model_name == "claude-haiku-5-5"


def test_latest_energy_qa_uses_haiku_for_cost() -> None:
    """Since 0.2.0 the default model is Haiku 5.5 (no server-side refusal fallback)."""
    latest = resolve(AGENTS, "energy_qa")
    assert parse_semver(latest.config.version) >= (0, 2, 0)
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


# ---------- Stage 1: new data in plain language ----------

latest = resolve(AGENTS, "energy_qa")
latest_cases = {c.id: c for c in load_agent_eval_set(latest)[0].cases}


def latest_run(case_id: str):
    runner = Agent(latest, oracle_llm(latest_cases[case_id]), FixtureDataSource(FIXTURE_DIR),
                   today=lambda: eval_set.today)  # fmt: skip
    return runner.run(latest_cases[case_id].question)


def test_no_jargon_in_steps_for_any_latest_case() -> None:
    jargon = ("compute_stats", "get_", "ref", "tool", "model", "capture_price", "weight_",
              "count_below", "_")  # fmt: skip
    for case_id in latest_cases:
        for step in services.method_steps(latest_run(case_id)):
            assert not any(j in step for j in jargon), (case_id, step)


def test_stage1_steps_read_well() -> None:
    assert services.method_steps(latest_run("poland_exchange_last_week")) == [
        "Retrieved physical cross-border electricity flows between Germany and its neighbours "
        "in 15-minute resolution for 2 Nov 2026 – 8 Nov 2026",
        "Calculated the net energy exchanged with Poland",
    ]
    steps = services.method_steps(latest_run("solar_market_value_october"))
    assert steps[2] == "Calculated the price weighted by solar generation (market value)"
    assert steps[3] == (
        "Looked up the official figure: Official monthly market values (Monatsmarktwerte) "
        "under the EEG (2026-10)"
    )
    [step, cf] = services.method_steps(latest_run("capacity_factor_wind_onshore_october"))
    assert cf.startswith("Calculated the capacity factor of onshore wind")
    [_, count] = services.method_steps(latest_run("negative_price_hours_october"))
    assert count == "Counted the hours below 0 for the price"


def test_stage1_sources() -> None:
    names = [s["name"] for s in services.sources_for_run(latest_run("regional_solar_tennet"))]
    assert names == [services.SMARD_SOURCE["name"]]
    co2 = services.sources_for_run(latest_run("co2_factor_2025"))
    assert co2[0]["name"].startswith("Umweltbundesamt") and co2[0]["url"].startswith("https://")
    market = [s["name"] for s in services.sources_for_run(latest_run("solar_market_value_october"))]
    assert any("netztransparenz" in n for n in market) and services.PRICE_SOURCE["name"] in market


def test_stage1_charts() -> None:
    data = FixtureDataSource(FIXTURE_DIR)
    [flow] = services.charts_for_run(latest_run("poland_exchange_last_week"), data)
    assert flow.title == "Net physical imports: Poland (negative = export)" and flow.unit == "GW"
    [region] = services.charts_for_run(latest_run("regional_solar_tennet"), data)
    assert region.title == "Solar generation, TenneT control area" and region.unit == "MW"
    price, solar = services.charts_for_run(latest_run("solar_market_value_october"), data)
    assert price.title.startswith("Day-ahead") and solar.title == "Solar generation"
    assert services.charts_for_run(latest_run("co2_factor_2025"), data) == []  # no series


def test_regional_steps_name_their_control_area() -> None:
    steps = services.method_steps(latest_run("regional_wind_largest_area"))
    energy_steps = [s for s in steps if s.startswith("Calculated")]
    assert energy_steps == [
        f"Calculated the total energy from onshore wind in the {area} control area"
        for area in ("50Hertz", "Amprion", "TenneT", "TransnetBW")
    ]
    data = FixtureDataSource(FIXTURE_DIR)
    assert len(services.charts_for_run(latest_run("regional_wind_largest_area"), data)) == 4
