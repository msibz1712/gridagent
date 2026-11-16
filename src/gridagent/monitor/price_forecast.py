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

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cached_property
from typing import Any

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.monitor.anomalies import quadratic_fit
from gridagent.monitor.hours import best_blocks, by_local_hour, clock_list, hours_text

log = logging.getLogger(__name__)

CALIBRATION_DAYS = 60
MERIT_ORDER_DAYS = 28


@dataclass
class History:
    """Hourly series for the whole backtest span, fetched once."""

    price: dict[tuple[date, int], float]
    load: dict[tuple[date, int], float]
    wind: dict[tuple[date, int], float]
    solar: dict[tuple[date, int], float]
    wind_fc: dict[tuple[date, int], float]  # hub-height wind index, issued before the auction
    solar_fc: dict[tuple[date, int], float]  # radiation index, issued before the auction

    @cached_property
    def residual(self) -> dict[tuple[date, int], float]:
        """Load minus wind minus solar, computed on first use and kept (not per forecast)."""
        return {k: self.load[k] - self.wind[k] - self.solar[k]
                for k in self.load if k in self.wind and k in self.solar}  # fmt: skip


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

    wind_curve = quadratic_fit(*pairs(h.wind_fc, h.wind, calibration))
    solar_curve = quadratic_fit(*pairs(h.solar_fc, h.solar, calibration))
    rx, ry = pairs(h.residual, h.price, merit_days)
    merit = quadratic_fit([x / 1000 for x in rx], ry)

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


def backtest(h: History, first: date, last: date,
             forecasts: dict[date, dict[int, float]] | None = None) -> dict[str, Any]:  # fmt: skip
    """Hourly mean absolute error of the forecast and of the two baselines, day by day.
    `forecasts`, if given, receives each day's forecast (so callers need not recompute)."""
    model, yesterday, last_week, rows = [], [], [], []
    for target in _days(first, last):
        forecast = forecast_day(h, target)
        if forecasts is not None:
            forecasts[target] = forecast
        for hour, predicted in forecast.items():
            actual = h.price.get((target, hour))
            same_yesterday = h.price.get((target - timedelta(days=1), hour))
            same_last_week = h.price.get((target - timedelta(days=7), hour))
            if None in (actual, same_yesterday, same_last_week):
                continue
            model.append((predicted, actual))
            yesterday.append((same_yesterday, actual))
            last_week.append((same_last_week, actual))
        both = [hr for hr in forecast if (target, hr) in h.price]  # the same hours for both means
        if both:
            rows.append({"date": target.isoformat(),
                         "forecast_mean": round(sum(forecast[hr] for hr in both) / len(both), 2),
                         "actual_mean": round(sum(h.price[(target, hr)] for hr in both)
                                              / len(both), 2)})  # fmt: skip
    errors = {"forecast": _mae(model), "same hour, day before": _mae(yesterday),
              "same hour, week before": _mae(last_week)}  # fmt: skip
    baselines = [v for k, v in errors.items() if k != "forecast" and v]
    skill = None
    if errors["forecast"] is not None and baselines:
        skill = round(100 * (1 - errors["forecast"] / min(baselines)), 1)
    return {
        "period": f"{first} to {last}",
        "hours": len(model),
        "mae_eur_mwh": {k: round(v, 2) for k, v in errors.items() if v is not None},
        "skill_vs_best_baseline_pct": skill,
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


def outlook(data: DataProvider, today: date) -> dict[str, Any] | None:
    """For the briefing: the forecast for tomorrow (before its auction), how the forecast for
    today compares with today's published prices, and the error of the last 4 weeks."""
    target = today + timedelta(days=1)
    forecasts: dict[date, dict[int, float]] = {}
    try:
        h = load_history(data, today - timedelta(days=MERIT_ORDER_DAYS), target)
        hours = forecast_day(h, target)
        recent = backtest(h, today - timedelta(days=MERIT_ORDER_DAYS - 1), today, forecasts)
        cheap, cheap_price, dear, dear_price = best_blocks(hours)
    except (NoDataError, EnergyChartsError, ValueError, KeyError) as exc:
        # Missing data or too little of it to fit: the briefing goes on without a forecast,
        # and says so (daily.py lists it under not_available). Other errors are bugs: raise.
        log.warning("No price forecast for %s: %s", target, exc)
        return None
    today_forecast = forecasts.get(today, {})
    if len(hours) < 20:
        log.warning("No price forecast for %s: only %d hours forecast", target, len(hours))
        return None
    result: dict[str, Any] = {
        "date": target.isoformat(),
        "hourly_eur_mwh": clock_list(hours),  # by clock hour; None where an hour is missing
        "mean_eur_mwh": round(sum(hours.values()) / len(hours), 2),
        "cheapest_hours": hours_text(cheap, cheap + 3), "cheapest_eur_mwh": round(cheap_price, 2),
        "dearest_hours": hours_text(dear, dear + 3), "dearest_eur_mwh": round(dear_price, 2),
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
