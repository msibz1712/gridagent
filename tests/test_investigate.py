"""Price event investigation: hour selection, comparable days, merit order, drivers."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Point, Series
from gridagent.tools.investigate import (
    BERLIN,
    STEEP_TOP,
    hourly,
    investigate,
    merit_order,
    select_hours,
)
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

START = datetime(2026, 9, 30, 22, tzinfo=UTC)  # 1 Oct 2026 00:00 in Berlin (CEST)
DAYS = 29  # 1..29 October: the 28 days before the 29th, and the 29th


def hours(n: int = DAYS * 24) -> list[datetime]:
    return [START + timedelta(hours=i) for i in range(n)]


def test_hourly_means_of_quarter_hours_need_half_the_values() -> None:
    t = datetime(2026, 10, 1, 10, tzinfo=UTC)
    points = [Point(timestamp=t + timedelta(minutes=15 * i), value=v)
              for i, v in enumerate([10, 20, None, None, 1, None, None, None])]  # fmt: skip
    series = Series(id="x", unit="MW", interval_minutes=15, points=points)
    assert hourly(series) == {t: 15.0}  # the second hour has only 1 of 4 values


def test_comparable_days_are_earlier_days_of_the_same_kind() -> None:
    sel = select_hours(set(hours()), date(2026, 10, 29), 17, 18)  # a Thursday
    assert len(sel.event) == 1
    assert all(d.weekday() < 5 and d < date(2026, 10, 29) for d in sel.comparable)
    assert len(sel.comparable) == 20  # weekdays from 1 to 28 October
    assert all(h.astimezone(BERLIN).date() != date(2026, 10, 29) for h in sel.fit)
    assert len(sel.fit) == 28 * 24 + 1  # the event day is never in the fit (DST: 25 h day)


def test_merit_order_line_is_exact_on_a_perfect_line() -> None:
    hs = hours()
    residual = {h: 30_000 + 1000 * (i % 30) for i, h in enumerate(hs)}  # MW
    price = {h: 10 + 5 * residual[h] / 1000 for h in hs}  # 5 EUR/MWh per GW
    sel = select_hours(set(hs), date(2026, 10, 29), 17, 18)
    order = merit_order(price, residual, sel)
    assert order["eur_per_mwh_per_gw_residual_load"] == 5 and order["r_squared"] == 1
    assert order["unexplained_eur_per_mwh"] == 0


def series_world(event_wind: float, event_price: float) -> dict:
    """4 weeks of steady hours; the event hour has `event_wind` and `event_price`."""
    hs = hours()
    event = datetime(2026, 10, 29, 16, tzinfo=UTC)  # 17:00 in Berlin (CET after 25 Oct)
    wind = {h: 15_000 + 500 * (h.day % 5) for h in hs}
    load = {h: 60_000 + 300 * (h.day % 4) for h in hs}
    wind[event] = event_wind
    residual = {h: load[h] - wind[h] for h in hs}
    price = {h: 20 + 3 * residual[h] / 1000 + (h.day % 3) for h in hs}
    price[event] = event_price
    return {"price": price, "wind": wind, "load": load, "residual_load": residual}


def test_conditions_and_system_responses_are_kept_apart() -> None:
    world = series_world(event_wind=2_000, event_price=400)
    world["fossil_gas"] = {h: 8_000.0 for h in world["price"]}
    world["fossil_gas"][datetime(2026, 10, 29, 16, tzinfo=UTC)] = 15_000.0
    result = investigate(date(2026, 10, 29), 17, 18, date(2026, 10, 1), world)
    conditions = {c["driver"].split(" (")[0]: c for c in result["conditions_by_unusualness"]}
    assert conditions["Wind generation"]["consistent_with_price_move"] is True  # less wind
    assert conditions["Residual load"]["difference"] > 0
    assert [r["driver"] for r in result["system_responses"]] == ["Gas generation"]
    assert "Solar generation" in result["not_available"]  # missing evidence is named
    assert result["caveats"][0] == STEEP_TOP  # the highest residual load of the 4 weeks


def test_a_cheap_event_is_read_the_other_way_round() -> None:
    world = series_world(event_wind=40_000, event_price=-5)
    result = investigate(date(2026, 10, 29), 17, 18, date(2026, 10, 1), world)
    wind = next(c for c in result["conditions_by_unusualness"] if c["driver"].startswith("Wind"))
    assert result["price"]["difference"] < 0 and wind["consistent_with_price_move"] is True
    assert STEEP_TOP not in result["caveats"]


# ---------- the tool on recorded October data ----------


def box() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))


def test_october_peak_on_real_data() -> None:
    raw = box().execute("investigate_price_event", {"date": "2026-10-29", "from_hour": 17,
                                                    "to_hour": 18}).content  # fmt: skip
    result = json.loads(raw)
    assert (
        result["ref"] == "event_1" and result["comparison"]["period"] == "2026-10-01 to 2026-10-28"
    )
    assert result["price"]["comparable_days_below"] == 20  # dearer than every comparable day
    assert result["conditions_by_unusualness"][0]["driver"].startswith("Residual load")
    assert {r["driver"].split(" ")[0] for r in result["system_responses"]} >= {"Gas", "Lignite"}


@pytest.mark.parametrize(("args", "message"), [
    ({"date": "2026-11-14", "from_hour": 17, "to_hour": 18}, "past days only"),
    ({"date": "2026-10-29", "from_hour": 18, "to_hour": 17}, "exclusive"),
    ({"date": "2026-10-29", "from_hour": 17, "to_hour": 25}, "Invalid input"),
    ({"date": "2026-10-06", "from_hour": 19, "to_hour": 20}, "No data"),  # no 4 weeks recorded
])  # fmt: skip
def test_invalid_or_uncovered_events_are_explained(args: dict, message: str) -> None:
    result = box().execute("investigate_price_event", args)
    assert result.is_error and message in result.content


def test_investigation_gets_method_step_charts_and_sources() -> None:
    from test_loop import AGENTS_DIR

    from gridagent import services
    from gridagent.agent.loop import Agent
    from gridagent.evals.offline import oracle_llm
    from gridagent.evals.session import load_agent_eval_set
    from gridagent.garden.registry import resolve

    agent = resolve(AGENTS_DIR, "energy_qa@0.11.0")
    eval_set, _ = load_agent_eval_set(agent)
    case = next(c for c in eval_set.cases if c.id == "investigate_october_peak")
    data = FixtureDataSource(FIXTURE_DIR)
    log = Agent(agent, oracle_llm(case), data, today=lambda: eval_set.today).run(case.question)
    assert services.method_steps(log)[0].startswith("Compared the price on 29 Oct 2026, 17:00")
    price, residual = services.charts_for_run(log, data)
    assert residual.title.startswith("Residual load") and price.unit == "EUR / MWh"
    assert any("Wetterdienst" in s["name"] for s in services.sources_for_run(log))
    assert log.unsupported_numbers == []
