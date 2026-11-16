"""Open-Meteo: request shape, caching, weighting, the radiation shift, and lead-time skill."""

from datetime import date

import httpx
import pytest

from gridagent.data.cache import ResponseCache
from gridagent.data.client import NoDataError
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.mastr import load_register
from gridagent.data.openmeteo import OFFSHORE, STATES, OpenMeteoClient, capacity_weights, national
from gridagent.monitor.forecast_skill import correlation, lead_skill

from conftest import FIXTURE_DIR

N = len(STATES) + len(OFFSHORE)


def fake_api(status: int = 200):
    """Answers like Open-Meteo: a list with one object per point; records the requests."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": True, "reason": "out of range"})
        names = request.url.params["hourly"].split(",")
        hourly = {"time": ["2026-11-02T10:00", "2026-11-02T11:00", "2026-11-02T12:00"]}
        hourly |= {n: [10.0, 20.0, 30.0] for n in names}
        return httpx.Response(200, json=[{"hourly": hourly}] * N)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_previous_runs_are_asked_with_the_lead_suffix_and_cached(tmp_path) -> None:
    http, seen = fake_api()
    client = OpenMeteoClient(ResponseCache(tmp_path), http, today=lambda: date(2026, 11, 16))
    points = client.points(date(2026, 11, 2), date(2026, 11, 2), lead_days=3)
    assert len(points) == N and points[0]["wind_100m"] == [10.0, 20.0, 30.0]
    request = seen[0]
    assert request.url.host == "previous-runs-api.open-meteo.com"
    assert "wind_speed_100m_previous_day3" in request.url.params["hourly"]
    assert request.url.params["wind_speed_unit"] == "ms"
    client.points(date(2026, 11, 2), date(2026, 11, 2), lead_days=3)
    assert len(seen) == 1  # a past period comes from the cache


def test_latest_forecast_uses_the_forecast_api_and_bad_ranges_are_no_data(tmp_path) -> None:
    http, seen = fake_api()
    OpenMeteoClient(http=http).points(date(2026, 11, 16), date(2026, 11, 22), lead_days=None)
    assert seen[0].url.host == "api.open-meteo.com"
    with pytest.raises(ValueError):
        OpenMeteoClient(http=http).points(date(2026, 11, 2), date(2026, 11, 2), lead_days=8)
    http, _ = fake_api(status=400)
    with pytest.raises(NoDataError, match="out of range"):
        OpenMeteoClient(http=http).points(date(2020, 1, 1), date(2020, 1, 1), lead_days=1)


def test_weights_and_the_one_hour_radiation_shift() -> None:
    points = [{"time": ["2026-11-02T10:00", "2026-11-02T11:00"],
               "wind_100m": [4.0 if i == 0 else 8.0] * 2, "solar_radiation": [100.0, 200.0],
               "temperature": [5.0, 5.0]} for i in range(N)]  # fmt: skip
    wind_w = [3.0] + [0.0] * (N - 3) + [1.0, 0.0]  # point 0 has 3x the capacity of point N-2
    response = national(points, wind_w, [1.0] * N, "test", lead_days=1)
    first, second = response.data
    assert first.values["wind_100m"] == 5.0  # (3 * 4 + 1 * 8) / 4
    assert first.values["solar_radiation"] == 200.0  # the 11:00 value covers 10:00-11:00
    assert second.values["solar_radiation"] is None  # its hour is not in the request yet


def test_capacity_weights_from_a_register_without_solar() -> None:
    register = load_register(FIXTURE_DIR / "mastr" / "register_excerpt.json.gz")
    wind, solar, note = capacity_weights(register)
    by_state = dict(zip([s for s, *_ in STATES], wind, strict=False))
    assert by_state["Niedersachsen"] > by_state["Saarland"] > 0
    assert solar[: len(STATES)] == [1.0] * len(STATES) and "solar equal" in note
    assert capacity_weights(None)[2] == "equal weights (no plant register)"


def test_recorded_forecasts_track_actual_wind() -> None:
    """October 2026: the day-ahead hub-height index against actual daily wind energy."""
    rows = lead_skill(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 1), date(2026, 10, 31),
                      leads=range(1, 2))  # fmt: skip
    operators, open_meteo = rows
    assert operators["wind_r"] > open_meteo["wind_r"] > 0.9  # operators' forecast is better
    assert correlation({1: 1.0}, {1: 1.0}) == (None, 1)  # too few days: no claim
