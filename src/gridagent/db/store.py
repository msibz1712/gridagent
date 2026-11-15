"""Read and write time series in PostgreSQL.

Writing: rows go into a temporary table with COPY (fast bulk load), then one
INSERT ... ON CONFLICT DO UPDATE merges them. Re-running a sync therefore never
creates duplicates; it simply refreshes values (recent data gets revised).

Reading: rows come back as the same `TimeSeriesResponse` the API clients return,
so the agent's tools and calculations work unchanged. Only series that have at
least one value in the period are included, so "series absent in this period"
(e.g. nuclear after April 2023) stays distinguishable from "value missing".
"""

import gzip
import json
import sys
from array import array
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse
from gridagent.db.tables import PERCENT_SERIES, SCALE, SCHEMA, TABLES, TableSpec, schema_sql

BERLIN = ZoneInfo("Europe/Berlin")
NULL_MARKER = -(2**31)  # smallest integer: stands for NULL in packed reads only


def connect(url: str, autocommit: bool = False) -> psycopg.Connection:
    """Encrypted connection; all timestamps are returned in German local time."""
    conn = psycopg.connect(url, sslmode="prefer", connect_timeout=15, autocommit=autocommit)
    conn.execute("set time zone 'Europe/Berlin'")
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(schema_sql())
    conn.commit()


def day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, day after end 00:00) in German time, as aware datetimes."""
    lower = datetime.combine(start, time(0), tzinfo=BERLIN)
    upper = datetime.combine(end + timedelta(days=1), time(0), tzinfo=BERLIN)
    return lower, upper


# ---------- writing ----------


def to_stored(value: float | None) -> int | None:
    """API value -> fixed-point integer (see tables.py). Values out of range fail loudly."""
    return None if value is None else round(value * SCALE)


def upsert_response(
    conn: psycopg.Connection, table: TableSpec, response: TimeSeriesResponse, region: str = ""
) -> tuple[int, list[str]]:
    """Merge a response into its table. Returns (rows written, API series not stored)."""
    columns = [c for c in table.series if c in response.series_ids()]
    ignored = sorted(set(response.series_ids()) - set(table.series))
    key_columns = list(table.keys)
    all_columns = key_columns + columns

    temp = sql.Identifier(f"incoming_{table.name}")
    target = sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table.name))
    with conn.cursor() as cur:
        cur.execute(sql.SQL("create temp table {} (like {} including defaults) on commit drop")
                    .format(temp, target))  # fmt: skip
        copy_sql = sql.SQL("copy {} ({}) from stdin").format(
            temp, sql.SQL(", ").join(map(sql.Identifier, all_columns))
        )
        with cur.copy(copy_sql) as copy:
            for row in response.data:
                keys = [region, row.timestamp] if table.regional else [row.timestamp]
                copy.write_row([*keys, *(to_stored(row.values.get(c)) for c in columns)])
        updates = sql.SQL(", ").join(
            sql.SQL("{0} = excluded.{0}").format(sql.Identifier(c)) for c in columns
        )
        cur.execute(
            sql.SQL("insert into {target} ({cols}) select {cols} from {temp} "
                    "on conflict ({keys}) do update set {updates}").format(
                target=target, temp=temp,
                cols=sql.SQL(", ").join(map(sql.Identifier, all_columns)),
                keys=sql.SQL(", ").join(map(sql.Identifier, key_columns)),
                updates=updates,
            )
        )  # fmt: skip
        written = cur.rowcount
    return written, ignored


def record_sync(
    conn: psycopg.Connection, table: TableSpec, date_from: date, date_until: date,
    rows: int, region: str = "",
) -> None:  # fmt: skip
    conn.execute(
        f"insert into {SCHEMA}.sync_log (table_name, region, date_from, date_until, rows_written) "
        "values (%s, %s, %s, %s, %s)",
        (table.name, region, date_from, date_until, rows),
    )


# ---------- coverage ----------


def covered_ranges(conn: psycopg.Connection, table: TableSpec, region: str = "") -> list[tuple]:
    """Synced date ranges, merged where they touch or overlap: [(from, until), ...]."""
    rows = conn.execute(
        f"select date_from, date_until from {SCHEMA}.sync_log "
        "where table_name = %s and region = %s order by date_from",
        (table.name, region),
    ).fetchall()
    merged: list[list[date]] = []
    for date_from, date_until in rows:
        if merged and date_from <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], date_until)
        else:
            merged.append([date_from, date_until])
    return [tuple(r) for r in merged]


def last_sync_id(
    conn: psycopg.Connection, table: TableSpec, start: date, end: date, region: str = ""
) -> int | None:
    """Id of the latest sync that touched [start, end]: changes exactly when its data may have.

    Used as part of the disk-cache key, so re-syncing a period refreshes cached reads of
    it, while the nightly sync of the last days leaves older cached periods valid.
    """
    return conn.execute(
        f"select max(id) from {SCHEMA}.sync_log where table_name = %s and region = %s "
        "and date_from <= %s and date_until >= %s",
        (table.name, region, end, start),
    ).fetchone()[0]


def is_covered(
    conn: psycopg.Connection, table: TableSpec, start: date, end: date, region: str = ""
) -> bool:
    return any(lo <= start and end <= hi for lo, hi in covered_ranges(conn, table, region))


# ---------- reading ----------


def interval_minutes(timestamps: list[datetime]) -> int | None:
    """The fixed step between rows, like the API reports it; None if the step varies.

    Prices, for example, were hourly until 30 Sep 2025 and are quarter-hourly since,
    so a period spanning the switch has no single interval (the API then says null too).
    Compared in UTC: Python subtracts two datetimes of the *same* time zone by wall
    clock, which would make the DST switch (01:00 -> 03:00) look like a 2-hour step.
    """
    utc = [t.astimezone(UTC) for t in timestamps]
    steps = {b - a for a, b in zip(utc, utc[1:], strict=False)}
    if len(steps) != 1:
        return None
    return int(steps.pop().total_seconds() // 60)


def _unpack(blob: bytes, typecode: str) -> array:
    """Big-endian bytes from PostgreSQL's int4send/int8send -> numbers."""
    values = array(typecode)
    values.frombytes(blob)
    if sys.byteorder == "little":
        values.byteswap()
    return values


def _fetch_rows(
    conn: psycopg.Connection, table: TableSpec, start: date, end: date, region: str = ""
) -> list[tuple]:
    """Rows (ts, value per series...) for the period, ordered by time.

    The download, not the database, is the slow part (about 2 MB/s to Supabase), so
    the values are sent compactly: per day and series, all stored integers packed
    into one block of raw 4-byte numbers. Row by row, every value would carry a
    4-byte length header as well, which doubles the transfer (measured: 8 years
    36 s -> 16 s). NULL travels as NULL_MARKER and becomes None again.
    """
    lower, upper = day_bounds(start, end)
    target = sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table.name))
    packed = sql.SQL(", ").join(
        sql.SQL("string_agg(int4send(coalesce({}, (%s)::integer)), '' order by ts)").format(
            sql.Identifier(column)
        )
        for column in table.series
    )
    where = sql.SQL("ts >= %s and ts < %s") + (
        sql.SQL(" and region = %s") if table.regional else sql.SQL("")
    )
    params: list[Any] = [NULL_MARKER] * len(table.series) + [lower, upper]
    params += [region] if table.regional else []
    query = sql.SQL(
        "select string_agg(int8send(extract(epoch from ts)::int8), '' order by ts), {} "
        "from {} where {} group by (ts at time zone 'Europe/Berlin')::date order by min(ts)"
    ).format(packed, target, where)

    rows: list[tuple] = []
    for day in conn.execute(query, params, binary=True).fetchall():
        seconds = _unpack(day[0], "q")
        columns = [
            [None if v == NULL_MARKER else v / SCALE for v in _unpack(blob, "i")]
            for blob in day[1:]
        ]
        for i, second in enumerate(seconds):
            timestamp = datetime.fromtimestamp(second, UTC)
            rows.append((timestamp, *(column[i] for column in columns)))
    return rows


def read_response(
    conn: psycopg.Connection, table: TableSpec, start: date, end: date, region: str = ""
) -> TimeSeriesResponse:
    rows = _fetch_rows(conn, table, start, end, region)
    present = [c for i, c in enumerate(table.series) if any(r[i + 1] is not None for r in rows)]
    data = []
    for row in rows:
        values = dict(zip(table.series, row[1:], strict=True))
        data.append(
            DataRow(
                timestamp=row[0].astimezone(BERLIN),
                values={c: values[c] for c in present},
            )
        )  # fmt: skip
    attributes = dict(table.attributes)
    units = dict(table.series_units)
    if table.regional:
        attributes |= {"region": region, "source": "SMARD.de (Bundesnetzagentur)"}
    return TimeSeriesResponse(
        endpoint=table.endpoint,
        timezone="Europe/Berlin",
        unit=table.unit or None,
        interval_minutes=interval_minutes([row.timestamp for row in data]),
        license=table.license,
        country="de",
        bidding_zone="DE-LU" if table.endpoint == "price" else None,
        attributes=attributes | {"served_from": "database"},
        series=[SeriesInfo(id=c, name=c.replace("_", " "),
                           unit="%" if c in PERCENT_SERIES else units.get(c)) for c in present],
        data=data,
    )  # fmt: skip


# ---------- plant register (one compressed aggregate per export) ----------

REGISTER_EXPORTS_KEPT = 4


def upload_register(conn: psycopg.Connection, gz_bytes: bytes) -> tuple[str, bool]:
    """Store a register aggregate (the .json.gz `garden mastr sync` writes); keep the newest 4.

    Returns (export date, complete). A complete copy is never replaced by a light one
    of the same day.
    """
    data = json.loads(gzip.decompress(gz_bytes))
    export, complete = data["export_date"], bool(data.get("complete", True))
    conn.execute(
        f"""insert into {SCHEMA}.plant_register (export_date, complete, data)
            values (%s, %s, %s)
            on conflict (export_date) do update
            set complete = excluded.complete, data = excluded.data, uploaded_at = now()
            where excluded.complete or not {SCHEMA}.plant_register.complete""",
        (export, complete, gz_bytes),
    )
    conn.execute(
        f"""delete from {SCHEMA}.plant_register where export_date not in (
              select export_date from {SCHEMA}.plant_register
              order by export_date desc limit %s)""",
        (REGISTER_EXPORTS_KEPT,),
    )
    conn.commit()
    return export, complete


def latest_register(conn: psycopg.Connection) -> tuple[date, bool] | None:
    """(export date, complete) of the newest stored register, without downloading it."""
    return conn.execute(
        f"select export_date, complete from {SCHEMA}.plant_register "
        "order by export_date desc limit 1"
    ).fetchone()


def read_register(conn: psycopg.Connection, export: date) -> bytes:
    row = conn.execute(
        f"select data from {SCHEMA}.plant_register where export_date = %s", (export,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No plant register of {export} in the database.")
    return bytes(row[0])


def register_exports(conn: psycopg.Connection) -> list[tuple[date, bool, int]]:
    """(export date, complete, bytes) of every stored register, newest first."""
    return conn.execute(
        f"select export_date, complete, length(data) from {SCHEMA}.plant_register "
        "order by export_date desc"
    ).fetchall()


# ---------- status ----------


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per table: rows, size on disk and covered date ranges (for `garden db status`)."""
    result = []
    for table in TABLES.values():
        rows, size = conn.execute(
            f"select count(*), pg_total_relation_size('{SCHEMA}.{table.name}') "
            f"from {SCHEMA}.{table.name}"
        ).fetchone()
        regions = [""]
        if table.regional:
            regions = [r[0] for r in conn.execute(
                f"select distinct region from {SCHEMA}.sync_log where table_name = %s",
                (table.name,)).fetchall()]  # fmt: skip
        coverage = {region or "DE": covered_ranges(conn, table, region) for region in regions}
        result.append({"table": table.name, "rows": rows, "bytes": size, "coverage": coverage})
    return result


def database_bytes(conn: psycopg.Connection) -> int:
    return conn.execute("select pg_database_size(current_database())").fetchone()[0]


# ---------- read-only role ----------

READER_ROLE = "gridagent_reader"


def create_reader(conn: psycopg.Connection, password: str, role: str = READER_ROLE) -> None:
    """Create (or reset the password of) a login role that can only SELECT from `energy`.

    The app answers questions with this role, so a bug in the app cannot change data.
    Default privileges make tables added later readable as well.
    """
    exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
    verb = sql.SQL("alter" if exists else "create")
    name = sql.Identifier(role)
    conn.execute(
        sql.SQL("{} role {} with login password {}").format(verb, name, sql.Literal(password))
    )
    for statement in (
        "grant usage on schema {schema} to {role}",
        "grant select on all tables in schema {schema} to {role}",
        "alter default privileges in schema {schema} grant select on tables to {role}",
    ):
        conn.execute(sql.SQL(statement).format(schema=sql.Identifier(SCHEMA), role=name))
    conn.commit()


def reader_url(owner_url: str, password: str, role: str = READER_ROLE) -> str:
    """The owner's connection URL with the reader's credentials.

    Supabase's pooler expects user names of the form `<role>.<project ref>`; the
    project ref is taken over from the owner's user name.
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(owner_url)
    owner_user = parts.username or ""
    user = f"{role}.{owner_user.split('.', 1)[1]}" if "." in owner_user else role
    host = parts.hostname or ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def set_env_line(env_file, key: str, value: str) -> None:
    """Replace or append `key=value` in a .env file without touching other lines."""
    from pathlib import Path

    path = Path(env_file)
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
