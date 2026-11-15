"""Battery arbitrage and shiftable load: the optimiser against brute force and by hand."""

import itertools
import json
import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Point, Series
from gridagent.tools.flexibility import (
    Battery,
    make_grid,
    optimise_day,
    plan_load,
    simulate_battery,
)
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

DAY = date(2026, 11, 2)


def hourly(prices: list[float | None], days: int = 1) -> Series:
    start = datetime(2026, 11, 2, tzinfo=UTC)
    points = [Point(timestamp=start + timedelta(hours=i), value=v)
              for i, v in enumerate(prices * days)]  # fmt: skip
    return Series(id="day_ahead_price", unit="EUR / MWh", interval_minutes=60, points=points)


def brute_force(prices: list[float], battery: Battery) -> float:
    """Try every sequence of moves on the same grid; keep the best that ends empty."""
    grid = make_grid(battery, 1.0)
    eta = math.sqrt(battery.round_trip_efficiency)
    best = float("-inf")
    for moves in itertools.product(range(-grid.max_move, grid.max_move + 1), repeat=len(prices)):
        level, cash = 0, 0.0
        for move, price in zip(moves, prices, strict=True):
            level += move
            if not 0 <= level <= grid.levels:
                break
            stored = move * grid.step
            grid_energy = stored / eta if stored > 0 else stored * eta
            cash -= grid_energy * price
            if grid_energy < 0:
                cash -= battery.wear_eur_per_mwh * -grid_energy
        else:
            if level == 0:
                best = max(best, cash)
    return best


@pytest.mark.parametrize("seed", range(5))
def test_optimiser_matches_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    prices = [round(rng.uniform(-20, 200), 2) for _ in range(5)]
    battery = Battery(power_mw=1, capacity_mwh=1.5, round_trip_efficiency=0.85,
                      wear_eur_per_mwh=rng.choice([0, 10]))  # fmt: skip
    result = optimise_day(DAY, prices, battery, make_grid(battery, 1.0))
    assert result.profit_eur == pytest.approx(brute_force(prices, battery))


def test_buy_low_sell_high_by_hand() -> None:
    battery = Battery(power_mw=1, capacity_mwh=1, round_trip_efficiency=0.81)  # 0.9 each way
    run = simulate_battery(hourly([10, 100]), battery)
    # buy 1 / 0.9 MWh at 10, sell 0.9 MWh at 100
    assert run.summary()["profit_eur"] == pytest.approx(round(90 - 10 / 0.9, 2))
    assert run.grid_mwh == pytest.approx([1 / 0.9, -0.9])


def test_flat_prices_earn_nothing_and_losses_make_small_spreads_unprofitable() -> None:
    battery = Battery(power_mw=1, capacity_mwh=2)
    assert simulate_battery(hourly([50] * 24), battery).summary()["profit_eur"] == 0
    # 90 % round trip: buying at 100 and selling at 105 loses money, so the battery idles
    run = simulate_battery(hourly([100, 105]), battery)
    assert run.summary()["profit_eur"] == 0 and run.summary()["full_cycles"] == 0


def test_limits_hold_and_every_day_ends_empty() -> None:
    rng = random.Random(1)
    prices = [rng.uniform(0, 300) for _ in range(96)]
    series = Series(id="p", unit="EUR / MWh", interval_minutes=15, points=[
        Point(timestamp=datetime(2026, 11, 2, tzinfo=UTC) + timedelta(minutes=15 * i), value=v)
        for i, v in enumerate(prices)])  # fmt: skip
    battery = Battery(power_mw=10, capacity_mwh=20)
    run = simulate_battery(series, battery)
    assert run.grid.exact
    assert all(0 <= s <= 20 + 1e-9 for s in run.stored) and run.stored[-1] == 0
    previous = 0.0
    for level in run.stored:  # never more than 10 MW x 0.25 h into or out of the cells
        assert abs(level - previous) <= 2.5 + 1e-9
        previous = level


def test_missing_price_means_no_trading_in_that_interval() -> None:
    run = simulate_battery(hourly([10, None, 100]), Battery(power_mw=1, capacity_mwh=1))
    assert run.grid_mwh[1] == 0


def test_wear_cost_reduces_cycling() -> None:
    prices = [50, 120, 60, 130] * 6
    free = simulate_battery(hourly(prices), Battery(1, 1)).summary()
    worn = simulate_battery(hourly(prices), Battery(1, 1, wear_eur_per_mwh=80)).summary()
    assert worn["full_cycles"] < free["full_cycles"] and worn["profit_eur"] < free["profit_eur"]


def test_days_are_optimised_separately() -> None:
    day = [100.0] * 6 + [10.0] * 12 + [200.0] * 6  # cheap midday, expensive evening
    one = simulate_battery(hourly(day), Battery(1, 1)).days[0].profit_eur
    three = simulate_battery(hourly(day, days=3), Battery(1, 1))
    assert [d.day for d in three.days] == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
    assert all(d.stored[-1] == 0 for d in three.days)  # nothing carried over midnight
    assert [d.profit_eur for d in three.days] == pytest.approx([one] * 3)


def test_unrealistic_batteries_are_refused() -> None:
    with pytest.raises(ValueError, match="24 h"):
        simulate_battery(hourly([1, 2]), Battery(power_mw=1, capacity_mwh=100))
    with pytest.raises(ValueError, match="efficiency"):
        simulate_battery(hourly([1, 2]), Battery(1, 1, round_trip_efficiency=1.2))


def test_capacity_off_the_grid_is_flagged_not_exact() -> None:
    assert not make_grid(Battery(power_mw=1, capacity_mwh=1.1), 1.0).exact
    assert make_grid(Battery(power_mw=10, capacity_mwh=20), 0.25).exact


# ---------- shiftable load ----------


def test_load_takes_the_cheapest_hours() -> None:
    plan = plan_load(hourly([50, 10, 40, 20]), energy_mwh=3, max_power_mw=2)
    # 2 MWh at 10, then 1 MWh at 20 -> 40 EUR; evenly: 3 x mean(30) = 90 EUR
    assert (plan["cost_eur"], plan["even_running_cost_eur"], plan["saving_eur"]) == (40, 90, 50)
    assert [b["power_mw"] for b in plan["blocks"]] == [2, 1]
    assert plan["blocks"][0]["until"] == "2026-11-02T02:00:00+00:00"


def test_load_that_does_not_fit_is_refused() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        plan_load(hourly([1, 2]), energy_mwh=5, max_power_mw=2)


def test_no_saving_percentage_on_a_non_positive_cost() -> None:
    assert plan_load(hourly([-10, 0]), energy_mwh=1, max_power_mw=1)["saving_pct"] is None


# ---------- as tools ----------


def toolbox() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))


def week_prices(box: ToolBox) -> str:
    args = {"start": "2026-11-02", "end": "2026-11-08"}
    return json.loads(box.execute("get_day_ahead_price", args).content)["ref"]


def test_battery_tool_stores_a_schedule_for_compute_stats() -> None:
    box = toolbox()
    args = {"ref": week_prices(box), "power_mw": 10, "capacity_mwh": 20,
            "round_trip_efficiency": None, "wear_cost_eur_per_mwh": None}  # fmt: skip
    result = json.loads(box.execute("simulate_battery", args).content)
    assert result["ref"] == "battery_2" and result["exact"] and result["zone"] == "DE-LU"
    assert result["battery"]["round_trip_efficiency"] == 0.9 and result["days"] == 7
    stats = {"ref": result["ref"], "operation": "max", "source": "stored_energy",
             "relative_to": None, "per_day": False, "threshold": None,
             "weight_ref": None, "weight_source": None}  # fmt: skip
    assert json.loads(box.execute("compute_stats", stats).content)["value"] == 20


def test_flexibility_tools_need_a_price_ref() -> None:
    box = toolbox()
    gen = json.loads(
        box.execute(
            "get_generation",
            {"start": "2026-11-02", "end": "2026-11-08", "sources": None, "scope": None},
        ).content
    )
    result = box.execute("plan_flexible_load", {"ref": gen["ref"], "energy_mwh": 1,
                                                "max_power_mw": 1})  # fmt: skip
    assert result.is_error and "price ref" in result.content
    bad = box.execute("simulate_battery", {"ref": gen["ref"], "power_mw": -1, "capacity_mwh": 1,
                                           "round_trip_efficiency": None,
                                           "wear_cost_eur_per_mwh": None})  # fmt: skip
    assert bad.is_error


# ---------- on the website ----------


def test_battery_answer_gets_method_steps_and_charts() -> None:
    from test_loop import AGENTS_DIR

    from gridagent import services
    from gridagent.agent.loop import Agent
    from gridagent.evals.offline import oracle_llm
    from gridagent.evals.session import load_agent_eval_set
    from gridagent.garden.registry import resolve

    agent = resolve(AGENTS_DIR, "energy_qa@0.9.0")
    eval_set, _ = load_agent_eval_set(agent)
    case = next(c for c in eval_set.cases if c.id == "battery_last_week")
    data = FixtureDataSource(FIXTURE_DIR)
    log = Agent(agent, oracle_llm(case), data, today=lambda: eval_set.today).run(case.question)
    steps = services.method_steps(log)
    assert steps[1].startswith("Optimised a 10 MW / 20 MWh battery day by day")
    price, stored = services.charts_for_run(log, data)
    assert price.title.startswith("Day-ahead electricity price")
    assert stored.title == "Battery: stored energy, MWh" and stored.unit == "MWh"
    assert log.unsupported_numbers == []
