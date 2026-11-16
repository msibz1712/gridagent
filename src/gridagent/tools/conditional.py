"""Conditions -> effect: calculate on one series in the hours that other series select.

Examples: "mean day-ahead price in hours with wind below 3 m/s", "gas generation in
hours with neither wind nor sun" (a Dunkelflaute indicator), "how many such hours".

Everything is compared hour by hour in UTC:
- every series is first turned into hourly means (15-minute power -> 4 values per hour;
  hourly weather and hourly prices stay as they are). An hour counts only if at least
  half of its values exist.
- an hour is "selected" if every condition holds in it; hours where any involved series
  has no value are left out and reported, so gaps are never silently counted as matches.
UTC avoids the repeated hour when clocks go back (it would merge two hours).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from gridagent.data.models import Series

BERLIN = ZoneInfo("Europe/Berlin")
OPERATIONS = ("hours", "mean", "min", "max", "energy")
ENERGY_UNITS = {"MW": "MWh", "GW": "GWh"}


@dataclass(frozen=True)
class Condition:
    series: Series
    below: float | None = None  # value < below
    above: float | None = None  # value > above
    meaning: str = ""  # plain words for signs that are easy to misread, e.g. price spreads

    def __post_init__(self) -> None:
        if self.below is None and self.above is None:
            raise ValueError(f"condition on {self.series.id} needs `below` and/or `above`")

    def holds(self, value: float) -> bool:
        return (self.below is None or value < self.below) and (
            self.above is None or value > self.above
        )

    def describe(self) -> str:
        parts = []
        if self.above is not None:
            parts.append(f"> {self.above:g}")
        if self.below is not None:
            parts.append(f"< {self.below:g}")
        text = f"{self.series.id} {' and '.join(parts)} {self.series.unit}".strip()
        return f"{text} ({self.meaning})" if self.meaning else text


def hourly_means(series: Series) -> dict[datetime, float]:
    """{UTC hour start: mean of the values in that hour} (hours with < half the values dropped)."""
    per_hour = 60 // series.interval_minutes if series.interval_minutes else 1
    buckets: dict[datetime, list[float | None]] = {}
    for point in series.points:
        hour = point.timestamp.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(point.value)
    means = {}
    for hour, values in buckets.items():
        found = [v for v in values if v is not None]
        if found and len(found) * 2 >= per_hour:
            means[hour] = sum(found) / len(found)
    return means


def stats_when(operation: str, conditions: list[Condition], target: Series | None = None) -> dict:
    """Apply `operation` to `target` in the hours where every condition holds.

    operation "hours" counts the selected hours and needs no target; the others
    (mean, min, max, energy) are computed on the target's hourly means.
    """
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}")
    if not conditions:
        raise ValueError("at least one condition is needed")
    if operation != "hours" and target is None:
        raise ValueError(f"'{operation}' needs a target series (ref + source)")
    if operation == "energy" and target is not None and target.unit not in ENERGY_UNITS:
        raise ValueError(f"energy needs a power series in MW or GW, not {target.unit}")

    condition_hours = [hourly_means(c.series) for c in conditions]
    target_hours = hourly_means(target) if target is not None else None
    usable = set.intersection(*(set(h) for h in condition_hours))
    if target_hours is not None:
        usable &= set(target_hours)
    if not usable:
        raise ValueError("the condition and target data do not overlap in time")

    selected = sorted(
        hour
        for hour in usable
        if all(c.holds(hours[hour]) for c, hours in zip(conditions, condition_hours, strict=True))
    )
    result: dict = {
        "operation": operation,
        "conditions": [c.describe() for c in conditions],
        "hours_selected": len(selected),
        "hours_considered": len(usable),
        "share_of_hours_pct": round(100 * len(selected) / len(usable), 2),
        "period": f"{_local(min(usable)):%Y-%m-%d %H:%M} to {_local(max(usable)):%Y-%m-%d %H:%M}",
    }
    if operation == "hours":
        result.update(value=len(selected), unit="h")
        return result

    assert target is not None and target_hours is not None  # checked at the top
    result["series"] = target.id
    values = [target_hours[hour] for hour in selected]
    if not values:
        result.update(value=None, unit=target.unit, note="no hour met the conditions")
        return result
    if operation == "mean":
        result.update(value=round(sum(values) / len(values), 2), unit=target.unit)
    elif operation == "energy":
        result.update(value=round(sum(values), 2), unit=ENERGY_UNITS[target.unit])  # x 1 h
    else:
        pick = max if operation == "max" else min
        best = pick(range(len(values)), key=values.__getitem__)
        result.update(value=round(values[best], 2), unit=target.unit,
                      at=_local(selected[best]).isoformat())  # fmt: skip
    return result


def _local(hour: datetime) -> datetime:
    return hour.astimezone(BERLIN)
