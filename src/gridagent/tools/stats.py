"""Deterministic statistics over time series.

The LLM must never do arithmetic in its head: every number in an answer
should come from here. Missing values (`None`) are skipped and counted, so
the agent can report incomplete data instead of hiding it.
"""

from collections import defaultdict
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel

from gridagent.data.models import Point, Series

ENERGY_UNITS = {"MW": "MWh", "GW": "GWh"}  # power unit -> energy unit


class Operation(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    ENERGY = "energy"  # power over time: MW -> MWh, GW -> GWh
    SHARE = "share"  # sum(series) / sum(total) in %, needs `total`
    COUNT_BELOW = "count_below"  # hours with value < threshold, needs `threshold`
    COUNT_ABOVE = "count_above"  # hours with value > threshold, needs `threshold`
    CAPTURE_PRICE = "capture_price"  # generation-weighted average price, needs `weights`
    CAPACITY_FACTOR = "capacity_factor"  # see capacity_factor()


class StatsResult(BaseModel):
    """Result of one calculation, with enough context to explain it."""

    operation: Operation
    series_id: str
    value: float | None  # None if there was no usable data
    unit: str
    timestamp: datetime | None = None  # when the min/max occurred
    n_points: int  # values used
    n_missing: int  # values skipped because they were None
    start: date | None
    end: date | None
    detail: dict[str, float | str] = {}  # extra context, e.g. threshold or capacity used


def compute_stats(
    series: Series,
    operation: Operation,
    total: Series | None = None,
    threshold: float | None = None,
    weights: Series | None = None,
) -> StatsResult:
    """Compute one statistic over a series.

    - sum/mean/min/max/count: over the non-missing values, in the series unit.
    - energy: sum(power) * interval hours (MW -> MWh, GW -> GWh).
    - share: 100 * sum(series) / sum(total), using only timestamps where both
      have a value. Because all intervals have equal length, a ratio of summed
      power equals the ratio of energy.
    - count_below / count_above: hours in which the value is strictly below /
      above `threshold` (e.g. negative prices: count_below with threshold 0).
    - capture_price: sum(price * generation) / sum(generation) over matching
      timestamps; `series` is the price, `weights` the generation. This is how
      the official monthly market values ("Marktwerte") are defined.
    """
    operation = Operation(operation)
    if operation is Operation.SHARE:
        return _share(series, total)
    if operation is Operation.CAPTURE_PRICE:
        return _capture_price(series, weights)
    if operation is Operation.CAPACITY_FACTOR:
        raise ValueError("use capacity_factor() for capacity factors")

    valid = [p for p in series.points if p.value is not None]
    result = StatsResult(
        operation=operation,
        series_id=series.id,
        value=None,
        unit=series.unit,
        n_points=len(valid),
        n_missing=len(series.points) - len(valid),
        start=series.start,
        end=series.end,
    )
    if operation is Operation.COUNT:
        result.value = float(len(valid))
        result.unit = "points"
        return result
    if operation in (Operation.COUNT_BELOW, Operation.COUNT_ABOVE):
        return _count_threshold(series, operation, threshold, result, valid)
    if not valid:
        return result

    values = [p.value for p in valid if p.value is not None]  # typed: no None left
    if operation is Operation.SUM:
        result.value = sum(values)
    elif operation is Operation.MEAN:
        result.value = sum(values) / len(values)
    elif operation in (Operation.MIN, Operation.MAX):
        pick = min if operation is Operation.MIN else max
        best = valid[pick(range(len(values)), key=values.__getitem__)]
        result.value, result.timestamp = best.value, best.timestamp
    elif operation is Operation.ENERGY:
        if series.unit not in ENERGY_UNITS or not series.interval_minutes:
            raise ValueError("energy needs a MW or GW series with a fixed interval")
        result.value = sum(values) * series.interval_minutes / 60
        result.unit = ENERGY_UNITS[series.unit]
    return result


def _count_threshold(
    series: Series,
    operation: Operation,
    threshold: float | None,
    result: StatsResult,
    valid: list[Point],
) -> StatsResult:
    if threshold is None:
        raise ValueError(f"{operation.value} needs a threshold")
    if not series.interval_minutes:
        raise ValueError(f"{operation.value} needs a series with a fixed interval")
    if operation is Operation.COUNT_BELOW:
        hits = [p for p in valid if p.value is not None and p.value < threshold]
    else:
        hits = [p for p in valid if p.value is not None and p.value > threshold]
    result.value = len(hits) * series.interval_minutes / 60
    result.unit = "h"
    result.detail = {"threshold": threshold, "threshold_unit": series.unit,
                     "intervals": float(len(hits))}  # fmt: skip
    return result


def _share(series: Series, total: Series | None) -> StatsResult:
    if total is None:
        raise ValueError("share needs a `total` series")
    if len(series.points) != len(total.points):
        raise ValueError("share needs two series over the same timestamps")
    pairs = [
        (p.value, t.value)
        for p, t in zip(series.points, total.points, strict=True)
        if p.value is not None and t.value is not None
    ]
    denominator = sum(t for _, t in pairs)
    return StatsResult(
        operation=Operation.SHARE,
        series_id=f"{series.id}/{total.id}",
        value=100 * sum(p for p, _ in pairs) / denominator if denominator else None,
        unit="%",
        n_points=len(pairs),
        n_missing=len(series.points) - len(pairs),
        start=series.start,
        end=series.end,
    )


def _capture_price(price: Series, generation: Series | None) -> StatsResult:
    if generation is None:
        raise ValueError("capture_price needs a generation series as weights")
    # Keyed by UTC: in German time the repeated hour when clocks go back (02:00-03:00
    # twice) would compare equal and the two hours would overwrite each other.
    weight_at = {p.timestamp.astimezone(UTC): p.value for p in generation.points}
    pairs: list[tuple[float, float]] = []
    for p in price.points:
        weight = weight_at.get(p.timestamp.astimezone(UTC))
        if p.value is not None and weight is not None:
            pairs.append((p.value, weight))
    weight_sum = sum(w for _, w in pairs)
    value = sum(v * w for v, w in pairs) / weight_sum if weight_sum else None
    # Context analysts always ask for: the plain (time-weighted) average price and the
    # capture rate (market value / average price, "Wertigkeit"), computed here so the
    # answer never has to work them out.
    prices = [p.value for p in price.points if p.value is not None]
    average = sum(prices) / len(prices) if prices else None
    detail: dict = {"weighted_by": generation.id}
    if average is not None:
        detail["average_price"] = round(average, 2)
        detail["average_price_ct_per_kwh"] = round(average / 10, 3)
        if value is not None and average > 0:
            detail["capture_rate_pct"] = round(100 * value / average, 2)
    return StatsResult(
        operation=Operation.CAPTURE_PRICE,
        series_id=f"{price.id}@{generation.id}",
        value=value,
        unit=price.unit,
        n_points=len(pairs),
        n_missing=len(price.points) - len(pairs),
        start=price.start,
        end=price.end,
        detail=detail,
    )


def capacity_factor(generation: Series, capacity_mw: float, capacity_note: str) -> StatsResult:
    """Energy produced / (installed capacity x hours covered), in %.

    `capacity_note` says which capacity figure was used (date, AC/DC), so the
    answer can state it: capacity factors depend on that choice.
    """
    if generation.unit != "MW" or not generation.interval_minutes:
        raise ValueError("capacity_factor needs a MW generation series with a fixed interval")
    if capacity_mw <= 0:
        raise ValueError("installed capacity must be positive")
    valid = [p.value for p in generation.points if p.value is not None]
    hours = len(valid) * generation.interval_minutes / 60
    energy_mwh = sum(valid) * generation.interval_minutes / 60
    return StatsResult(
        operation=Operation.CAPACITY_FACTOR,
        series_id=generation.id,
        value=100 * energy_mwh / (capacity_mw * hours) if hours else None,
        unit="%",
        n_points=len(valid),
        n_missing=len(generation.points) - len(valid),
        start=generation.start,
        end=generation.end,
        detail={
            "capacity_mw": capacity_mw,
            "capacity_used": capacity_note,
            "hours": hours,
        },  # fmt: skip
    )


def daily(series: Series, operation: Operation) -> list[StatsResult]:
    """Apply `operation` to each local calendar day separately (e.g. daily energy)."""
    if Operation(operation) in (
        Operation.SHARE, Operation.CAPTURE_PRICE, Operation.CAPACITY_FACTOR,
        Operation.COUNT_BELOW, Operation.COUNT_ABOVE,
    ):  # fmt: skip
        raise ValueError(f"per_day is not supported for {Operation(operation).value}")
    by_day: dict[date, list[Point]] = defaultdict(list)
    for p in series.points:
        by_day[p.timestamp.date()].append(p)  # timestamps carry Berlin offsets
    return [
        compute_stats(series.model_copy(update={"points": pts}), operation)
        for _, pts in sorted(by_day.items())
    ]
