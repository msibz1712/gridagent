"""Data commands: the optional PostgreSQL store (`db`) and the plant register (`mastr`)."""

import os
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from gridagent.cli.common import app, console, env_path, owner_connection
from gridagent.data.client import berlin_today

db_app = typer.Typer(help="Optional PostgreSQL store (e.g. Supabase) in front of the APIs.")
app.add_typer(db_app, name="db")


@db_app.command("init")
def db_init() -> None:
    """Create the `energy` schema and tables (safe to run again)."""
    from gridagent.db.store import init_schema

    with owner_connection() as conn:
        init_schema(conn)
    console.print("Schema [bold]energy[/] is up to date.")


@db_app.command("create-reader")
def db_create_reader(
    env_file: Annotated[Path, typer.Option(help="Where DATABASE_URL_READER is written.")] = Path(
        ".env"
    ),
) -> None:
    """Create the read-only role the app answers with; its URL goes into .env (never printed)."""
    import secrets

    from gridagent.db.store import create_reader, init_schema, reader_url, set_env_line

    password = secrets.token_urlsafe(24)
    with owner_connection() as conn:
        init_schema(conn)
        create_reader(conn, password)
    set_env_line(env_file, "DATABASE_URL_READER", reader_url(os.environ["DATABASE_URL"], password))
    console.print(f"Read-only role ready; DATABASE_URL_READER written to {env_file}.")


@db_app.command("status")
def db_status() -> None:
    """Rows, size and synced date ranges per table."""
    from gridagent.db.store import database_bytes, init_schema, register_exports, status

    with owner_connection() as conn:
        init_schema(conn)  # tables added by newer versions (e.g. plant_register)
        rows, total, exports = status(conn), database_bytes(conn), register_exports(conn)
    table = Table("table", "rows", "size", "covered")
    for row in rows:
        covered = "; ".join(
            f"{region}: " + ", ".join(f"{lo}..{hi}" for lo, hi in ranges)
            for region, ranges in row["coverage"].items() if ranges
        )  # fmt: skip
        table.add_row(
            row["table"], f"{row['rows']:,}", f"{row['bytes'] / 1e6:.1f} MB", covered or "-"
        )
    console.print(table)
    for export, complete, size in exports:
        kind = "complete" if complete else "without solar and storage"
        console.print(f"Plant register of {export}: {kind}, {size / 1e6:.1f} MB")
    console.print(f"Database size: {total / 1e6:.1f} MB (Supabase free plan: 500 MB)")


@db_app.command("sync")
def db_sync(
    since: Annotated[
        str | None,
        typer.Option(
            help="First day (YYYY-MM-DD). Default: continue from the "
            "last synced day, minus a week of revisions."
        ),
    ] = None,
    until: Annotated[str | None, typer.Option(help="Last day. Default: yesterday.")] = None,
    tables: Annotated[
        list[str] | None, typer.Option("--table", help="Only this table (repeatable).")
    ] = None,
    regional_since: Annotated[
        str | None, typer.Option(help="First day for control-area (SMARD) data.")
    ] = None,
) -> None:
    """Load API data into the database (complete days only; re-syncing is safe)."""
    from datetime import timedelta

    from gridagent.data.provider import LiveDataSource
    from gridagent.db.store import covered_ranges, init_schema
    from gridagent.db.sync import RESYNC_DAYS, run_sync
    from gridagent.db.tables import TABLES

    unknown = sorted(set(tables or []) - set(TABLES))
    if unknown:
        console.print(f"[red]Unknown table(s): {unknown}.[/] Known: {', '.join(TABLES)}")
        raise typer.Exit(code=1)
    end = date.fromisoformat(until) if until else berlin_today() - timedelta(days=1)
    with owner_connection() as conn:
        init_schema(conn)
        if since:
            start = date.fromisoformat(since)
        else:
            ranges = covered_ranges(conn, TABLES["public_power"])
            if not ranges:
                console.print("[red]Nothing synced yet:[/] pass --since for the first load.")
                raise typer.Exit(code=1)
            start = ranges[-1][1] - timedelta(days=RESYNC_DAYS)
        regional = date.fromisoformat(regional_since) if regional_since else None
        console.print(f"Syncing {start}..{end}")
        report = run_sync(
            conn, LiveDataSource(env_path("GARDEN_CACHE_DIR", ".cache")), start, end,
            tables=tables, regional_since=regional, progress=console.print,
        )  # fmt: skip
    console.print(f"Done: {report.rows:,} rows in {report.chunks} chunks.")
    if report.ignored_series:
        console.print(f"[yellow]API series without a column:[/] {sorted(report.ignored_series)}")
    if report.skipped:
        console.print(f"[yellow]Skipped:[/] {len(report.skipped)} chunk(s)")
        for item in report.skipped:
            console.print(f"  {item}")


mastr_app = typer.Typer(help="Marktstammdatenregister: every power plant and storage unit.")
app.add_typer(mastr_app, name="mastr")


def _register_file() -> Path:
    return env_path("GARDEN_CACHE_DIR", ".cache") / "mastr" / "register.json.gz"


@mastr_app.command("sync")
def mastr_sync(
    light: Annotated[
        bool, typer.Option(help="Skip solar and storage (~1.6 GB of the ~1.7 GB download).")
    ] = False,
    url: Annotated[
        str | None, typer.Option(help="A specific export ZIP (default: newest).")
    ] = None,
    upload: Annotated[
        bool, typer.Option(help="Also store it in the database (for the website).")
    ] = False,
) -> None:
    """Download the register's unit files and store the aggregate (takes ~30 min in full)."""
    from gridagent.data.mastr import MastrError, sync_register

    try:
        data = sync_register(_register_file(), url=url, skip_heavy=light, progress=console.print)
    except MastrError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"Register of {data['export_date']}: {len(data['rows']):,} rows, "
                  f"{len(data['places']):,} places -> {_register_file()}")  # fmt: skip
    if data["skipped"]:
        console.print(f"Skipped units: {data['skipped']}")
    if upload:
        mastr_upload()


@mastr_app.command("upload")
def mastr_upload() -> None:
    """Store the downloaded register in the database, so the website (Docker) can use it."""
    from gridagent.db.store import init_schema, upload_register

    if not _register_file().is_file():
        console.print("[red]No register yet:[/] run `garden mastr sync` first.")
        raise typer.Exit(code=1)
    blob = _register_file().read_bytes()
    with owner_connection() as conn:
        init_schema(conn)
        export, complete = upload_register(conn, blob)
    kind = "complete" if complete else "without solar and storage"
    console.print(f"Uploaded the plant register of {export} ({kind}, {len(blob) / 1e6:.1f} MB).")


@mastr_app.command("export")
def mastr_export(
    out: Annotated[Path, typer.Argument(help="CSV file to write.")],
    technology: Annotated[str, typer.Option(help="e.g. wind_onshore, solar, coal.")] = "wind",
    state: Annotated[str | None, typer.Option(help="e.g. Nordrhein-Westfalen.")] = None,
    first_year: Annotated[int, typer.Option(help="First year in the panel.")] = 2000,
) -> None:
    """Municipality x technology x year panel as CSV (dl-de/by-2-0: cite the source)."""
    import csv

    from gridagent.data.mastr import ATTRIBUTION, load_register
    from gridagent.tools.register import PANEL_COLUMNS, PlantRegister, RegisterQueryError, panel

    if not _register_file().is_file():
        console.print("[red]No register yet:[/] run `garden mastr sync` first.")
        raise typer.Exit(code=1)
    register = PlantRegister(load_register(_register_file()))
    try:
        rows = panel(register, state, technology, first_year)
    except RegisterQueryError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PANEL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    console.print(f"{len(rows):,} rows -> {out}")
    console.print(f"Source to cite: {ATTRIBUTION}, register of {register.export_date}.")
