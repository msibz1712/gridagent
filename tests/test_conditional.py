"""Conditions -> effect (tools/conditional.py and the compute_when tool)."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Point, Series
from gridagent.tools.conditional import Condition, hourly_means, stats_when
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

T0 = datetime(2026, 11, 2, 0, tzinfo=UTC)


def hourly(series_id: str, unit: str, values: list[float | None]) -> Series:
    points = [Point(timestamp=T0 + timedelta(hours=i), value=v) for i, v in enumerate(values)]
    return Series(id=series_id, unit=unit, interval_minutes=60, points=points)


WIND = hourly("wind_speed", "m/s", [2.0, 6.0, 1.0, None, 2.5])
PRICE = hourly("day_ahead_price", "EUR / MWh", [200.0, 50.0, 300.0, 400.0, 100.0])


def test_quarter_hours_become_hourly_means_in_utc() -> None:
    points = [Point(timestamp=T0 + timedelta(minutes=15 * i), value=v)
              for i, v in enumerate([10, 20, 30, 40, 1, None, None, None])]  # fmt: skip
    means = hourly_means(Series(id="gas", unit="MW", interval_minutes=15, points=points))
    assert means == {T0: 25.0}  # second hour has 1 of 4 values: dropped


def test_repeated_hour_when_clocks_go_back_stays_two_hours() -> None:
    first = datetime(2026, 10, 25, 0, tzinfo=UTC)  # 02:00 CEST, then 02:00 CET
    series = Series(id="x", unit="MW", interval_minutes=60, points=[
        Point(timestamp=(first + timedelta(hours=h)).astimezone(), value=float(h))
        for h in range(2)])  # fmt: skip
    assert len(hourly_means(series)) == 2


def test_mean_price_in_calm_hours() -> None:
    result = stats_when("mean", [Condition(WIND, below=3)], PRICE)
    # calm hours: 0 (200), 2 (300), 4 (100); hour 3 has no wind value -> not considered
    assert result["value"] == 200.0
    assert (result["hours_selected"], result["hours_considered"]) == (3, 4)
    assert result["share_of_hours_pct"] == 75.0


def test_hours_needs_no_target_and_conditions_combine_with_and() -> None:
    sun = hourly("solar_radiation", "W/m²", [0.0, 0.0, 300.0, 0.0, 10.0])
    result = stats_when("hours", [Condition(WIND, below=3), Condition(sun, below=50)])
    assert result["value"] == 2 and result["unit"] == "h"  # hours 0 and 4


def test_between_min_max_with_time_and_energy() -> None:
    between = stats_when("max", [Condition(WIND, above=1.5, below=3)], PRICE)
    assert between["value"] == 200.0 and between["at"] == "2026-11-02T01:00:00+01:00"
    gas = hourly("gas", "MW", [1000.0, 500.0, 2000.0, 0.0, 1500.0])
    assert stats_when("energy", [Condition(WIND, below=3)], gas)["value"] == 4500.0  # MWh
    assert stats_when("min", [Condition(WIND, above=10)], PRICE)["value"] is None


def test_invalid_requests_are_explained() -> None:
    with pytest.raises(ValueError, match="needs `below` and/or `above`"):
        Condition(WIND)
    with pytest.raises(ValueError, match="needs a target"):
        stats_when("mean", [Condition(WIND, below=3)])
    with pytest.raises(ValueError, match="power series"):
        stats_when("energy", [Condition(WIND, below=3)], PRICE)
    late = WIND.model_copy(
        update={
            "points": [
                Point(timestamp=p.timestamp + timedelta(days=30), value=1.0) for p in WIND.points
            ]
        }
    )
    with pytest.raises(ValueError, match="do not overlap"):
        stats_when("mean", [Condition(late, below=3)], PRICE)


# ---------- through the tool, on recorded data ----------


def run(box: ToolBox, name: str, args: dict) -> dict:
    result = box.execute(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content)


def test_compute_when_tool_on_recorded_october() -> None:
    box = ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))
    weather = run(box, "get_weather", {"start": "2026-10-01", "end": "2026-10-31"})["ref"]
    price = run(box, "get_day_ahead_price", {"start": "2026-10-01", "end": "2026-10-31"})["ref"]
    calm = {"ref": weather, "source": "wind_speed", "below": 3, "above": None}
    result = run(box, "compute_when", {"operation": "mean", "ref": price, "source": None,
                                       "conditions": [calm]})  # fmt: skip
    assert result["hours_considered"] == 745 and result["value_ct_per_kwh"] > 0


def test_weather_only_up_to_yesterday() -> None:
    box = ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))
    result = box.execute("get_weather", {"start": "2026-11-14", "end": "2026-11-14"})
    assert result.is_error
