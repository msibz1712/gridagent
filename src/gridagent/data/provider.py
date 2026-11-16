"""The data interface the agent's tools use, and the live implementation.

Two implementations share this interface:
- `LiveDataSource`: Energy-Charts API + SMARD + DWD weather (real network, cached)
- `FixtureDataSource` (fixture_source.py): recorded responses for offline tests/evals
"""

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient, NoDataError, berlin_today
from gridagent.data.dwd import DwdClient, fetch_weather
from gridagent.data.mastr import load_register
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse
from gridagent.data.netztransparenz import Measure, NetztransparenzClient
from gridagent.data.smard import SMARD_BASE_URL, SmardClient


class DataProvider(Protocol):
    """Everything the tools need from a data source."""

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse: ...

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse: ...

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse: ...

    def cross_border(
        self, start: date, end: date, kind: str = "physical"
    ) -> TimeSeriesResponse: ...

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse: ...

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse: ...

    def weather(self, start: date, end: date) -> TimeSeriesResponse: ...

    def redispatch(self, start: date, end: date) -> list[Measure]: ...

    def plant_register(self) -> dict[str, Any]: ...

    def forecast(self, start: date, end: date) -> TimeSeriesResponse: ...


def month_chunks(start: date, end: date, today: date) -> list[tuple[date, date]]:
    """Calendar months covering start..end. The current month always runs to tomorrow (the
    API returns what is published), so one request per day serves every window in it."""
    chunks, first = [], start.replace(day=1)
    while first <= end:
        following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = following - timedelta(days=1)
        if last >= today:
            last = max(end, today + timedelta(days=1))
        chunks.append((first, last))
        first = following
    return chunks


# The API also lists a combined "wind" type, but returns no data for it (checked Nov 2026):
# onshore and offshore are fetched and added up instead.
FORECAST_KINDS = ("wind_onshore", "wind_offshore", "solar", "load")
FORECAST_SERIES = ("wind", "solar", "load")


def merge_forecasts(parts: dict[str, TimeSeriesResponse]) -> TimeSeriesResponse:
    """One response with series wind (onshore + offshore), solar, load and residual_load (MW)."""
    values: dict = {}
    for kind, response in parts.items():
        for row in response.data:
            values.setdefault(row.timestamp, {})[kind] = row.values.get(kind)
    rows = []
    for timestamp in sorted(values):
        raw = values[timestamp]
        if any(raw.get(k) is None for k in FORECAST_KINDS):
            v = {k: None for k in (*FORECAST_SERIES, "residual_load")}
        else:
            wind = raw["wind_onshore"] + raw["wind_offshore"]
            v = {"wind": round(wind, 1), "solar": raw["solar"], "load": raw["load"],
                 "residual_load": round(raw["load"] - wind - raw["solar"], 1)}  # fmt: skip
        rows.append(DataRow(timestamp=timestamp, values=v))
    first = next(iter(parts.values()))
    return first.model_copy(update={
        "endpoint": "forecast",
        "attributes": {"horizon": "day-ahead"},
        "series": [SeriesInfo(id=k, name=f"{k.replace('_', ' ')} forecast (day-ahead)")
                   for k in (*FORECAST_SERIES, "residual_load")],
        "data": rows,
    })  # fmt: skip


class LiveDataSource:
    """Energy-Charts for national data, SMARD for control areas. Both cached on disk."""

    def __init__(self, cache_dir: Path) -> None:
        self.energy_charts = EnergyChartsClient(cache=ResponseCache(cache_dir / "energy_charts"))
        # SMARD serves static JSON files; the same HTTP client (cache, retries,
        # Retry-After handling, 404 -> NoDataError) works for it unchanged.
        smard_http = EnergyChartsClient(
            cache=ResponseCache(cache_dir / "smard"), base_url=SMARD_BASE_URL
        )
        self.smard = SmardClient(get_json=lambda path, end: smard_http.get_json(path, {}, end))
        self.dwd = DwdClient(cache_dir / "dwd")
        self._ntp_cache = ResponseCache(cache_dir / "netztransparenz")
        self.register_file = cache_dir / "mastr" / "register.json.gz"
        self._register: dict[str, Any] | None = None
        self._ntp: NetztransparenzClient | None = None

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self.energy_charts.public_power(start, end, country)

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self.energy_charts.total_power(start, end, country)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        return self.energy_charts.day_ahead_price(start, end, bzn)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        return self.energy_charts.cross_border(start, end, kind)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        return self.energy_charts.installed_power(time_step)

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        return self.smard.regional(region, sources, start, end)

    def redispatch(self, start: date, end: date) -> list[Measure]:
        """Needs NTP_CLIENT_ID / NTP_CLIENT_SECRET (free netztransparenz.de account)."""
        if self._ntp is None:
            self._ntp = NetztransparenzClient(
                os.environ.get("NTP_CLIENT_ID", ""),
                os.environ.get("NTP_CLIENT_SECRET", ""),
                cache=self._ntp_cache,
            )
        return self._ntp.measures(start, end)

    def forecast(self, start: date, end: date) -> TimeSeriesResponse:
        """Day-ahead forecasts of wind, solar and load (Energy-Charts), merged into one
        response with a forecast residual load (load minus wind and solar)."""
        # Fetched by calendar month: a past month is one cached request per series, however
        # many different windows (backtests, briefings) ask for parts of it.
        merged: list[TimeSeriesResponse] = []
        for first, last in month_chunks(start, end, berlin_today()):
            try:
                parts = {kind: self.energy_charts.power_forecast(kind, first, last)
                         for kind in FORECAST_KINDS}  # fmt: skip
            except NoDataError:
                if first > berlin_today():
                    continue  # tomorrow's forecast may not be published yet
                raise
            merged.append(merge_forecasts(parts))
        if not merged:
            raise NoDataError(f"No forecast published for {start} to {end}.")
        rows = [r for m in merged for r in m.data if start <= r.timestamp.date() <= end]
        return merged[0].model_copy(update={"data": rows})

    def plant_register(self) -> dict[str, Any]:
        """The MaStR aggregate built by `garden mastr sync` (read once, then kept in memory)."""
        if self._register is None:
            if not self.register_file.is_file():
                raise NoDataError(
                    "The plant register (MaStR) has not been downloaded yet: "
                    "run `garden mastr sync`."
                )
            self._register = load_register(self.register_file)
        return self._register

    def weather(self, start: date, end: date) -> TimeSeriesResponse:
        """DWD publishes observations up to yesterday; a period including today is refused."""
        if end >= berlin_today():
            raise NoDataError("DWD weather observations are published up to yesterday.")
        return fetch_weather(self.dwd, start, end)
