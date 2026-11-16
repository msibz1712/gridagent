"""How far ahead is the weather forecast useful? Measured, not assumed.

For each lead time (1..7 days), the Open-Meteo forecast as it was issued that many days
before is compared with what the power system then did: the daily mean hub-height wind
index against actual daily wind energy, and the daily mean solar radiation index against
actual daily solar energy (Energy-Charts). The grid operators' own day-ahead forecasts
are the reference row: what the best available forecast for tomorrow achieves.
"""

import math
from collections import defaultdict
from datetime import date
from typing import Any

from gridagent.data.models import Series
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.monitor.forecasts import daily_gwh
from gridagent.tools.investigate import BERLIN


def correlation(a: dict, b: dict) -> tuple[float | None, int]:
    keys = [k for k in a if k in b]
    if len(keys) < 10:
        return None, len(keys)
    x, y = [a[k] for k in keys], [b[k] for k in keys]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y, strict=True))
    sxx, syy = sum((p - mx) ** 2 for p in x), sum((q - my) ** 2 for q in y)
    return (round(sxy / math.sqrt(sxx * syy), 3) if sxx and syy else None), len(keys)


def daily_mean(series: Series) -> dict[date, float]:
    """Local day -> mean of the hourly values (complete days: at least 22 hours)."""
    days: dict[date, list[float]] = defaultdict(list)
    for p in series.points:
        if p.value is not None:
            days[p.timestamp.astimezone(BERLIN).date()].append(p.value)
    return {d: sum(v) / len(v) for d, v in days.items() if len(v) >= 22}


def lead_skill(data: DataProvider, start: date, end: date, leads: range = range(1, 8),
               ) -> list[dict[str, Any]]:  # fmt: skip
    """One row per lead time: correlation of the daily forecast with actual daily energy."""
    generation = data.public_power(start, end)
    wind, solar = (daily_gwh(source_series(generation, k)) for k in ("wind", "solar"))
    rows = []
    try:  # the reference: the grid operators' day-ahead forecasts (Energy-Charts)
        operators = data.forecast(start, end)
        rows.append({
            "forecast": "grid operators (Energy-Charts), day-ahead", "lead_days": 1,
            "wind_r": correlation(daily_gwh(operators.get_series("wind")), wind)[0],
            "solar_r": correlation(daily_gwh(operators.get_series("solar")), solar)[0],
        })  # fmt: skip
    except Exception:  # noqa: BLE001 - a missing reference row must not stop the table
        pass
    for lead in leads:
        weather = data.weather_forecast(start, end, lead_days=lead)
        wind_r, days = correlation(daily_mean(weather.get_series("wind_100m")), wind)
        solar_r, _ = correlation(daily_mean(weather.get_series("solar_radiation")), solar)
        rows.append({"forecast": "Open-Meteo, 18 points", "lead_days": lead,
                     "wind_r": wind_r, "solar_r": solar_r, "days": days})  # fmt: skip
    return rows
