"""Data sources backed by the database.

`DatabaseDataSource` serves a request only if the whole period has been synced
(see `energy.sync_log`); otherwise it raises `NotInDatabase`.
`HybridDataSource` puts it in front of the live APIs: history comes from the
database (fast, no rate limits, any length), and anything not synced yet, such as
today, falls back to the live API. If the database is unreachable, the live API
is used too, so the agent keeps working.

A new connection is opened per request: simple and robust behind Supabase's
pooler, and cheap compared with a model call.

With a cache directory, every read is also kept on disk, keyed by the request and
the latest sync of that period: asking about the same period again skips the
download (the slow part), and re-syncing the period makes the cached copy stale.
"""

import gzip
import json
from datetime import date
from pathlib import Path

import psycopg

from gridagent.data.cache import ResponseCache
from gridagent.data.mastr import load_register
from gridagent.data.models import TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.db.store import (
    connect,
    is_covered,
    last_sync_id,
    latest_register,
    read_register,
    read_response,
)
from gridagent.db.tables import TABLES


class NotInDatabase(Exception):
    """The requested period has not (completely) been synced."""


class DatabaseDataSource:
    def __init__(self, url: str, cache_dir: Path | None = None) -> None:
        self.url = url
        self.cache_dir = cache_dir
        self.cache = ResponseCache(cache_dir) if cache_dir is not None else None
        self._register: tuple[date, dict] | None = None  # (export date, aggregate) in memory

    def _read(
        self, table_name: str, start: date, end: date, region: str = ""
    ) -> TimeSeriesResponse:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        table = TABLES[table_name]
        with connect(self.url) as conn:
            if not is_covered(conn, table, start, end, region):
                raise NotInDatabase(f"{table_name} {region} {start}..{end}")
            key = None
            if self.cache is not None:
                sync_id = last_sync_id(conn, table, start, end, region)
                key = ResponseCache.make_key(f"db/{table_name}", {
                    "start": start.isoformat(), "end": end.isoformat(),
                    "region": region, "sync": str(sync_id)})  # fmt: skip
                cached = self.cache.get(key, max_age=None)
                if cached is not None:
                    return TimeSeriesResponse.model_validate(cached)
            response = read_response(conn, table, start, end, region)
        if key is not None and response.data:
            self.cache.set(key, response.model_dump(mode="json"), f"db {table_name} {start}..{end}")
        if not response.data:
            # Synced, but the source has nothing there (e.g. DE-LU prices before Oct 2018):
            # let the live API answer, which reports "no data" the usual way.
            raise NotInDatabase(f"{table_name} {region} {start}..{end}: no rows")
        return response

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._read("public_power", start, end)

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._read("total_power", start, end)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        if bzn != "DE-LU":  # only Germany's prices are synced; never return them for another zone
            raise NotInDatabase(f"prices for {bzn} are not stored")
        return self._read("day_ahead_price", start, end)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        if kind not in ("physical", "trading"):
            raise ValueError("kind must be 'physical' or 'trading'")
        return self._read(f"cross_border_{kind}", start, end)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        raise NotInDatabase("installed capacity is small and always fetched live")

    def weather(self, start: date, end: date) -> TimeSeriesResponse:
        return self._read("weather", start, end)

    def redispatch(self, start: date, end: date):
        raise NotInDatabase("redispatch measures are not stored (licence: own analysis only)")

    def plant_register(self) -> dict:
        """The newest uploaded register. One small query per call checks for a newer
        export; the 4 MB aggregate itself is downloaded once per export (disk + memory)."""
        with connect(self.url) as conn:
            latest = latest_register(conn)
            if latest is None:
                raise NotInDatabase("no plant register uploaded (garden mastr upload)")
            export, _complete = latest
            if self._register is not None and self._register[0] == export:
                return self._register[1]
            file = (self.cache_dir / "mastr" / f"register_{export}.json.gz"
                    if self.cache_dir is not None else None)  # fmt: skip
            if file is None or not file.is_file():
                blob = read_register(conn, export)
                if file is not None:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_bytes(blob)
        data = load_register(file) if file is not None else json.loads(gzip.decompress(blob))
        self._register = (export, data)
        return data

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        response = self._read("smard_regional", start, end, region)
        missing = [s for s in sources if s not in response.series_ids()]
        if missing:
            raise NotInDatabase(f"series {missing} not synced for {region}")
        keep = set(sources)
        return response.model_copy(update={
            "series": [s for s in response.series if s.id in keep],
            "data": [row.model_copy(update={"values": {k: v for k, v in row.values.items()
                                                      if k in keep}}) for row in response.data],
        })  # fmt: skip


class HybridDataSource:
    """Database first, live API as fallback. Same interface as every DataProvider."""

    def __init__(self, database: DatabaseDataSource, live: DataProvider) -> None:
        self.database = database
        self.live = live
        self.last_served_from = ""  # "database" or "live": for logs and tests

    def _either(self, method: str, *args, **kwargs) -> TimeSeriesResponse:
        try:
            response = getattr(self.database, method)(*args, **kwargs)
            self.last_served_from = "database"
            return response
        except (NotInDatabase, psycopg.Error):
            self.last_served_from = "live"
            return getattr(self.live, method)(*args, **kwargs)

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._either("public_power", start, end)

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._either("total_power", start, end)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        return self._either("day_ahead_price", start, end, bzn=bzn)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        return self._either("cross_border", start, end, kind=kind)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        return self.live.installed_power(time_step)

    def weather(self, start: date, end: date) -> TimeSeriesResponse:
        return self._either("weather", start, end)

    def redispatch(self, start: date, end: date):
        return self.live.redispatch(start, end)

    def forecast(self, start: date, end: date) -> TimeSeriesResponse:
        return self.live.forecast(start, end)  # forecasts are not stored

    def plant_register(self):
        try:
            data = self.database.plant_register()
            self.last_served_from = "database"
            return data
        except (NotInDatabase, psycopg.Error):
            self.last_served_from = "live"  # the local copy from `garden mastr sync`
            return self.live.plant_register()

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        return self._either("regional", region, sources, start, end)
