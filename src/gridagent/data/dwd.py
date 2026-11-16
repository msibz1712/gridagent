"""Weather indicators for Germany from DWD (Deutscher Wetterdienst) open data.

What we use (checked against DWD's dataset descriptions, v24.03):
- 10-minute station observations of wind (FF_10, mean wind speed in m/s) and solar
  radiation (GS_10, global radiation summed over 10 minutes in J/cm2), from
  opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes/.
- Timestamps are UTC (from 2000 on) and mark the END of the 10 minutes they cover.
- Missing values are -999. Licence: CC BY 4.0, attribution "Deutscher Wetterdienst".
- One zip per station: `recent/` holds the last 500 days up to yesterday (updated daily,
  quality check not finished); `historical/` holds older years in multi-year files
  (updated yearly, quality-checked). Where both cover a time, historical wins.

How the national indicator is built (all in this module, never by the model):
1. 10-minute values -> hourly values per station. Hour h covers the six values that end
   at h:10 ... h+1:00. At least 4 of 6 must exist, otherwise the hour is missing.
   Wind: mean speed. Solar: mean irradiance in W/m2 = mean(J/cm2 per 10 min) * 10000 / 600.
2. Hourly values -> Germany: plain mean over the fixed station set in dwd_stations.yaml,
   if at least half of the stations reported that hour.

Limits stated in every answer: station wind is measured about 10 m above ground, not at
turbine hub height (100-150 m), so it is an indicator of how windy it was, not of what
turbines received. The plain mean over the stations is not weighted by where wind or
solar plants stand.
"""

import io
import json
import math
import re
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse

DWD_BASE_URL = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes"
)
BERLIN = ZoneInfo("Europe/Berlin")
LICENSE = "CC BY 4.0, Deutscher Wetterdienst (DWD), opendata.dwd.de"
STATIONS_FILE = Path(__file__).with_name("dwd_stations.yaml")
MISSING = -999.0
MIN_VALUES_PER_HOUR = 4  # of 6 ten-minute values
MIN_STATION_SHARE = 0.5  # of the fixed station set, for a national hourly value


@dataclass(frozen=True)
class Product:
    """One DWD 10-minute dataset and the column we read from it."""

    folder: str  # directory below DWD_BASE_URL
    file_code: str  # part of the zip file names
    column: str  # value column in the produkt_*.txt file
    series: str  # our series id
    unit: str


WIND = Product(folder="wind", file_code="wind", column="FF_10", series="wind_speed", unit="m/s")
SOLAR = Product(
    folder="solar", file_code="SOLAR", column="GS_10", series="solar_radiation", unit="W/m²"
)
PRODUCTS = (WIND, SOLAR)


def load_stations(path: Path = STATIONS_FILE) -> list[dict]:
    return yaml.safe_load(path.read_text())["stations"]


# ---------- pure functions: parsing and aggregation ----------


def parse_product(text: str, column: str) -> dict[datetime, float | None]:
    """produkt_*.txt -> {end of 10-minute interval (UTC): value or None}."""
    lines = text.strip().splitlines()
    header = [h.strip() for h in lines[0].split(";")]
    time_index, value_index = header.index("MESS_DATUM"), header.index(column)
    values: dict[datetime, float | None] = {}
    for line in lines[1:]:
        fields = [f.strip() for f in line.split(";")]
        if len(fields) <= value_index:
            continue
        timestamp = datetime.strptime(fields[time_index], "%Y%m%d%H%M").replace(tzinfo=UTC)
        value = float(fields[value_index])
        values[timestamp] = None if value == MISSING else value
    return values


def to_hourly(values: dict[datetime, float | None], product: Product) -> dict[datetime, float]:
    """10-minute values -> {start of the hour (UTC): hourly value} (see module docstring)."""
    buckets: dict[datetime, list[float]] = defaultdict(list)
    for end, value in values.items():
        if value is None:
            continue
        start_of_interval = end - timedelta(minutes=10)
        buckets[start_of_interval.replace(minute=0)].append(value)
    hourly = {}
    for hour, found in buckets.items():
        if len(found) < MIN_VALUES_PER_HOUR:
            continue
        mean = sum(found) / len(found)
        hourly[hour] = mean * 10_000 / 600 if product is SOLAR else mean  # J/cm2/10min -> W/m2
    return hourly


def national_mean(
    per_station: Iterable[dict[datetime, float]], n_stations: int, hours: list[datetime]
) -> dict[datetime, tuple[float | None, int]]:
    """Mean over stations per hour, and how many stations reported (None below the minimum)."""
    stations = list(per_station)
    result = {}
    for hour in hours:
        found = [s[hour] for s in stations if hour in s]
        enough = len(found) >= math.ceil(n_stations * MIN_STATION_SHARE)
        result[hour] = (sum(found) / len(found) if enough else None, len(found))
    return result


def utc_hours(start: date, end: date) -> list[datetime]:
    """All hours (as UTC starts) of the German local days start..end, DST-aware."""
    first = datetime.combine(start, datetime.min.time(), tzinfo=BERLIN).astimezone(UTC)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=BERLIN)
    hours, cursor = [], first
    while cursor < last.astimezone(UTC):
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


def build_response(
    start: date, end: date, series: dict[str, dict[datetime, tuple[float | None, int]]],
    n_stations: int,
) -> TimeSeriesResponse:  # fmt: skip
    """The national hourly indicators as a TimeSeriesResponse (timestamps in German time)."""
    hours = utc_hours(start, end)
    data = [
        DataRow(
            timestamp=hour.astimezone(BERLIN),
            values={
                sid: None if (value := values[hour][0]) is None else round(value, 2)
                for sid, values in series.items()
            },
        )  # fmt: skip
        for hour in hours
    ]
    units = {p.series: p.unit for p in PRODUCTS}
    names = {"wind_speed": "mean wind speed, ~10 m above ground",
             "solar_radiation": "mean global horizontal irradiance"}  # fmt: skip
    return TimeSeriesResponse(
        endpoint="weather",
        timezone="Europe/Berlin",
        unit=None,
        interval_minutes=60,
        license=LICENSE,
        country="de",
        attributes={
            "source": "Deutscher Wetterdienst (DWD), 10-minute station observations",
            "stations": f"plain mean over a fixed set of {n_stations} DWD stations",
            "caveat": "station wind is measured ~10 m above ground, not at hub height: "
            "an indicator of how windy it was, not of what turbines received",
        },
        series=[SeriesInfo(id=sid, name=names[sid], unit=units[sid]) for sid in series],
        data=data,
    )


# ---------- download ----------


class DwdClient:
    """Downloads station zips from DWD open data, cached on disk.

    `recent/` files change daily and are re-downloaded after `recent_max_age`;
    `historical/` files only change once a year and are kept.
    """

    def __init__(
        self,
        cache_dir: Path,
        http: httpx.Client | None = None,
        base_url: str = DWD_BASE_URL,
        recent_max_age: timedelta = timedelta(hours=6),
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.http = http or httpx.Client(timeout=60.0, follow_redirects=True)
        self.base_url = base_url.rstrip("/")
        self.recent_max_age = recent_max_age
        self.max_retries = max_retries
        self._sleep = sleep
        self._today = today or (lambda: datetime.now(BERLIN).date())
        self._listings: dict[str, list[str]] = {}

    def _get(self, path: str) -> bytes:
        url = f"{self.base_url}/{path}"
        problem = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.http.get(url)
            except httpx.TransportError as exc:
                problem = f"network error: {exc!r}"
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code == 404:
                    raise NoDataError(f"DWD has no file {path}")
                problem = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    break
            if attempt < self.max_retries:
                self._sleep(2.0 * 2**attempt)
        raise EnergyChartsError(f"DWD download failed ({path}): {problem}")

    def _cached(self, path: str, max_age: timedelta | None, url_path: str | None = None) -> bytes:
        """File from the disk cache if fresh enough, else downloaded (from `url_path`)."""
        file = self.cache_dir / path
        if file.exists() and (max_age is None or _age(file) < max_age):
            return file.read_bytes()
        content = self._get(url_path or path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return content

    def historical_files(self, product: Product) -> list[str]:
        """Names of all historical zips of a product (one directory listing, cached a week)."""
        if product.folder not in self._listings:
            folder = f"{product.folder}/historical/"
            html = self._cached(folder + "listing.html", timedelta(days=7), url_path=folder)
            self._listings[product.folder] = re.findall(r'href="([^"]+_hist\.zip)"', html.decode())
        return self._listings[product.folder]

    def _hourly(self, path: str, product: Product, max_age: timedelta | None) -> dict:
        """Hourly values of one zip. Converting 80,000 rows takes a while, so the hourly
        result is cached next to the zip and rebuilt only when the zip is downloaded again."""
        content = self._cached(path, max_age)
        hourly_file = self.cache_dir / (path + ".hourly.json")
        if hourly_file.exists() and _age(hourly_file) <= _age(self.cache_dir / path):
            stored = json.loads(hourly_file.read_text())
            return {datetime.fromisoformat(k): v for k, v in stored.items()}
        hourly = to_hourly(read_zip(content, product.column), product)
        hourly_file.write_text(json.dumps({k.isoformat(): v for k, v in hourly.items()}))
        return hourly

    def station_hourly(
        self, product: Product, station: str, start: date, end: date
    ) -> dict[datetime, float]:
        """Hourly values (UTC hour start) of one station, from every file covering the period.
        Recent first, then historical on top: where both exist, the checked values win."""
        hourly: dict[datetime, float] = {}
        recent_from = self._today() - timedelta(days=495)
        if end >= recent_from:
            path = f"{product.folder}/recent/10minutenwerte_{product.file_code}_{station}_akt.zip"
            try:
                hourly |= self._hourly(path, product, self.recent_max_age)
            except NoDataError:
                pass  # station has no recent file
        if start < recent_from + timedelta(days=30):
            pattern = re.compile(rf"_{station}_(\d{{8}})_(\d{{8}})_hist\.zip$")
            for name in self.historical_files(product):
                match = pattern.search(name)
                if not match:
                    continue
                first = datetime.strptime(match[1], "%Y%m%d").date()
                last = datetime.strptime(match[2], "%Y%m%d").date()
                if first <= end and start <= last:
                    hourly |= self._hourly(f"{product.folder}/historical/{name}", product, None)
        return hourly


def _age(file: Path) -> timedelta:
    return datetime.now(UTC) - datetime.fromtimestamp(file.stat().st_mtime, UTC)


def read_zip(content: bytes, column: str) -> dict[datetime, float | None]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = next(n for n in archive.namelist() if n.startswith("produkt_"))
        return parse_product(archive.read(name).decode("latin-1"), column)


def fetch_weather(
    client: DwdClient, start: date, end: date, stations: list[dict] | None = None
) -> TimeSeriesResponse:
    """Hourly national wind and solar indicators for the German local days start..end."""
    stations = stations or load_stations()
    hours = utc_hours(start, end)
    margin_start, margin_end = start - timedelta(days=1), end + timedelta(days=1)
    series = {}
    for product in PRODUCTS:
        per_station = [
            client.station_hourly(product, station["id"], margin_start, margin_end)
            for station in stations
        ]
        series[product.series] = national_mean(per_station, len(stations), hours)
    if all(v[0] is None for s in series.values() for v in s.values()):
        raise NoDataError(f"DWD has no weather data from {start} to {end} yet.")
    return build_response(start, end, series, len(stations))
