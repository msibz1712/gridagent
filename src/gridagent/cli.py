"""Command-line interface for the Agent Garden (`garden`).

Commands so far: `version`, `run`. Later milestones add
`list`, `new` (M3), `eval` and `compare` (M4).

Paths are relative to the current directory and can be overridden with
environment variables (useful in Docker):
  GARDEN_AGENTS_DIR (default: agents), GARDEN_LOG_DIR (default: logs),
  GARDEN_CACHE_DIR (default: .cache/energy_charts)
"""

import json
import os
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gridagent import __version__
from gridagent.agent.config import load_agent
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient, berlin_today
from gridagent.data.fixture_source import DataProvider, FixtureDataSource
from gridagent.llm.claude import ClaudeLLM

app = typer.Typer(
    help="Agent Garden: list, run, evaluate and scaffold agents.", no_args_is_help=True
)
console = Console()

FIXTURE_DIR = Path("tests/fixtures")


def _path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, default))


@app.callback()
def main() -> None:
    """Agent Garden CLI."""
    # Keeping a callback means Typer always treats commands as subcommands.


@app.command()
def version() -> None:
    """Print the installed gridagent version."""
    console.print(f"gridagent [bold green]{__version__}[/]")


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent name, e.g. energy_qa")],
    question: Annotated[str, typer.Argument(help="The question to ask")],
    model: Annotated[str | None, typer.Option(help="Override the model from agent.yaml")] = None,
    fixtures: Annotated[
        bool, typer.Option("--fixtures", help="Use recorded fixture data instead of the live API")
    ] = False,
    today: Annotated[
        str | None, typer.Option(help="Pretend today is YYYY-MM-DD (reproducible runs)")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool results")] = False,
) -> None:
    """Ask an agent a question (calls the Claude API: costs a little money)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] Put it in .env and load it with:\n"
            "  set -a && source .env && set +a"
        )
        raise typer.Exit(code=1)

    try:
        loaded = load_agent(_path("GARDEN_AGENTS_DIR", "agents"), agent)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Could not load agent '{agent}':[/] {exc}")
        raise typer.Exit(code=1) from exc

    cfg = loaded.config
    llm = ClaudeLLM(
        model_name=model or cfg.model.name,
        max_tokens=cfg.model.max_tokens,
        effort=cfg.model.effort,
        refusal_fallback=cfg.model.refusal_fallback,
        timeout_seconds=cfg.limits.model_timeout_seconds,
        max_retries=cfg.limits.model_max_retries,
    )
    data: DataProvider = (
        FixtureDataSource(FIXTURE_DIR)
        if fixtures
        else EnergyChartsClient(
            cache=ResponseCache(_path("GARDEN_CACHE_DIR", ".cache/energy_charts"))
        )
    )
    fixed_today = date.fromisoformat(today) if today else None

    with console.status(f"Running {cfg.name} {cfg.version} on {llm.model_name}..."):
        log = Agent(loaded, llm, data, today=lambda: fixed_today or berlin_today()).run(question)

    log_path = _path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
    append_run_log(log_path, log)
    _print_run(log, verbose)
    console.print(f"[dim]Logged run {log.run_id} to {log_path}[/]")
    if log.status != "ok":
        raise typer.Exit(code=2)


def _print_run(log: RunLog, verbose: bool) -> None:
    style = "green" if log.status == "ok" else "yellow" if log.status == "refused" else "red"
    console.print(
        Panel(log.final_answer or "(no answer)", title=f"Answer ({log.status})", border_style=style)
    )
    if log.error:
        console.print(f"[red]Error:[/] {log.error}")

    if log.tool_calls:
        table = Table(title="Tool calls", show_lines=verbose)
        table.add_column("#", justify="right")
        table.add_column("tool")
        table.add_column("input")
        table.add_column("s", justify="right")
        if verbose:
            table.add_column("result")
        for i, call in enumerate(log.tool_calls, 1):
            name = f"[red]{call.name} (error)[/]" if call.is_error else call.name
            row = [str(i), name, json.dumps(call.input), f"{call.latency_s:.2f}"]
            if verbose:
                row.append(call.result[:500])
            table.add_row(*row)
        console.print(table)

    u = log.usage
    cost = f"${log.cost_usd:.4f}" if log.cost_usd is not None else "unknown"
    console.print(
        f"steps={len(log.steps)}  latency={log.latency_s:.1f}s  "
        f"tokens in/out={u.input_tokens}/{u.output_tokens} "
        f"(cache read {u.cache_read_input_tokens}, write {u.cache_creation_input_tokens})  "
        f"est. cost={cost}"
    )
