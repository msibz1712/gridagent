"""Daily briefing: key figures, day plans, explanations and storage (event rules:
test_anomalies.py)."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Point, Series
from gridagent.monitor.daily import (
    Figure,
    build_briefing,
    day_plan,
)
from gridagent.monitor.explain import (
    explain_events,
    list_briefings,
    load_briefing,
    question_for,
    save_briefing,
)

from conftest import FIXTURE_DIR

DAY = date(2026, 10, 29)  # Thursday; the 28 days before it are recorded


def briefing():
    return build_briefing(FixtureDataSource(FIXTURE_DIR), DAY, today=date(2026, 10, 30))


def prices(values_by_day: dict[date, list[float]]) -> Series:
    """Hourly prices, local midnight to midnight (CET, so UTC + 1 h, no DST in November)."""
    points = []
    for day, values in sorted(values_by_day.items()):
        start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=1)
        points += [Point(timestamp=start + timedelta(hours=h), value=v)
                   for h, v in enumerate(values)]  # fmt: skip
    return Series(id="day_ahead_price", unit="EUR / MWh", interval_minutes=60, points=points)


# ---------- key figures on recorded data ----------


def test_key_figures_against_the_raw_data() -> None:
    raw = json.loads((FIXTURE_DIR / "price_DE-LU_2026-10-01_2026-10-31.json").read_text())
    day_values = [r["values"]["day_ahead_price"] for r in raw["data"]
                  if r["timestamp"].startswith("2026-10-29")]  # fmt: skip
    figures = {f.name: f for f in briefing().figures}
    mean = figures["Average day-ahead price"]
    assert mean.value == pytest.approx(sum(day_values) / len(day_values), abs=0.01)
    assert figures["Highest price"].value == max(day_values)
    assert figures["Highest price"].at == "17:15"
    assert figures["Wind energy"].change_pct < -50  # a calm day


def test_percentages_only_where_they_mean_something() -> None:
    assert Figure(name="Wind energy", value=100, unit="GWh", normal=500).change_pct == -80
    imports = Figure(name="Net commercial imports", value=138, unit="GWh", normal=-36)
    assert imports.change_pct is None and imports.difference == 174
    assert (
        Figure(name="Hours with negative prices", value=0, unit="h", normal=0.06).change_pct is None
    )


def test_october_peak_day_has_a_spike_a_lull_and_plans() -> None:
    result = briefing()
    kinds = [e.kind for e in result.events]
    assert kinds[0] == "price_spike" and "lull" in kinds
    spike = result.events[0]
    assert (spike.from_hour, spike.to_hour) == (13, 20) and spike.facts["robust_z"] > 5
    assert [p.day for p in result.plans] == ["2026-10-30", "2026-10-31"]


# ---------- day plans on made-up prices ----------


def test_day_plan_finds_the_cheapest_and_dearest_three_hours() -> None:
    day = date(2026, 11, 26)
    values = [100.0] * 24
    values[11:14] = [10.0, 5.0, 15.0]
    values[17:20] = [300.0, 320.0, 310.0]
    plan = day_plan(day, prices({day: values}))
    assert (plan.cheapest_hours, plan.cheapest_price) == ("11:00–14:00", 10.0)
    assert (plan.dearest_hours, plan.dearest_price) == ("17:00–20:00", 310.0)
    assert plan.battery_profit_eur_per_mw > 0


def test_unpublished_prices_mean_no_plan_not_an_error() -> None:
    result = build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 30),
                            today=date(2026, 11, 1))  # fmt: skip
    assert [p.day for p in result.plans] == ["2026-11-02"]  # 1 November is not recorded


# ---------- explanations and storage ----------


def test_explanations_ask_the_agent_and_respect_the_cost_limit() -> None:
    from test_loop import AGENTS_DIR

    from gridagent.garden.registry import resolve
    from gridagent.llm.fake import FakeLLM, text

    result = briefing()
    asked = []

    def llm():
        asked.append(1)
        return FakeLLM([[text("Little wind and a high residual load.")]])

    agent = resolve(AGENTS_DIR, "energy_qa@0.11.0")
    explain_events(result, agent, llm, FixtureDataSource(FIXTURE_DIR), date(2026, 10, 30),
                   max_cost_usd=1.0)  # fmt: skip
    explained = [e for e in result.events if e.explanation]
    assert len(asked) == 2 and len(explained) == 2  # the two price spikes, not the lull
    assert explained[0].explanation == "Little wind and a high residual load."
    assert question_for(result.events[0], DAY) == (
        "Why was the electricity price so high on 29 October 2026 between 13:00 and 20:00?"
    )

    fresh, asked[:] = briefing(), []
    explain_events(fresh, agent, llm, FixtureDataSource(FIXTURE_DIR), date(2026, 10, 30),
                   max_cost_usd=0.0)  # fmt: skip
    assert asked == []


def test_briefings_are_saved_listed_and_loaded(tmp_path) -> None:
    save_briefing(tmp_path, briefing())
    assert list_briefings(tmp_path) == ["2026-10-29"]
    stored = load_briefing(tmp_path, "2026-10-29")
    assert stored["figures"][0]["change_pct"] is not None and stored["events"]


def test_cli_brief_without_the_model(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from gridagent.cli import app

    monkeypatch.setenv("GARDEN_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["brief", "--fixtures", "--today", "2026-10-30"])
    assert result.exit_code == 0, result.output
    assert "without explanations" in result.output
    assert (tmp_path / "briefings" / "2026-10-29.json").is_file()
