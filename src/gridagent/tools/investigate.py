"""Why was the price so high (or low)? Evidence for one price event.

An event is a set of clock hours on one day (e.g. 29 Oct 2026, 17:00-18:00). It is
compared with the same clock hours on *comparable days* (weekdays with weekdays,
weekends with weekends) in the 4 weeks before it: what an analyst has the morning
after. Everything is compared as hourly means (prices are 15-minute values since
Oct 2025, hourly before).

Two kinds of evidence:

1. Merit order. Power plants are dispatched from cheapest to most expensive, so the
   price mostly follows *residual load* (demand minus wind and solar: what the other
   plants must cover). A straight line price = a + b * residual load is fitted on
   all hours of the 4 weeks (excluding the event day) and asked: what price would the
   event's residual load normally give? The rest is "unexplained" by residual load:
   fuel and CO2 prices, neighbours, scarcity, plant outages (not in our data).
2. Drivers. For demand, wind, solar, gas, coal, imports and weather: the event value
   against comparable days, as a difference and as a z-score (how many standard
   deviations from the comparable days' mean). Sorted by how unusual they were.

These are associations on 4 weeks of data, not proof of cause.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gridagent.data.models import Series

BERLIN = ZoneInfo("Europe/Berlin")
WINDOW_DAYS = 28  # the comparison period before the event day
MIN_COMPARABLE_DAYS = 5

# (key, name, unit, effect on the price when higher: +1 raises, -1 lowers; 0 = a system
# response, which reacts to the price as much as it sets it)
DRIVERS = [
    ("residual_load", "Residual load (demand minus wind and solar)", "MW", +1),
    ("load", "Demand (load)", "MW", +1),
    ("wind", "Wind generation (onshore + offshore)", "MW", -1),
    ("solar", "Solar generation", "MW", -1),
    ("fossil_gas", "Gas generation", "MW", 0),
    ("lignite", "Lignite generation", "MW", 0),
    ("hard_coal", "Hard coal generation", "MW", 0),
    ("net_imports", "Net commercial imports (+ import, - export)", "MW", 0),
    ("pumped_storage", "Pumped-storage generation", "MW", 0),
    ("wind_speed", "Wind speed, DWD station mean (~10 m)", "m/s", -1),
    ("solar_radiation", "Solar radiation, DWD station mean", "W/m²", -1),
]
CAVEATS = [
    "Associations over 4 weeks, not proof of cause.",
    "System responses (gas, coal, storage, imports) react to the price as much as they set "
    "it: they show how the gap was covered, not why the price moved.",
    "Fuel (gas) and CO2 prices, plant outages and bids are not in the data; they are "
    "candidates for the part residual load does not explain.",
    "Public holidays are treated as normal weekdays.",
]
STEEP_TOP = (
    "The event's residual load was among the highest 5 % of the 4 weeks. The real merit "
    "order gets much steeper there (the last, most expensive plants set the price), so a "
    "straight line understates the expected price: much of the unexplained rest is likely "
    "this scarcity, not other factors."
)


def hourly(series: Series) -> dict[datetime, float]:
    """UTC hour start -> mean of the values in that hour (at least half present)."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    counts: dict[datetime, int] = defaultdict(int)
    for point in series.points:
        hour = point.timestamp.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        counts[hour] += 1
        if point.value is not None:
            buckets[hour].append(point.value)
    return {h: sum(v) / len(v) for h, v in buckets.items() if len(v) * 2 >= counts[h]}


def combine(parts: list[dict[datetime, float]]) -> dict[datetime, float]:
    """Sum of several hourly series (e.g. onshore + offshore), hours present in all."""
    common = set.intersection(*(set(p) for p in parts)) if parts else set()
    return {h: sum(p[h] for p in parts) for h in common}


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


@dataclass
class Selection:
    event: list[datetime]  # UTC hours of the event
    comparable: dict[date, list[datetime]]  # comparable day -> its UTC hours (same clock hours)
    fit: list[datetime]  # every hour of the window except the event day


def select_hours(hours: set[datetime], day: date, from_hour: int, to_hour: int) -> Selection:
    by_day: dict[date, list[datetime]] = defaultdict(list)
    fit = []
    for h in sorted(hours):
        local = h.astimezone(BERLIN)
        if local.date() != day:
            fit.append(h)
        if from_hour <= local.hour < to_hour:
            by_day[local.date()].append(h)
    event = by_day.pop(day, [])
    comparable = {d: hs for d, hs in by_day.items() if is_weekend(d) == is_weekend(day) and d < day}
    return Selection(event=event, comparable=comparable, fit=fit)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    m = _mean(values)
    return (
        math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1)) if len(values) > 1 else 0
    )


def compare(data: dict[datetime, float], sel: Selection) -> dict[str, Any] | None:
    """Event mean vs comparable days (each day = mean of its event hours)."""
    if any(h not in data for h in sel.event):
        return None
    days = [_mean([data[h] for h in hs]) for hs in sel.comparable.values()
            if hs and all(h in data for h in hs)]  # fmt: skip
    if len(days) < MIN_COMPARABLE_DAYS:
        return None
    event, normal, spread = _mean([data[h] for h in sel.event]), _mean(days), _std(days)
    return {
        "event": round(event, 2),
        "comparable_days_mean": round(normal, 2),
        "difference": round(event - normal, 2),
        "difference_pct": round(100 * (event - normal) / abs(normal), 1) if normal else None,
        "z_score": round((event - normal) / spread, 2) if spread else None,
        "comparable_days_below": sum(d < event for d in days),
        "comparable_days": len(days),
    }


def merit_order(price: dict[datetime, float], residual: dict[datetime, float],
                sel: Selection) -> dict[str, Any] | None:  # fmt: skip
    """Least-squares line price = a + b * residual load (GW), fitted without the event day."""
    pairs = [(residual[h] / 1000, price[h]) for h in sel.fit if h in price and h in residual]
    if len(pairs) < 48 or any(h not in price or h not in residual for h in sel.event):
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    intercept = my - slope * mx
    ss_res = sum((y - intercept - slope * x) ** 2 for x, y in pairs)
    ss_tot = sum((y - my) ** 2 for y in ys)
    event_x = _mean([residual[h] / 1000 for h in sel.event])
    event_y = _mean([price[h] for h in sel.event])
    expected = intercept + slope * event_x
    return {
        "eur_per_mwh_per_gw_residual_load": round(slope, 2),
        "r_squared": round(1 - ss_res / ss_tot, 3) if ss_tot else None,
        "hours_fitted": len(pairs),
        "event_residual_load_gw": round(event_x, 2),
        "expected_price_eur_per_mwh": round(expected, 2),
        "actual_price_eur_per_mwh": round(event_y, 2),
        "unexplained_eur_per_mwh": round(event_y - expected, 2),
        "highest_residual_load_in_fit_gw": round(max(xs), 2),
        "hours_with_lower_residual_load_pct": round(
            100 * sum(x < event_x for x in xs) / len(xs), 1
        ),
    }


def investigate(
    day: date,
    from_hour: int,
    to_hour: int,
    window_start: date,
    series: dict[str, dict[datetime, float]],
) -> dict[str, Any]:
    """The evidence for one event; `series` must contain 'price', drivers may be missing."""
    price = series["price"]
    sel = select_hours(set(price), day, from_hour, to_hour)
    if not sel.event:
        raise ValueError(f"No prices for {day} {from_hour:02d}:00-{to_hour:02d}:00.")
    price_cmp = compare(price, sel)
    if price_cmp is None:
        raise ValueError("Too few comparable days with data in the 4 weeks before the event.")
    direction = 1 if price_cmp["difference"] > 0 else -1
    conditions, responses, unavailable = [], [], []
    for key, name, unit, effect in DRIVERS:
        result = compare(series[key], sel) if key in series else None
        if result is None:
            unavailable.append(name)
            continue
        entry = {"driver": name, "unit": unit, **result}
        if effect:
            # Does this condition point the same way as the price did (less wind, higher price)?
            entry["consistent_with_price_move"] = result["difference"] * effect * direction > 0
            conditions.append(entry)
        else:
            responses.append(entry)
    for group in (conditions, responses):
        group.sort(key=lambda d: -abs(d["z_score"] or 0))
    order = merit_order(price, series.get("residual_load", {}), sel)
    caveats = list(CAVEATS)
    if order and order["hours_with_lower_residual_load_pct"] >= 95:
        caveats.insert(0, STEEP_TOP)
    kind = "weekend" if is_weekend(day) else "weekday"
    return {
        "event": {"date": day.isoformat(), "hours": f"{from_hour:02d}:00-{to_hour:02d}:00",
                  "day_type": kind, "hours_in_event": len(sel.event)},
        "comparison": {
            "period": f"{window_start} to {day - timedelta(days=1)}",
            "comparable_days": f"{len(sel.comparable)} {kind}s, same clock hours",
        },
        "price": {"unit": "EUR / MWh", **price_cmp},
        "merit_order": order,
        "conditions_by_unusualness": conditions,
        "system_responses": responses,
        "not_available": unavailable,
        "caveats": caveats,
    }  # fmt: skip
