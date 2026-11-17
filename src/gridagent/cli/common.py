"""Shared by every command group: the Typer app, the console, paths and small checks."""

import importlib.util
import os
from pathlib import Path

import typer
from rich.console import Console

from gridagent.services import (
    reader_database_url,
)

app = typer.Typer(
    help="Agent Garden: list, run, evaluate and scaffold agents.", no_args_is_help=True
)
console = Console()

FIXTURE_DIR = Path("tests/fixtures")


def env_path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, default))


def agents_dir() -> Path:
    return env_path("GARDEN_AGENTS_DIR", "agents")


@app.callback()
def main() -> None:
    """Agent Garden CLI."""
    # Keeping a callback means Typer always treats commands as subcommands.


def require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] Put it in .env and load it with:\n"
            "  set -a && source .env && set +a"
        )
        raise typer.Exit(code=1)


def db_url() -> str | None:
    return reader_database_url()


def owner_connection():
    if importlib.util.find_spec("psycopg") is None:
        console.print("[red]Needs the optional 'db' extra:[/] uv sync --extra db")
        raise typer.Exit(code=1)
    url = os.environ.get("DATABASE_URL")
    if not url:
        console.print("[red]DATABASE_URL is not set.[/] Put it in .env (never commit it).")
        raise typer.Exit(code=1)
    from gridagent.db.store import connect

    return connect(url)
