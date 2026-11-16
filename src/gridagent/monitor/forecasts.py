"""Forecasts: what the day-ahead forecasts got wrong yesterday, and what they expect next.

Forecast misses. For wind, solar and demand, each day's actual energy is compared with
the energy its day-ahead forecast promised (Energy-Charts, CC BY 4.0). A miss is news
only when it is large against the misses of the 4 weeks before (robust z, as in
anomalies.py) AND large in absolute terms: forecasts always miss a little, and traders
care about the unusual misses, which is when balancing gets expensive.

Outlook. For days whose forecast is published, the expected wind, solar and demand
energy and peak residual load, against the actual daily values of the 4 weeks before.
"""

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from gridagent.data.models import Series, TimeSeriesResponse
from gridagent.data.sources import source_series
from gridagent.monitor.anomalies import Story, _clip, median, robust_scale
from gridagent.tools.investigate import BERLIN

WINDOW_DAYS = 28
MISS_Z = 3.0
# (series, label, smallest miss worth a story in GWh)
MISS_FIGURES = [("wind", "Wind", 40.0), ("solar", "Solar", 20.0), ("load", "Demand", 40.0)]


def daily_gwh(series: Series) -> dict[date, float]:
    """Local day -> energy in GWh, complete days only (at least 22 hours of values)."""
    sums: dict[date, list[float]] = {}
    for p in series.points:
        if p.value is not None:
            sums.setdefault(p.timestamp.astimezone(BERLIN).date(), []).append(p.value)
    hours = (series.interval_minutes or 15) / 60
    return {d: sum(v) * hours / 1000 for d, v in sums.items() if len(v) * hours >= 22}


def _actual(generation: TimeSeriesResponse, key: str) -> Series:
    return source_series(generation, key)


def miss_stories(day: date, generation: TimeSeriesResponse, forecast: TimeSeriesResponse,
                 ) -> list[Story]:  # fmt: skip
    """Unusually large gaps between yesterday's energy and its day-ahead forecast."""
    stories = []
    for key, label, floor in MISS_FIGURES:
        actual = daily_gwh(_actual(generation, key))
        predicted = daily_gwh(forecast.get_series(key))
        if day not in actual or day not in predicted:
            continue
        past = [d for d in actual if d < day and d in predicted
                and (day - d).days <= WINDOW_DAYS]  # fmt: skip
        if len(past) < 14:
            continue
        misses = [actual[d] - predicted[d] for d in past]
        miss = actual[day] - predicted[day]
        scale = robust_scale(misses)
        z = (miss - median(misses)) / scale if scale else 0.0
        if abs(z) < MISS_Z or abs(miss) < floor:
            continue
        pct = 100 * miss / predicted[day] if predicted[day] else None
        word = "above" if miss > 0 else "below"
        hour, gap = _largest_hourly_gap(day, _actual(generation, key), forecast.get_series(key))
        parts = {"surprise": _clip((abs(z) - MISS_Z) / 4), "size": _clip(abs(miss) / 300)}
        facts: dict[str, Any] = {
            "date": day.isoformat(), "figure": label.lower(),
            "actual_gwh": round(actual[day], 2), "forecast_gwh": round(predicted[day], 2),
            "miss_gwh": round(miss, 2), "miss_pct": None if pct is None else round(pct, 1),
            "robust_z": round(z, 2), "days_compared": len(past),
            "largest_hourly_gap_mw": round(gap, 1), "largest_gap_hour": f"{hour:02d}:00",
        }  # fmt: skip
        pct_text = f"{abs(pct):.1f} % " if pct is not None else ""
        stories.append(Story(
            kind="forecast_miss", score_parts=parts,
            score=round(0.3 + 0.3 * parts["surprise"] + 0.2 * parts["size"], 3), facts=facts,
            title=(f"{label} came in {pct_text}{word} its day-ahead forecast: "
                   f"{actual[day]:.2f} vs {predicted[day]:.2f} GWh"),
        ))  # fmt: skip
    return stories


def _largest_hourly_gap(day: date, actual: Series, forecast: Series) -> tuple[int, float]:
    """(local hour, MW) of the largest hourly gap between actual and forecast."""

    def by_hour(series: Series) -> dict[int, list[float]]:
        out: dict[int, list[float]] = {}
        for p in series.points:
            t = p.timestamp.astimezone(BERLIN)
            if t.date() == day and p.value is not None:
                out.setdefault(t.hour, []).append(p.value)
        return out

    a, f = by_hour(actual), by_hour(forecast)
    gaps = {h: sum(a[h]) / len(a[h]) - sum(f[h]) / len(f[h]) for h in a if h in f}
    if not gaps:
        return 0, 0.0
    hour = max(gaps, key=lambda h: abs(gaps[h]))
    return hour, gaps[hour]


class Outlook(BaseModel):
    """What the day-ahead forecast expects for one day, against the 4 weeks before."""

    day: str
    wind_gwh: float
    solar_gwh: float
    load_gwh: float
    peak_residual_load_gw: float
    normal_wind_gwh: float | None
    normal_solar_gwh: float | None
    normal_load_gwh: float | None
    headline: str  # one plain sentence from the numbers


def outlook(day: date, forecast: TimeSeriesResponse, generation: TimeSeriesResponse | None,
            ) -> Outlook | None:  # fmt: skip
    """None when the day's forecast is not (completely) published yet."""
    expected = {k: daily_gwh(forecast.get_series(k)).get(day) for k in ("wind", "solar", "load")}
    if any(v is None for v in expected.values()):
        return None
    residual = [
        p.value
        for p in forecast.get_series("residual_load").points
        if p.value is not None and p.timestamp.astimezone(BERLIN).date() == day
    ]
    normals: dict[str, float | None] = {}
    for key in ("wind", "solar", "load"):
        if generation is None:
            normals[key] = None
            continue
        past = daily_gwh(_actual(generation, key))
        values = [v for d, v in past.items() if day - timedelta(days=WINDOW_DAYS + 1) <= d < day]
        normals[key] = sum(values) / len(values) if values else None
    wind, normal_wind = expected["wind"], normals["wind"]
    if normal_wind:
        change = 100 * (wind - normal_wind) / normal_wind
        word = "more" if change >= 0 else "less"
        headline = (f"{wind:.0f} GWh of wind expected, {abs(change):.0f} % {word} than "
                    "a normal day of the last four weeks")  # fmt: skip
    else:
        headline = f"{wind:.0f} GWh of wind expected"
    return Outlook(
        day=day.isoformat(), wind_gwh=round(wind, 2), solar_gwh=round(expected["solar"], 2),
        load_gwh=round(expected["load"], 2),
        peak_residual_load_gw=round(max(residual) / 1000, 2) if residual else 0.0,
        normal_wind_gwh=_r(normals["wind"]), normal_solar_gwh=_r(normals["solar"]),
        normal_load_gwh=_r(normals["load"]), headline=headline,
    )  # fmt: skip


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
