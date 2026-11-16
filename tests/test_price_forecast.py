"""GridAgent's day-ahead price forecast: no look-ahead, recovery, baselines, the outlook."""

from datetime import date, timedelta

import pytest

import gridagent.monitor.price_forecast as pf
from gridagent.data.client import NoDataError
from gridagent.monitor.hours import best_blocks
from gridagent.monitor.price_forecast import History, backtest, forecast_day

DAY = timedelta(days=1)
FIRST, LAST = date(2026, 8, 1), date(2026, 11, 20)


def world(price_of=lambda residual_gw: 20 + 0.08 * residual_gw**2) -> History:
    """Every hour: wind index w (m/s) gives 1000*w MW, radiation r gives 50*r MW, load has a
    weekly pattern; price follows a convex merit order. The forecasts are perfect."""
    h = History(price={}, load={}, wind={}, solar={}, wind_fc={}, solar_fc={})
    d = FIRST
    while d <= LAST:
        for hour in range(24):
            k = (d, hour)
            w = 3 + (d.toordinal() * 7 + hour * 3) % 11
            r = max(0, 12 - abs(hour - 12)) * 30
            h.wind_fc[k], h.solar_fc[k] = float(w), float(r)
            h.wind[k], h.solar[k] = 1000.0 * w, 50.0 * r
            h.load[k] = 50_000 + 8_000 * (8 <= hour < 20) + 2_000 * (d.weekday() < 5)
            h.price[k] = price_of((h.load[k] - h.wind[k] - h.solar[k]) / 1000)
        d += DAY
    return h


def test_a_perfect_world_is_forecast_almost_exactly() -> None:
    h = world()
    target = date(2026, 11, 10)
    forecast = forecast_day(h, target)
    assert len(forecast) == 24
    # demand comes from a week earlier (same weekday), so the only error is the fit itself
    assert all(forecast[hr] == pytest.approx(h.price[(target, hr)], abs=1.0) for hr in forecast)


def test_no_look_ahead_the_target_day_and_the_day_before_do_not_matter() -> None:
    target = date(2026, 11, 10)
    clean = forecast_day(world(), target)
    poisoned = world()
    for d in (target - DAY, target):  # actual values not known on the morning before
        for hr in range(24):
            for series in (poisoned.price, poisoned.load, poisoned.wind, poisoned.solar):
                series[(d, hr)] = 1e6
    assert forecast_day(poisoned, target) == pytest.approx(clean)


def test_backtest_compares_with_the_two_simple_rules() -> None:
    result = backtest(world(), date(2026, 11, 1), date(2026, 11, 14))
    errors = result["mae_eur_mwh"]
    assert errors["forecast"] < errors["same hour, day before"]
    assert result["skill_vs_best_baseline_pct"] > 50 and result["hours"] == 14 * 24


def test_cheapest_and_dearest_blocks() -> None:
    hours = {h: 100.0 for h in range(24)} | {
        3: 10.0,
        4: 12.0,
        5: 11.0,
        18: 300.0,
        19: 310.0,
        20: 305.0,
    }
    assert best_blocks(hours) == (3, 11.0, 18, 305.0)


# ---------- the outlook on the dashboard ----------

TODAY = date(2026, 11, 10)


def outlook_with(monkeypatch, h: History) -> dict | None:
    monkeypatch.setattr(pf, "load_history", lambda data, start, end: h)
    return pf.outlook(data=None, today=TODAY)  # type: ignore[arg-type]


def test_outlook_after_the_auction_shows_forecast_next_to_the_result(monkeypatch) -> None:
    result = outlook_with(monkeypatch, world())
    assert result is not None and result["date"] == "2026-11-11"
    assert len(result["hourly_eur_mwh"]) == 24 and None not in result["hourly_eur_mwh"]
    assert result["cheapest_hours"].endswith(":00") and "–" in result["dearest_hours"]
    assert result["published_mean_eur_mwh"] == pytest.approx(result["mean_eur_mwh"], abs=1.0)
    assert result["check_today"]["mae_eur_mwh"] < 1.0  # a perfect world: tiny errors
    assert "days" not in result["recent_error"]  # the summary only, not every day


def test_outlook_before_the_auction_has_no_published_prices(monkeypatch) -> None:
    h = world()
    for hour in range(24):
        del h.price[(TODAY + DAY, hour)]  # tomorrow's auction has not run yet
    result = outlook_with(monkeypatch, h)
    assert result is not None and "published_mean_eur_mwh" not in result


def test_no_outlook_without_data_or_with_too_few_hours(monkeypatch) -> None:
    def missing(data, start, end):
        raise NoDataError("no prices")

    monkeypatch.setattr(pf, "load_history", missing)
    assert pf.outlook(data=None, today=TODAY) is None  # type: ignore[arg-type]

    h = world()
    for hour in range(6):  # a weather forecast with a gap: not a day's forecast
        del h.wind_fc[(TODAY + DAY, hour)]
    assert outlook_with(monkeypatch, h) is None
