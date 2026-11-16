"""Anomaly engine: robust normal, merit order, records, streaks, shifts and the score."""

from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.models import Point, Series
from gridagent.monitor.anomalies import (
    median,
    negative_price_story,
    price_stories,
    quadratic_fit,
    record_stories,
    robust_scale,
    shift_stories,
    streak_stories,
)

DAY = date(2026, 11, 26)  # a Thursday in CET (UTC + 1 h all month)


def hourly(values_by_day: dict[date, list[float]], name: str = "x") -> Series:
    points = []
    for day, values in sorted(values_by_day.items()):
        start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=1)
        points += [Point(timestamp=start + timedelta(hours=h), value=v)
                   for h, v in enumerate(values)]  # fmt: skip
    return Series(id=name, unit="", interval_minutes=60, points=points)


def window(days: int = 28) -> list[date]:
    return [DAY - timedelta(days=i) for i in range(days, 0, -1)]


# ---------- statistics ----------


def test_one_extreme_day_does_not_widen_the_robust_normal() -> None:
    normal = [100.0, 102.0, 98.0, 101.0, 99.0, 100.0, 103.0, 97.0, 600.0]  # one past spike
    assert median(normal) == 100.0
    assert robust_scale(normal) < 5  # a standard deviation would be ~166
    assert robust_scale([5.0, 5.0, 5.0, 5.0, 9.0]) > 0  # MAD 0 -> standard deviation


def test_quadratic_fit_recovers_a_curve() -> None:
    xs = [float(x) for x in range(20, 70)]
    a, b, c = quadratic_fit(xs, [10 + 0.5 * x + 0.05 * x * x for x in xs])
    assert (a, b, c) == pytest.approx((10, 0.5, 0.05), abs=1e-6)


# ---------- price stories ----------


def market(event_price: float, event_residual: float) -> tuple[Series, Series]:
    """4 weeks on a convex merit order (price = 20 + 0.06 x^2, x in GW) with some noise;
    on DAY, hour 18 gets the given price and residual load."""
    prices, residual = {}, {}
    for i, d in enumerate([*window(), DAY]):
        loads = [30 + 15 * (h in range(7, 21)) + (i * 7 + h * 3) % 9 for h in range(24)]
        residual[d] = [x * 1000 for x in loads]
        prices[d] = [20 + 0.06 * x * x + ((i * 5 + h) % 7) - 3 for h, x in enumerate(loads)]
    residual[DAY][18] = event_residual * 1000
    prices[DAY][18] = event_price
    return hourly(prices), hourly(residual)


def test_a_spike_the_merit_order_predicts_is_explained() -> None:
    price, residual = market(event_price=20 + 0.06 * 75**2, event_residual=75)  # 357.50
    [story] = price_stories(DAY, price, residual)
    assert story.kind == "price_spike" and story.explained_by_fundamentals is True
    assert "in line with the residual load" in story.title


def test_a_spike_with_normal_residual_load_is_unexplained_and_ranks_higher() -> None:
    explained = price_stories(DAY, *market(event_price=357.5, event_residual=75))[0]
    surprising = price_stories(DAY, *market(event_price=357.5, event_residual=50))[0]
    assert surprising.explained_by_fundamentals is False
    assert surprising.facts["gap_to_merit_order_z"] > 2.5
    assert surprising.score > explained.score


def test_a_spike_needs_a_high_robust_z_and_50_eur() -> None:
    price, residual = market(event_price=20 + 0.06 * 50**2 + 30, event_residual=50)  # +30 EUR
    assert price_stories(DAY, price, residual) == []


def test_negative_prices() -> None:
    values = [80.0] * 24
    values[12:14] = [-5.0, -20.0]
    story = negative_price_story(DAY, hourly({DAY: values}))
    assert story.facts == {"date": "2026-11-26", "negative_hours": 2.0, "lowest_eur_mwh": -20.0}
    assert (story.from_hour, story.to_hour) == (12, 14)


# ---------- history ----------


def history(values: dict[date, float], key: str = "wind_gwh") -> dict[date, dict]:
    return {d: {key: v} for d, v in values.items()}


def days_before(n: int) -> list[date]:
    return [DAY - timedelta(days=i) for i in range(n, 0, -1)]


def test_record_names_the_last_day_that_was_higher() -> None:
    past = {d: 300.0 for d in days_before(200)}
    past[DAY - timedelta(days=150)] = 900.0
    stories = record_stories(DAY, history(past | {DAY: 800.0}))
    [record] = [s for s in stories if s.facts["direction"] == "highest"]
    assert record.facts["previous_date"] == str(DAY - timedelta(days=150))
    assert record.facts["days_spanned"] == 150 and "the highest since" in record.title


def test_record_without_a_higher_day_says_at_least() -> None:
    past = {d: 300.0 + (d.day % 5) for d in days_before(100)}
    [record] = [s for s in record_stories(DAY, history(past | {DAY: 800.0}))
                if s.facts["direction"] == "highest"]  # fmt: skip
    assert "since at least" in record.title  # never "ever": history was only loaded so far


def test_no_record_for_a_short_span() -> None:
    past = {d: 300.0 for d in days_before(100)}
    past[DAY - timedelta(days=20)] = 900.0  # beaten only 20 days ago
    assert [s for s in record_stories(DAY, history(past | {DAY: 800.0}))
            if s.facts["direction"] == "highest"] == []  # fmt: skip


def test_streaks_are_news_at_milestones_and_when_they_end() -> None:
    past = {d: 100.0 + (d.toordinal() % 50) for d in days_before(200)}
    run = {DAY - timedelta(days=i): 1000.0 for i in range(0, 5)}  # 5 days at the top
    h = history(past | run)
    assert [s.facts["days_in_a_row"] for s in streak_stories(DAY, h)] == [5]
    day4 = {d: v for d, v in h.items() if d < DAY}
    assert streak_stories(DAY - timedelta(days=1), day4) == []  # day 4: no milestone
    ended = history(past | run | {DAY + timedelta(days=1): 110.0})
    [end] = streak_stories(DAY + timedelta(days=1), ended)
    assert end.kind == "streak_end" and end.facts["run_days"] == 5


def test_streak_thresholds_never_use_later_days() -> None:
    past = {d: 100.0 + (d.toordinal() % 50) for d in days_before(200)}
    run = {DAY - timedelta(days=i): 1000.0 for i in range(0, 3)}
    future = {DAY + timedelta(days=i): 5000.0 for i in range(1, 100)}  # would raise the bar
    assert streak_stories(DAY, history(past | run | future))[0].facts["days_in_a_row"] == 3


def test_a_day_to_day_shift_is_news_only_when_unusual() -> None:
    past = {d: 300.0 + 10 * (d.toordinal() % 3) for d in days_before(120)}
    calm = history(past | {DAY: 320.0})
    assert shift_stories(DAY, calm) == []
    [shift] = shift_stories(DAY, history(past | {DAY: 900.0}))
    assert shift.facts["change"] > 500 and "rose by" in shift.title
