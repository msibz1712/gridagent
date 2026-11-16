"""A day-ahead price forecast for tomorrow, and an honest backtest against simple baselines.

The forecast for day T is made on the morning of T-1, before the auction at noon, with only
what is known then:
- wind and solar for T: Open-Meteo's forecast issued two days before each hour (previous-
  runs archive, lead 2: strictly before the auction on T-1 at noon),
  turned into MW with curves fitted on the 60 days up to T-2 (forecast vs actual, hourly);
- demand for T: the same hour on the same weekday a week earlier (T-7), a plain baseline;
- price: a merit-order curve, price = a + b*x + c*x^2 with x = residual load in GW, fitted
  on the 28 days up to T-2 (actual residual load vs actual price, hourly).

It is judged against two rules anyone could use without a model: the price of the same
hour on T-1 (published the day before) and on T-7. A forecast that does not beat them is
not worth showing, and the backtest says so.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import Series
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.monitor.anomalies import quadratic_fit
from gridagent.tools.investigate import BERLIN, hourly

CALIBRATION_DAYS = 60
MERIT_ORDER_DAYS = 28


def by_local_hour(series: Series) -> dict[tuple[date, int], float]:
    """(local date, local hour) -> hourly mean (the repeated DST hour: both hours' mean)."""
    out: dict[tuple[date, int], list[float]] = defaultdict(list)
    for hour, value in hourly(series).items():
        local = hour.astimezone(BERLIN)
        out[(local.date(), local.hour)].append(value)
    return {k: sum(v) / len(v) for k, v in out.items()}


@dataclass
class History:
    """Hourly series for the whole backtest span, fetched once."""

    price: dict[tuple[date, int], float]
    load: dict[tuple[date, int], float]
    wind: dict[tuple[date, int], float]
    solar: dict[tuple[date, int], float]
    wind_fc: dict[tuple[date, int], float]  # hub-height wind index, issued the day before
    solar_fc: dict[tuple[date, int], float]  # radiation index, issued the day before


def load_history(data: DataProvider, start: date, end: date, lead_days: int = 2) -> History:
    """Everything a backtest from `start` to `end` needs (plus the fitting windows).

    lead_days 2 (default): the weather forecast issued two days before each hour. "1 day"
    would mean 24 h before each hour, so for tomorrow's evening hours a forecast issued
    after the noon auction: a small look into the future. Two days is strictly before it."""
    first = start - timedelta(days=CALIBRATION_DAYS + 2)
    try:
        price = data.day_ahead_price(first, end).get_series("day_ahead_price")
    except (NoDataError, EnergyChartsError):  # `end`'s auction may not have run yet
        price = data.day_ahead_price(first, end - timedelta(days=1)).get_series("day_ahead_price")
    generation = data.public_power(first, end)
    weather = data.weather_forecast(first, end, lead_days=lead_days)
    return History(
        price=by_local_hour(price),
        load=by_local_hour(source_series(generation, "load")),
        wind=by_local_hour(source_series(generation, "wind")),
        solar=by_local_hour(source_series(generation, "solar")),
        wind_fc=by_local_hour(weather.get_series("wind_100m")),
        solar_fc=by_local_hour(weather.get_series("solar_radiation")),
    )


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    return quadratic_fit(xs, ys)


def _curve(abc: tuple[float, float, float], x: float) -> float:
    return abc[0] + abc[1] * x + abc[2] * x * x


def forecast_day(h: History, target: date) -> dict[int, float]:
    """Hourly price forecast for `target`, using only data known on the morning before."""
    known_until = target - timedelta(days=2)  # complete actual data
    calibration = _days(known_until - timedelta(days=CALIBRATION_DAYS - 1), known_until)
    merit_days = _days(known_until - timedelta(days=MERIT_ORDER_DAYS - 1), known_until)

    def pairs(x: dict, y: dict, days: list[date]) -> tuple[list[float], list[float]]:
        keys = [(d, hr) for d in days for hr in range(24) if (d, hr) in x and (d, hr) in y]
        return [x[k] for k in keys], [y[k] for k in keys]

    wind_curve = _fit(*pairs(h.wind_fc, h.wind, calibration))
    solar_curve = _fit(*pairs(h.solar_fc, h.solar, calibration))
    residual = {k: h.load[k] - h.wind[k] - h.solar[k]
                for k in h.load if k in h.wind and k in h.solar}  # fmt: skip
    rx, ry = pairs(residual, h.price, merit_days)
    merit = _fit([x / 1000 for x in rx], ry)

    out = {}
    week_before = target - timedelta(days=7)
    for hour in range(24):
        key = (target, hour)
        if key not in h.wind_fc or key not in h.solar_fc or (week_before, hour) not in h.load:
            continue
        wind = max(0.0, _curve(wind_curve, h.wind_fc[key]))
        solar = max(0.0, _curve(solar_curve, h.solar_fc[key]))
        expected_residual = h.load[(week_before, hour)] - wind - solar
        out[hour] = _curve(merit, expected_residual / 1000)
    return out


def _mae(pairs: list[tuple[float, float]]) -> float | None:
    return sum(abs(a - b) for a, b in pairs) / len(pairs) if pairs else None


def backtest(h: History, first: date, last: date) -> dict[str, Any]:
    """Hourly mean absolute error of the forecast and of the two baselines, day by day."""
    model, yesterday, last_week, rows = [], [], [], []
    for target in _days(first, last):
        forecast = forecast_day(h, target)
        for hour, predicted in forecast.items():
            actual = h.price.get((target, hour))
            same_yesterday = h.price.get((target - timedelta(days=1), hour))
            same_last_week = h.price.get((target - timedelta(days=7), hour))
            if None in (actual, same_yesterday, same_last_week):
                continue
            model.append((predicted, actual))
            yesterday.append((same_yesterday, actual))
            last_week.append((same_last_week, actual))
        actual_day = [h.price[(target, hr)] for hr in forecast if (target, hr) in h.price]
        if actual_day and forecast:
            rows.append({"date": target.isoformat(),
                         "forecast_mean": round(sum(forecast.values()) / len(forecast), 2),
                         "actual_mean": round(sum(actual_day) / len(actual_day), 2)})  # fmt: skip
    errors = {"forecast": _mae(model), "same hour, day before": _mae(yesterday),
              "same hour, week before": _mae(last_week)}  # fmt: skip
    best_baseline = min(v for k, v in errors.items() if k != "forecast" and v is not None)
    return {
        "period": f"{first} to {last}",
        "hours": len(model),
        "mae_eur_mwh": {k: round(v, 2) for k, v in errors.items() if v is not None},
        "skill_vs_best_baseline_pct": round(100 * (1 - errors["forecast"] / best_baseline), 1)
        if errors["forecast"] is not None else None,
        "daily_mean_correlation": _correlation([r["forecast_mean"] for r in rows],
                                               [r["actual_mean"] for r in rows]),
        "days": rows,
    }  # fmt: skip


def _correlation(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if not sxx or not syy:
        return None
    return round(
        sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / math.sqrt(sxx * syy), 3
    )


def _blocks(hours: dict[int, float], size: int = 3) -> tuple[str, float, str, float]:
    """Cheapest and dearest `size` hours in a row."""
    starts = [h for h in sorted(hours) if all(h + i in hours for i in range(size))]
    mean = {h: sum(hours[h + i] for i in range(size)) / size for h in starts}
    cheap, dear = min(mean, key=mean.get), max(mean, key=mean.get)
    return (f"{cheap:02d}:00–{cheap + size:02d}:00", round(mean[cheap], 2),
            f"{dear:02d}:00–{dear + size:02d}:00", round(mean[dear], 2))  # fmt: skip


def outlook(data: DataProvider, today: date) -> dict[str, Any] | None:
    """For the briefing: the forecast for tomorrow (before its auction), how the forecast for
    today compares with today's published prices, and the error of the last 4 weeks."""
    target = today + timedelta(days=1)
    try:
        h = load_history(data, today - timedelta(days=MERIT_ORDER_DAYS), target)
        hours = forecast_day(h, target)
        recent = backtest(h, today - timedelta(days=MERIT_ORDER_DAYS - 1), today)
        today_forecast = forecast_day(h, today)
    except Exception:  # noqa: BLE001 - a missing forecast must never break the briefing
        return None
    if len(hours) < 20:
        return None
    cheap, cheap_price, dear, dear_price = _blocks(hours)
    result: dict[str, Any] = {
        "date": target.isoformat(),
        "hourly_eur_mwh": [round(hours[hr], 2) for hr in sorted(hours)],
        "mean_eur_mwh": round(sum(hours.values()) / len(hours), 2),
        "cheapest_hours": cheap, "cheapest_eur_mwh": cheap_price,
        "dearest_hours": dear, "dearest_eur_mwh": dear_price,
        "recent_error": {k: v for k, v in recent.items() if k != "days"},
    }  # fmt: skip
    published = [h.price[(target, hr)] for hr in hours if (target, hr) in h.price]
    if len(published) == len(hours):  # after the auction: the forecast next to the result
        result["published_mean_eur_mwh"] = round(sum(published) / len(published), 2)
        result["mae_vs_published_eur_mwh"] = round(
            sum(abs(hours[hr] - h.price[(target, hr)]) for hr in hours) / len(hours), 2
        )
    actual = [h.price[(today, hr)] for hr in today_forecast if (today, hr) in h.price]
    if actual and len(actual) == len(today_forecast):
        errors = [abs(today_forecast[hr] - h.price[(today, hr)]) for hr in today_forecast]
        result["check_today"] = {
            "date": today.isoformat(),
            "forecast_mean_eur_mwh": round(sum(today_forecast.values()) / len(today_forecast), 2),
            "actual_mean_eur_mwh": round(sum(actual) / len(actual), 2),
            "mae_eur_mwh": round(sum(errors) / len(errors), 2),
        }
    return result
