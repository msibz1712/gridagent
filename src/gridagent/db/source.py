"""Data sources backed by the database.

`DatabaseDataSource` serves a request only if the whole period has been synced
(see `energy.sync_log`); otherwise it raises `NotInDatabase`.
`HybridDataSource` puts it in front of the live APIs: history comes from the
database (fast, no rate limits, any length), and anything not synced yet, such as
today, falls back to the live API. If the database is unreachable, the live API
is used too, so the agent keeps working.

A new connection is opened per request: simple and robust behind Supabase's
pooler, and cheap compared with a model call.
"""

from datetime import date

import psycopg

from gridagent.data.models import TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.db.store import connect, is_covered, read_response
from gridagent.db.tables import TABLES


class NotInDatabase(Exception):
    """The requested period has not (completely) been synced."""


class DatabaseDataSource:
    def __init__(self, url: str) -> None:
        self.url = url

    def _read(
        self, table_name: str, start: date, end: date, region: str = ""
    ) -> TimeSeriesResponse:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        table = TABLES[table_name]
        with connect(self.url) as conn:
            if not is_covered(conn, table, start, end, region):
                raise NotInDatabase(f"{table_name} {region} {start}..{end}")
            response = read_response(conn, table, start, end, region)
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
        return self._read("day_ahead_price", start, end)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        if kind not in ("physical", "trading"):
            raise ValueError("kind must be 'physical' or 'trading'")
        return self._read(f"cross_border_{kind}", start, end)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        raise NotInDatabase("installed capacity is small and always fetched live")

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
        return self._either("day_ahead_price", start, end)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        return self._either("cross_border", start, end, kind=kind)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        return self.live.installed_power(time_step)

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        return self._either("regional", region, sources, start, end)
