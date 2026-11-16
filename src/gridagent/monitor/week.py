"""The week ahead: expected wind and solar energy and temperature for the next 7 days.

From Open-Meteo's latest forecast (data/openmeteo.py). Wind speed and radiation are turned
into energy with a curve fitted on the last 60 days: daily actual energy (Energy-Charts)
against the daily forecast issued one day before, energy = a + b*x + c*x^2. The fit's
correlation is shown, and every day carries the reliability measured with
`garden forecast-skill` (Sep-Nov 2026: wind r >= 0.92 up to 4 days ahead, 0.66 at 7).
"Normal" is the mean of the same forecast over the 4 weeks before today.
"""

import math
from datetime import date, timedelta
from typing import Any

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.monitor.anomalies import quadratic_fit
from gridagent.monitor.forecast_skill import correlation, daily_mean
from gridagent.monitor.forecasts import daily_gwh

DAYS = 7
CALIBRATION_DAYS = 60
NORMAL_DAYS = 28
RELIABLE_UP_TO = 4  # days ahead; from the measured lead-time skill


def reliability(days_ahead: int) -> str:
    if days_ahead == 0:
        return "today"
    return "reliable" if days_ahead <= RELIABLE_UP_TO else "rough indication"


def _fit(x: dict[date, float], y: dict[date, float]) -> tuple[tuple[float, float, float], float]:
    """Quadratic fit of y on x over their common days, and its correlation."""
    days = [d for d in x if d in y]
    if len(days) < 20:
        raise ValueError("Too few days to calibrate the forecast against actual output.")
    abc = quadratic_fit([x[d] for d in days], [y[d] for d in days])
    fitted = {d: abc[0] + abc[1] * x[d] + abc[2] * x[d] ** 2 for d in days}
    r, _ = correlation(fitted, {d: y[d] for d in days})
    return abc, r or 0.0


def week_outlook(
    data: DataProvider, today: date, operators: dict[str, tuple[float, float]] | None = None
) -> dict[str, Any] | None:
    """Seven days from `today`, or None when the forecast or the calibration is unavailable.

    `operators`: {day: (wind GWh, solar GWh)} from the grid operators' day-ahead forecast.
    Those days use it instead of Open-Meteo: it is measurably better (r 0.993 vs 0.973),
    and one page should never show two different forecasts for the same day."""
    past_start = today - timedelta(days=CALIBRATION_DAYS)
    yesterday = today - timedelta(days=1)
    try:
        ahead = data.weather_forecast(today, today + timedelta(days=DAYS - 1))
        issued_day_before = data.weather_forecast(past_start, yesterday, lead_days=1)
        generation = data.public_power(past_start, yesterday)
    except (NoDataError, EnergyChartsError):
        return None
    past = {k: daily_mean(issued_day_before.get_series(k))
            for k in ("wind_100m", "solar_radiation", "temperature")}  # fmt: skip
    future = {k: daily_mean(ahead.get_series(k))
              for k in ("wind_100m", "solar_radiation", "temperature")}  # fmt: skip
    try:
        wind_fit, wind_r = _fit(past["wind_100m"], daily_gwh(source_series(generation, "wind")))
        solar_fit, solar_r = _fit(past["solar_radiation"],
                                  daily_gwh(source_series(generation, "solar")))  # fmt: skip
    except ValueError:
        return None

    def energy(abc: tuple[float, float, float], x: float) -> float:
        return max(0.0, abc[0] + abc[1] * x + abc[2] * x * x)

    recent = [d for d in past["wind_100m"] if (today - d).days <= NORMAL_DAYS]
    normal_wind = sum(energy(wind_fit, past["wind_100m"][d]) for d in recent) / len(recent)
    normal_temp = [past["temperature"][d] for d in recent if d in past["temperature"]]
    days = []
    for offset in range(DAYS):
        d = today + timedelta(days=offset)
        if d not in future["wind_100m"]:
            continue
        day = {
            "date": d.isoformat(),
            "days_ahead": offset,
            "reliability": reliability(offset),
            "wind_gwh": round(energy(wind_fit, future["wind_100m"][d])),
            "solar_gwh": round(energy(solar_fit, future["solar_radiation"].get(d, 0.0))),
            "temperature_c": round(future["temperature"].get(d, math.nan), 1),
            "source": "Open-Meteo, calibrated",
        }  # fmt: skip
        if operators and d.isoformat() in operators:
            wind_gwh, solar_gwh = operators[d.isoformat()]
            day |= {"wind_gwh": round(wind_gwh), "solar_gwh": round(solar_gwh),
                    "source": "grid operators' day-ahead forecast"}  # fmt: skip
        days.append(day)
    if not days:
        return None
    return {
        "issued": today.isoformat(),
        "days": days,
        "normal_wind_gwh": round(normal_wind),
        "normal_temperature_c": round(sum(normal_temp) / len(normal_temp), 1) if normal_temp
        else None,
        "calibration": {"days": CALIBRATION_DAYS, "wind_r": round(wind_r, 3),
                        "solar_r": round(solar_r, 3)},
        "source": "Open-Meteo.com (CC BY 4.0); calibrated on Energy-Charts generation",
    }  # fmt: skip
