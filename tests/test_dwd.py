"""DWD weather: parsing, hourly aggregation and the national mean (no network)."""

import io
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from gridagent.data.client import NoDataError
from gridagent.data.dwd import (
    SOLAR,
    WIND,
    DwdClient,
    fetch_weather,
    load_stations,
    national_mean,
    parse_product,
    to_hourly,
    utc_hours,
)
from gridagent.data.fixture_source import FixtureDataSource

from conftest import FIXTURE_DIR

HEADER = "STATIONS_ID;MESS_DATUM;  QN;FF_10;DD_10;eor"


def product_text(start: datetime, values: list[float], header: str = HEADER) -> str:
    """A produkt_*.txt like DWD's: one line per 10 minutes, timestamp = END of interval."""
    lines = [header]
    for i, value in enumerate(values):
        end = start + timedelta(minutes=10 * (i + 1))
        lines.append(f"      3987;{end:%Y%m%d%H%M};    3;{value:6.1f}; 250;eor")
    return "\n".join(lines)


T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def test_parse_reads_utc_end_times_and_missing_values() -> None:
    values = parse_product(product_text(T0, [3.0, -999, 4.0]), "FF_10")
    assert values == {
        T0 + timedelta(minutes=10): 3.0,
        T0 + timedelta(minutes=20): None,
        T0 + timedelta(minutes=30): 4.0,
    }


def test_hour_covers_the_six_values_ending_at_10_past_to_full_hour() -> None:
    # values ending 09:10 ... 10:00 belong to the hour starting 09:00; 10:10 to the next
    values = parse_product(product_text(T0, [2, 2, 2, 2, 2, 8, 100]), "FF_10")
    hourly = to_hourly(values, WIND)
    assert hourly[T0] == pytest.approx(3.0)  # (5 * 2 + 8) / 6
    assert T0 + timedelta(hours=1) not in hourly  # only 1 value: below the 4-of-6 minimum


def test_hour_needs_four_of_six_values() -> None:
    four = to_hourly(parse_product(product_text(T0, [1, 1, 1, 1, -999, -999]), "FF_10"), WIND)
    three = to_hourly(parse_product(product_text(T0, [1, 1, 1, -999, -999, -999]), "FF_10"), WIND)
    assert T0 in four and T0 not in three


def test_solar_joules_per_10_minutes_become_watts_per_square_metre() -> None:
    header = "STATIONS_ID;MESS_DATUM;  QN;DS_10;GS_10;SD_10;LS_10;eor"
    lines = [header] + [
        f"3987;{T0 + timedelta(minutes=10 * (i + 1)):%Y%m%d%H%M};3;1.0;30.0;0.1;10;eor"
        for i in range(6)
    ]
    hourly = to_hourly(parse_product("\n".join(lines), "GS_10"), SOLAR)
    # 30 J/cm2 in 600 s = 300,000 J/m2 / 600 s = 500 W/m2
    assert hourly[T0] == pytest.approx(500.0)


def test_national_mean_needs_half_of_the_stations() -> None:
    stations = [{T0: 2.0}, {T0: 4.0}, {}, {}]  # 2 of 4 reported
    assert national_mean(stations, 4, [T0])[T0] == (3.0, 2)
    assert national_mean(stations[:1] + [{}, {}, {}], 4, [T0])[T0] == (None, 1)


def test_local_days_become_utc_hours_including_dst() -> None:
    assert len(utc_hours(date(2026, 11, 2), date(2026, 11, 2))) == 24
    assert len(utc_hours(date(2026, 10, 25), date(2026, 10, 25))) == 25  # clocks go back
    assert utc_hours(date(2026, 11, 2), date(2026, 11, 2))[0] == datetime(
        2026, 11, 1, 23, tzinfo=UTC
    )  # midnight in Berlin (CET) is 23:00 UTC


def test_station_list_is_the_documented_fixed_set() -> None:
    stations = load_stations()
    assert len(stations) == 65 and len({s["id"] for s in stations}) == 65


def _zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("produkt_zehn_min_ff_20250514_20261114_03987.txt", text)
    return buffer.getvalue()


def test_client_downloads_aggregates_and_caches(tmp_path) -> None:
    """Two stations, one product file each, served by a fake DWD; second call is offline."""
    t0 = datetime(2026, 11, 1, 23, tzinfo=UTC)  # 2 Nov 00:00 in Berlin
    files = {"wind": product_text(t0, [3.0] * 150), "SOLAR": product_text(
        t0, [6.0] * 150, header="STATIONS_ID;MESS_DATUM;  QN;GS_10;DD_10;eor")}  # fmt: skip
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        code = "wind" if "/wind/" in request.url.path else "SOLAR"
        return httpx.Response(200, content=_zip(files[code]))

    stations = [{"id": "03987"}, {"id": "00183"}]
    client = DwdClient(tmp_path, http=httpx.Client(transport=httpx.MockTransport(handler)),
                       today=lambda: date(2026, 11, 15))  # fmt: skip
    response = fetch_weather(client, date(2026, 11, 2), date(2026, 11, 2), stations)
    first = response.data[0].values
    assert first == {"wind_speed": 3.0, "solar_radiation": 100.0}  # 6 J/cm2 -> 100 W/m2
    assert len(response.data) == 24 and len(requests) == 4  # 2 stations x 2 products

    fetch_weather(client, date(2026, 11, 2), date(2026, 11, 2), stations)
    assert len(requests) == 4  # served from the disk cache


def test_404_means_no_data(tmp_path) -> None:
    client = DwdClient(
        tmp_path, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        today=lambda: date(2026, 11, 15),
    )  # fmt: skip
    with pytest.raises(NoDataError):
        fetch_weather(client, date(2026, 11, 2), date(2026, 11, 2), [{"id": "03987"}])


def test_recorded_weather_fixture_is_complete() -> None:
    week = FixtureDataSource(FIXTURE_DIR).weather(date(2026, 11, 2), date(2026, 11, 8))
    assert len(week.data) == 168 and week.interval_minutes == 60
    assert week.get_series("wind_speed").unit == "m/s"
    assert "10 m" in week.attributes["caveat"]
