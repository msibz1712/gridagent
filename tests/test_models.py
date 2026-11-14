"""Parsing real Energy-Charts responses into our models."""

from datetime import date

import pytest

from gridagent.data.models import TimeSeriesResponse


def test_public_power_envelope(week_power: TimeSeriesResponse) -> None:
    assert week_power.endpoint == "public_power"
    assert week_power.unit == "MW"
    assert week_power.interval_minutes == 15
    assert week_power.timezone == "Europe/Berlin"
    assert "CC BY 4.0" in (week_power.license or "")
    assert len(week_power.data) == 7 * 96


def test_price_envelope(yesterday_price: TimeSeriesResponse) -> None:
    assert yesterday_price.bidding_zone == "DE-LU"
    assert yesterday_price.unit == "EUR / MWh"
    assert yesterday_price.series_ids() == ["day_ahead_price"]


def test_get_series_uses_series_unit_override(week_power: TimeSeriesResponse) -> None:
    assert week_power.get_series("solar").unit == "MW"
    assert week_power.get_series("renewable_share_of_load").unit == "%"


def test_series_dates_are_local(week_power: TimeSeriesResponse) -> None:
    solar = week_power.get_series("solar")
    assert solar.start == date(2026, 11, 2)
    assert solar.end == date(2026, 11, 8)
    assert solar.points[0].timestamp.utcoffset() is not None  # timezone-aware


def test_unknown_series_raises_helpful_error(week_power: TimeSeriesResponse) -> None:
    with pytest.raises(KeyError, match="Available"):
        week_power.get_series("nuclear")
