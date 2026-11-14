"""Deterministic statistics over time series.

The LLM must never do arithmetic in its head: every number in an answer
should come from here. Missing values (`None`) are skipped and counted, so
the agent can report incomplete data instead of hiding it.
"""

from collections import defaultdict
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from gridagent.data.models import Point, Series


class Operation(StrEnum):
    SUM = "sum"
    MEAN = "mean"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    ENERGY = "energy"  # MW over time -> MWh
    SHARE = "share"  # sum(series) / sum(total) in %, needs `total`


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


def compute_stats(series: Series, operation: Operation, total: Series | None = None) -> StatsResult:
    """Compute one statistic over a series.

    - sum/mean/min/max/count: over the non-missing values, in the series unit.
    - energy: sum(MW) * interval hours -> MWh. Only for MW series.
    - share: 100 * sum(series) / sum(total), using only timestamps where both
      have a value. Because all intervals have equal length, a ratio of summed
      power equals the ratio of energy.
    """
    operation = Operation(operation)
    if operation is Operation.SHARE:
        return _share(series, total)

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
    if not valid:
        return result

    values = [p.value for p in valid]
    if operation is Operation.SUM:
        result.value = sum(values)
    elif operation is Operation.MEAN:
        result.value = sum(values) / len(values)
    elif operation in (Operation.MIN, Operation.MAX):
        pick = min if operation is Operation.MIN else max
        best: Point = pick(valid, key=lambda p: p.value)
        result.value, result.timestamp = best.value, best.timestamp
    elif operation is Operation.ENERGY:
        if series.unit != "MW" or not series.interval_minutes:
            raise ValueError("energy needs a MW series with a fixed interval")
        result.value = sum(values) * series.interval_minutes / 60
        result.unit = "MWh"
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


def daily(series: Series, operation: Operation) -> list[StatsResult]:
    """Apply `operation` to each local calendar day separately (e.g. daily energy)."""
    if Operation(operation) is Operation.SHARE:
        raise ValueError("use compute_stats per day for shares")
    by_day: dict[date, list[Point]] = defaultdict(list)
    for p in series.points:
        by_day[p.timestamp.date()].append(p)  # timestamps carry Berlin offsets
    return [
        compute_stats(series.model_copy(update={"points": pts}), operation)
        for _, pts in sorted(by_day.items())
    ]
