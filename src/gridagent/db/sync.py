"""Load data from the APIs into the database (`garden sync`).

- Energy-Charts tables are fetched one calendar year per request (verified: a
  full year of 15-minute data is one ~20 MB response), so ten years take about
  ten requests per table, well inside the API's rate limit.
- SMARD control-area data comes in weekly files; to stay polite only a few core
  series are synced by default.
- Each chunk is committed on its own: an interrupted sync keeps what it loaded.
- The last days are synced again on every run, because recent data is revised.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.provider import DataProvider
from gridagent.data.smard import REGIONS
from gridagent.db.store import record_sync, upsert_response
from gridagent.db.tables import TABLES, TableSpec

DEFAULT_REGIONAL_SERIES = ("solar", "wind_onshore", "wind_offshore", "load")
RESYNC_DAYS = 7  # recent data is preliminary; refresh it on every run


@dataclass
class SyncReport:
    rows: int = 0
    chunks: int = 0
    skipped: list[str] = field(default_factory=list)  # chunks with no data / errors
    ignored_series: set[str] = field(default_factory=set)  # API series not in the schema


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] at calendar-year boundaries."""
    chunks, cursor = [], start
    while cursor <= end:
        chunk_end = min(date(cursor.year, 12, 31), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def fetcher(
    provider: DataProvider,
    table: TableSpec,
    region: str = "",
    regional_series: tuple[str, ...] = DEFAULT_REGIONAL_SERIES,
):
    """The provider call that delivers this table's data for a date range."""
    if table.name == "public_power":
        return provider.public_power
    if table.name == "total_power":
        return provider.total_power
    if table.name == "day_ahead_price":
        return provider.day_ahead_price
    if table.name == "cross_border_physical":
        return lambda start, end: provider.cross_border(start, end, kind="physical")
    if table.name == "cross_border_trading":
        return lambda start, end: provider.cross_border(start, end, kind="trading")
    if table.name == "smard_regional":
        return lambda start, end: provider.regional(region, list(regional_series), start, end)
    raise ValueError(f"No fetcher for table {table.name}")


def sync_table(
    conn: psycopg.Connection,
    provider: DataProvider,
    table: TableSpec,
    start: date,
    end: date,
    region: str = "",
    report: SyncReport | None = None,
    progress: Callable[[str], None] = lambda message: None,
    regional_series: tuple[str, ...] = DEFAULT_REGIONAL_SERIES,
) -> SyncReport:
    report = report or SyncReport()
    fetch = fetcher(provider, table, region, regional_series)
    label = f"{table.name}{'/' + region if region else ''}"
    for chunk_start, chunk_end in year_chunks(start, end):
        try:
            response = fetch(chunk_start, chunk_end)
        except (NoDataError, EnergyChartsError) as exc:
            report.skipped.append(f"{label} {chunk_start}..{chunk_end}: {type(exc).__name__}")
            progress(f"  skip {label} {chunk_start}..{chunk_end} ({type(exc).__name__})")
            continue
        rows, ignored = upsert_response(conn, table, response, region)
        record_sync(conn, table, chunk_start, chunk_end, rows, region)
        conn.commit()
        report.rows += rows
        report.chunks += 1
        report.ignored_series |= set(ignored)
        progress(f"  {label} {chunk_start}..{chunk_end}: {rows:,} rows")
    return report


def run_sync(
    conn: psycopg.Connection,
    provider: DataProvider,
    since: date,
    until: date,
    tables: list[str] | None = None,
    regional_since: date | None = None,
    progress: Callable[[str], None] = lambda message: None,
    regional_series: tuple[str, ...] = DEFAULT_REGIONAL_SERIES,
) -> SyncReport:
    """Sync the selected tables for [since, until]; control areas from `regional_since`."""
    report = SyncReport()
    for name in tables or list(TABLES):
        table = TABLES[name]
        if table.regional:
            start = max(since, regional_since or since)
            for region in REGIONS:
                sync_table(
                    conn, provider, table, start, until, region, report, progress, regional_series
                )
        else:
            sync_table(conn, provider, table, since, until, "", report, progress)
    return report
