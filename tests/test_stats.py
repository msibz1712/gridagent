"""compute_stats: exact maths on small synthetic series, sanity checks on real data."""

from datetime import date, datetime, timedelta

import pytest

from gridagent.data.models import Point, Series, TimeSeriesResponse
from gridagent.data.sources import source_series
from gridagent.tools.stats import Operation, compute_stats, daily

T0 = datetime.fromisoformat("2026-11-10T00:00:00+01:00")


def make_series(values: list[float | None], unit: str = "MW", sid: str = "x") -> Series:
    """15-minute series starting at T0."""
    return Series(
        id=sid,
        unit=unit,
        interval_minutes=15,
        points=[
            Point(timestamp=T0 + timedelta(minutes=15 * i), value=v) for i, v in enumerate(values)
        ],
    )


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (Operation.SUM, 100.0),
        (Operation.MEAN, 25.0),
        (Operation.MIN, 10.0),
        (Operation.MAX, 40.0),
        (Operation.COUNT, 4.0),
        (Operation.ENERGY, 25.0),  # 100 MW summed * 0.25 h
    ],
)
def test_basic_operations(operation: Operation, expected: float) -> None:
    result = compute_stats(make_series([10, 20, 30, 40]), operation)
    assert result.value == pytest.approx(expected)


def test_max_reports_when_it_happened() -> None:
    result = compute_stats(make_series([10, 50, 30]), Operation.MAX)
    assert result.timestamp == T0 + timedelta(minutes=15)


def test_energy_unit_is_mwh() -> None:
    assert compute_stats(make_series([4]), Operation.ENERGY).unit == "MWh"


def test_operation_accepts_plain_string() -> None:
    """The LLM will send strings like "mean"."""
    assert compute_stats(make_series([1, 3]), "mean").value == 2.0


def test_missing_values_are_skipped_and_counted() -> None:
    result = compute_stats(make_series([10, None, 30, None]), Operation.MEAN)
    assert result.value == 20.0
    assert (result.n_points, result.n_missing) == (2, 2)


def test_all_missing_gives_no_value_instead_of_zero() -> None:
    result = compute_stats(make_series([None, None]), Operation.SUM)
    assert result.value is None
    assert result.n_missing == 2


def test_energy_rejects_non_mw_series() -> None:
    with pytest.raises(ValueError, match="MW"):
        compute_stats(make_series([1.0], unit="EUR / MWh"), Operation.ENERGY)


def test_share() -> None:
    part, total = make_series([25, 25], sid="wind"), make_series([100, 100], sid="total")
    result = compute_stats(part, Operation.SHARE, total=total)
    assert result.value == pytest.approx(25.0)
    assert result.unit == "%"


def test_share_ignores_timestamps_missing_in_either_series() -> None:
    part = make_series([10, None, 30])
    total = make_series([100, 100, None])
    result = compute_stats(part, Operation.SHARE, total=total)
    assert result.value == pytest.approx(10.0)
    assert result.n_missing == 2


def test_share_requires_total() -> None:
    with pytest.raises(ValueError, match="total"):
        compute_stats(make_series([1.0]), Operation.SHARE)


# ---------- real fixture data ----------


def test_mean_price_matches_plain_python(yesterday_price: TimeSeriesResponse) -> None:
    prices = [row.values["day_ahead_price"] for row in yesterday_price.data]
    result = compute_stats(yesterday_price.get_series("day_ahead_price"), Operation.MEAN)
    assert result.value == pytest.approx(sum(prices) / len(prices))
    assert result.unit == "EUR / MWh"
    assert result.start == result.end == date(2026, 11, 13)


def test_daily_handles_dst_day(october_power: TimeSeriesResponse) -> None:
    """25 Oct 2026 has 25 hours (clocks go back) -> 100 quarter-hours, not 96."""
    solar = source_series(october_power, "solar")
    days = daily(solar, Operation.ENERGY)
    assert len(days) == 31
    dst_day = next(d for d in days if d.start == date(2026, 10, 25))
    assert dst_day.n_points == 100
    total = compute_stats(solar, Operation.ENERGY).value
    assert sum(d.value for d in days) == pytest.approx(total)


def test_wind_share_is_plausible(week_power: TimeSeriesResponse) -> None:
    share = compute_stats(
        source_series(week_power, "wind"), Operation.SHARE, total=source_series(week_power, "total")
    )
    assert 0 < share.value < 100


def test_capture_price_keeps_both_repeated_hours_when_clocks_go_back() -> None:
    """25 Oct 2026: 02:00-03:00 happens twice. Each hour must keep its own weight."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from gridagent.data.models import Point, Series

    berlin = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # 02:00 CEST
    times = [first.astimezone(berlin), (first + timedelta(hours=1)).astimezone(berlin)]
    assert times[0] == times[1]  # the trap: equal in local time

    def hourly(series_id: str, unit: str, values: list[float]) -> Series:
        points = [Point(timestamp=t, value=v) for t, v in zip(times, values, strict=True)]
        return Series(id=series_id, unit=unit, interval_minutes=60, points=points)

    price = hourly("p", "EUR / MWh", [10.0, 100.0])
    solar = hourly("g", "MW", [3.0, 1.0])
    result = compute_stats(price, Operation.CAPTURE_PRICE, weights=solar)
    assert result.value == (10 * 3 + 100 * 1) / 4  # 32.5; the bug gave (10+100)*1/2 = 55
