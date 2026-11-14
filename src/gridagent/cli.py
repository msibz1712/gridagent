"""Command-line interface for the Agent Garden (`garden`).

Commands: `list`, `tools`, `validate`, `new`, `run`, `version`.
M4 adds `eval` and `compare`.

Paths are relative to the current directory and can be overridden with
environment variables (useful in Docker):
  GARDEN_AGENTS_DIR (default: agents), GARDEN_TEMPLATE_DIR (default:
  templates/agent_template), GARDEN_LOG_DIR (default: logs),
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
from gridagent.agent.config import AgentConfigError
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient, berlin_today
from gridagent.data.fixture_source import DataProvider, FixtureDataSource
from gridagent.garden.registry import discover, resolve
from gridagent.garden.scaffold import create_agent
from gridagent.llm.claude import ClaudeLLM
from gridagent.tools.definitions import ALL_TOOLS

app = typer.Typer(
    help="Agent Garden: list, run, evaluate and scaffold agents.", no_args_is_help=True
)
console = Console()

FIXTURE_DIR = Path("tests/fixtures")


def _path(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var, default))


def _agents_dir() -> Path:
    return _path("GARDEN_AGENTS_DIR", "agents")


@app.callback()
def main() -> None:
    """Agent Garden CLI."""
    # Keeping a callback means Typer always treats commands as subcommands.


@app.command()
def version() -> None:
    """Print the installed gridagent version."""
    console.print(f"gridagent [bold green]{__version__}[/]")


@app.command("list")
def list_agents() -> None:
    """Show registered agents, their versions and whether each config is valid."""
    entries = discover(_agents_dir())
    if not entries:
        console.print(f"No agents in {_agents_dir()}. Create one with: garden new <name>")
        return
    table = Table(title=f"Agent Garden ({_agents_dir()})")
    for column in ("agent", "versions", "latest", "model", "tools", "owner", "status"):
        table.add_column(column)
    for entry in entries:
        latest = entry.versions[-1] if entry.versions else None
        agent = latest.agent if latest else None
        bad = [v.version for v in entry.versions if v.error]
        if not entry.versions:
            status = "[red]no versions[/]"
        elif bad:
            status = f"[red]invalid: {', '.join(bad)}[/]"
        else:
            status = "[green]valid[/]" + ("" if entry.has_readme else " [yellow](no README)[/]")
        table.add_row(
            entry.name,
            ", ".join(v.version for v in entry.versions),
            latest.version if latest else "-",
            agent.config.model.name if agent else "-",
            ", ".join(agent.config.tools) if agent else "-",
            agent.config.owner if agent else "-",
            status,
        )
    console.print(table)


@app.command()
def tools() -> None:
    """Show the tools agents can use."""
    table = Table(title="Available tools", show_lines=True)
    table.add_column("tool")
    table.add_column("description")
    for name, definition in ALL_TOOLS.items():
        table.add_row(name, definition["description"])
    console.print(table)


@app.command()
def validate(
    agent: Annotated[str | None, typer.Argument(help="Only this agent (default: all)")] = None,
) -> None:
    """Validate agent configs. Exits with code 1 if any is invalid (use in CI)."""
    entries = [e for e in discover(_agents_dir()) if agent is None or e.name == agent]
    if agent and not entries:
        console.print(f"[red]No agent '{agent}' in {_agents_dir()}[/]")
        raise typer.Exit(code=1)
    failures = 0
    for entry in entries:
        if not entry.versions:
            failures += 1
            console.print(f"[red]FAIL[/] {entry.name}: no version folders")
        if not entry.has_readme:
            failures += 1
            console.print(f"[red]FAIL[/] {entry.name}: README.md missing")
        for v in entry.versions:
            if v.error:
                failures += 1
                console.print(f"[red]FAIL[/] {entry.name}@{v.version}\n{v.error}")
            else:
                console.print(f"[green]ok[/]   {entry.name}@{v.version}")
    if failures:
        console.print(f"[red]{failures} problem(s) found[/]")
        raise typer.Exit(code=1)


@app.command()
def new(
    name: Annotated[str, typer.Argument(help="snake_case name for the new agent")],
    owner: Annotated[str, typer.Option(prompt=True, help="Who is responsible for it")],
    description: Annotated[str, typer.Option(prompt=True, help="One sentence: what does it do?")],
) -> None:
    """Create a new agent from the template (agents/<name>/0.1.0/)."""
    template_dir = _path("GARDEN_TEMPLATE_DIR", "templates/agent_template")
    try:
        created = create_agent(_agents_dir(), template_dir, name, owner, description)
    except AgentConfigError as exc:
        console.print(f"[red]Could not create agent:[/]\n{exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Created {created.spec}[/] in {created.directory}")
    console.print(
        "Next steps:\n"
        f"  1. Edit {created.directory / 'system_prompt.md'} and agent.yaml (tools, model)\n"
        f"  2. Describe it in {created.directory.parent / 'README.md'}\n"
        f"  3. garden validate {name}\n"
        f'  4. garden run {name} "What was the average day-ahead price yesterday?"'
    )


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent, e.g. energy_qa or energy_qa@0.1.0")],
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
        loaded = resolve(_agents_dir(), agent)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load agent '{agent}':[/]\n{exc}")
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
