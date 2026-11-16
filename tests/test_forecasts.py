"""Forecasts: merging, monthly fetching, forecast misses and the outlook."""

from datetime import UTC, date, datetime, timedelta

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse
from gridagent.data.provider import merge_forecasts, month_chunks
from gridagent.monitor.daily import build_briefing
from gridagent.monitor.forecasts import miss_stories, outlook

from conftest import FIXTURE_DIR

DAY = date(2026, 11, 26)


def response(endpoint: str, values_by_day: dict[date, dict[str, float]]) -> TimeSeriesResponse:
    """Hourly rows, every hour of a day with the same values (CET: UTC + 1 h)."""
    rows = []
    for day, values in sorted(values_by_day.items()):
        start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=1)
        rows += [DataRow(timestamp=start + timedelta(hours=h), values=dict(values))
                 for h in range(24)]  # fmt: skip
    keys = sorted({k for v in values_by_day.values() for k in v})
    return TimeSeriesResponse(endpoint=endpoint, timezone="Europe/Berlin", unit="MW",
                              interval_minutes=60, series=[SeriesInfo(id=k, name=k) for k in keys],
                              data=rows)  # fmt: skip


def test_merge_adds_onshore_and_offshore_and_needs_every_part() -> None:
    t = datetime(2026, 11, 26, tzinfo=UTC)
    part = lambda kind, v: TimeSeriesResponse(  # noqa: E731
        endpoint="public_power_forecast", timezone="Europe/Berlin", unit="MW",
        interval_minutes=15, series=[SeriesInfo(id=kind, name=kind)],
        data=[DataRow(timestamp=t, values={kind: v}),
              DataRow(timestamp=t + timedelta(minutes=15), values={kind: None if kind == "solar"
                                                                    else v})])  # fmt: skip
    parts = {"wind_onshore": 20_000, "wind_offshore": 5_000, "solar": 1_000, "load": 60_000}
    merged = merge_forecasts({kind: part(kind, value) for kind, value in parts.items()})
    first, second = merged.data
    assert first.values == {"wind": 25_000, "solar": 1_000, "load": 60_000,
                            "residual_load": 34_000}  # fmt: skip
    assert second.values["residual_load"] is None  # never a residual load from partial data


def test_month_chunks_cache_past_months_and_run_the_current_one_to_tomorrow() -> None:
    today = date(2026, 11, 16)
    october = (date(2026, 10, 1), date(2026, 10, 31))
    november_to_tomorrow = (date(2026, 11, 1), date(2026, 11, 17))
    assert month_chunks(date(2026, 10, 18), date(2026, 11, 15), today) == [
        october,
        november_to_tomorrow,
    ]


def days(n: int) -> list[date]:
    return [DAY - timedelta(days=i) for i in range(n, -1, -1)]


def worlds(miss_mw: float):
    """Actual and forecast wind for 29 days; forecasts miss by +-1 GW, DAY by `miss_mw`."""
    actual, forecast = {}, {}
    for i, d in enumerate(days(28)):
        wind = 10_000.0 + 1000 * (i % 5)
        miss = miss_mw if d == DAY else (1000.0 if i % 2 else -1000.0)
        actual[d] = {"wind_onshore": wind + miss, "wind_offshore": 0.0, "solar": 0.0,
                     "load": 50_000.0}  # fmt: skip
        forecast[d] = {"wind": wind, "solar": 0.0, "load": 50_000.0,
                       "residual_load": 50_000.0 - wind}  # fmt: skip
    return response("public_power", actual), response("forecast", forecast)


def test_a_miss_is_news_only_when_unusual_and_large() -> None:
    assert miss_stories(DAY, *worlds(miss_mw=1500)) == []  # 36 GWh: normal-sized
    [story] = miss_stories(DAY, *worlds(miss_mw=-6000))
    assert story.kind == "forecast_miss" and story.facts["miss_gwh"] == -144.0
    assert "Wind came in" in story.title and "below its day-ahead forecast" in story.title


def test_outlook_from_recorded_forecasts() -> None:
    data = FixtureDataSource(FIXTURE_DIR)
    generation = data.public_power(date(2026, 10, 1), date(2026, 10, 29))
    expected = outlook(date(2026, 10, 30), data.forecast(date(2026, 10, 30), date(2026, 10, 30)),
                       generation)  # fmt: skip
    assert expected.wind_gwh == 98.21 and expected.normal_wind_gwh == 491.56
    assert expected.headline == (
        "98 GWh of wind expected, 80 % less than a normal day of the last four weeks"
    )


def test_briefing_carries_outlooks_for_published_days_only() -> None:
    result = build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 30),
                            today=date(2026, 10, 31), history={})  # fmt: skip
    assert [o.day for o in result.outlooks] == ["2026-10-31"]  # 1 November not recorded
