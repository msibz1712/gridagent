"""The week ahead: calibration, reliability labels, operator forecasts first, caption."""

from datetime import UTC, date, datetime, timedelta

import pytest

from gridagent.data.client import NoDataError
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse
from gridagent.monitor.captions import week_caption
from gridagent.monitor.week import week_outlook

TODAY = date(2027, 1, 20)  # the 60-day window is all winter time (no 25-hour day)
DAY = timedelta(days=1)


def wind_on(day: date) -> float:
    return 4.0 + (day.toordinal() % 6)  # m/s, varies from day to day


def hourly(endpoint: str, start: date, days: int, values) -> TimeSeriesResponse:
    first = datetime(start.year, start.month, start.day, tzinfo=UTC) - timedelta(hours=1)
    rows = [DataRow(timestamp=first + timedelta(hours=h), values=values(start + h // 24 * DAY))
            for h in range(24 * days)]  # fmt: skip
    keys = list(values(start))
    return TimeSeriesResponse(endpoint=endpoint, timezone="Europe/Berlin", unit="MW",
                              interval_minutes=60, series=[SeriesInfo(id=k, name=k) for k in keys],
                              data=rows)  # fmt: skip


class World:
    """Wind energy (GWh/day) = 100 * wind speed exactly, so the calibration is perfect."""

    def weather_forecast(self, start, end, lead_days=None):
        days = (end - start).days + 1
        return hourly(
            "weather_forecast",
            start,
            days,
            lambda d: {
                "wind_100m": wind_on(d),
                "solar_radiation": 30.0 + d.toordinal() % 3 * 10,
                "temperature": 5.0,
            },
        )

    def public_power(self, start, end):
        days = (end - start).days + 1
        return hourly("public_power", start, days, lambda d: {
            "wind_onshore": 100 * wind_on(d) * 1000 / 24, "wind_offshore": 0.0,
            "solar": 1000.0 + (d.toordinal() % 3) * 100})  # fmt: skip


def test_calibrated_energy_and_reliability_labels() -> None:
    week = week_outlook(World(), TODAY)
    assert week["calibration"]["wind_r"] == pytest.approx(1.0)
    by_day = {d["days_ahead"]: d for d in week["days"]}
    assert by_day[2]["wind_gwh"] == round(100 * wind_on(TODAY + timedelta(days=2)))
    assert [d["reliability"] for d in week["days"]] == [
        "today", "reliable", "reliable", "reliable", "reliable", "rough indication",
        "rough indication"]  # fmt: skip


def test_the_operators_forecast_wins_where_it_exists() -> None:
    week = week_outlook(World(), TODAY, {"2027-01-20": (896.0, 20.0)})
    today = week["days"][0]
    assert (today["wind_gwh"], today["source"]) == (896, "grid operators' day-ahead forecast")
    assert week["days"][1]["source"] == "Open-Meteo, calibrated"


def test_caption_names_only_reliable_days() -> None:
    text = week_caption(week_outlook(World(), TODAY))
    assert "rough indication" in text and "about" in text and "because" not in text


def test_no_forecast_means_no_outlook() -> None:
    class Offline(World):
        def weather_forecast(self, start, end, lead_days=None):
            raise NoDataError("offline")

    assert week_outlook(Offline(), TODAY) is None
