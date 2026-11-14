"""Command-line interface for the Agent Garden (`garden`).

Commands: `list`, `tools`, `validate`, `new`, `run`, `runs`, `eval`, `compare`, `ui`, `version`.

Paths are relative to the current directory and can be overridden with
environment variables (useful in Docker):
  GARDEN_AGENTS_DIR (default: agents), GARDEN_TEMPLATE_DIR (default:
  templates/agent_template), GARDEN_LOG_DIR (default: logs),
  GARDEN_CACHE_DIR (default: .cache)
"""

import importlib.util
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
from gridagent.data.client import berlin_today
from gridagent.evals.report import (
    METRIC_ROWS,
    compare,
    comparison_markdown,
    fmt,
    load_report,
    write_report,
)
from gridagent.evals.runner import CaseResult, EvalReport, estimate_live_cost, supported_cases
from gridagent.evals.session import choose_offline_mode, evaluate, load_agent_eval_set
from gridagent.garden.registry import discover, resolve
from gridagent.garden.scaffold import create_agent
from gridagent.services import build_data, build_llm, load_runs, summarize_runs
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
    _require_api_key()

    try:
        loaded = resolve(_agents_dir(), agent)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load agent '{agent}':[/]\n{exc}")
        raise typer.Exit(code=1) from exc

    cfg = loaded.config
    llm = build_llm(cfg, model)
    data = build_data(fixtures, FIXTURE_DIR, _path("GARDEN_CACHE_DIR", ".cache"))
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


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/] Put it in .env and load it with:\n"
            "  set -a && source .env && set +a"
        )
        raise typer.Exit(code=1)


def _print_progress(i: int, n: int, result: CaseResult) -> None:
    mark = "[green]pass[/]" if result.passed else f"[red]FAIL[/] {', '.join(result.failed_checks)}"
    console.print(f"  [{i:>2}/{n}] {result.id:<38} {mark}")


def _print_metrics(report: EvalReport) -> None:
    table = Table(title=f"{report.agent}@{report.agent_version} / {report.model} / {report.mode}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for field, label, kind in METRIC_ROWS:
        table.add_row(label, fmt(getattr(report.metrics, field), kind))
    console.print(table)
    for note in report.notes:
        console.print(f"[dim]{note}[/]")


@app.command("eval")
def eval_command(
    agent: Annotated[str, typer.Argument(help="Agent, e.g. energy_qa or energy_qa@0.1.0")],
    model: Annotated[str | None, typer.Option(help="Override the model from agent.yaml")] = None,
    live: Annotated[bool, typer.Option("--live", help="Real model calls (costs money)")] = False,
    record: Annotated[
        bool, typer.Option("--record", help="With --live: save responses for free replay")
    ] = False,
    oracle: Annotated[
        bool, typer.Option("--oracle", help="Force oracle mode even if a recording exists")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the cost confirmation")] = False,
    min_pass_rate: Annotated[
        float | None, typer.Option(help="Exit with code 1 if pass rate is below this (0-1)")
    ] = None,
) -> None:
    """Evaluate an agent on its eval set (offline by default; --live for real calls)."""
    if record and not live:
        console.print("[red]--record needs --live[/]")
        raise typer.Exit(code=1)
    try:
        loaded = resolve(_agents_dir(), agent)
        eval_set, _ = load_agent_eval_set(loaded)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load:[/]\n{exc}")
        raise typer.Exit(code=1) from exc

    model_name = model or loaded.config.model.name
    if live:
        _require_api_key()
        mode = "live"
        estimate = estimate_live_cost(model_name, len(eval_set.cases))
        cost = f"about ${estimate:.2f}" if estimate is not None else "unknown (no price data)"
        console.print(
            f"Live eval: {len(eval_set.cases)} questions on [bold]{model_name}[/], "
            f"estimated cost {cost} (rough upper estimate)."
        )
        if not yes and not typer.confirm("Continue?", default=False):
            raise typer.Exit(code=0)
    else:
        mode = "oracle" if oracle else choose_offline_mode(loaded, model_name)

    cases, skipped = supported_cases(loaded, eval_set)
    skipped_text = f", {len(skipped)} skipped (need a newer version)" if skipped else ""
    console.print(f"Evaluating {loaded.spec} ({mode}, {len(cases)} cases{skipped_text})")
    try:
        report, rec_path = evaluate(
            loaded, model_name, mode, FIXTURE_DIR, record=record, progress=_print_progress
        )
    except AgentConfigError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc

    md_path, json_path = write_report(report, _path("GARDEN_REPORTS_DIR", "reports"))
    _print_metrics(report)
    console.print(f"Report: {md_path}\n        {json_path}")
    if rec_path:
        console.print(f"Recording saved: {rec_path} (replay with: garden eval {agent})")
    if min_pass_rate is not None and report.metrics.pass_rate < min_pass_rate:
        console.print(
            f"[red]Pass rate {report.metrics.pass_rate:.1%} is below {min_pass_rate:.1%}[/]"
        )
        raise typer.Exit(code=1)


def _report_for(target: str, model: str | None) -> EvalReport:
    """A saved report (.json path) or an agent spec evaluated offline now."""
    if target.endswith(".json"):
        return load_report(Path(target))
    loaded = resolve(_agents_dir(), target)
    model_name = model or loaded.config.model.name
    mode = choose_offline_mode(loaded, model_name)
    report, _ = evaluate(loaded, model_name, mode, FIXTURE_DIR)
    return report


@app.command("compare")
def compare_command(
    a: Annotated[str, typer.Argument(help="energy_qa@0.1.0 or a report .json")],
    b: Annotated[str, typer.Argument(help="energy_qa@0.2.0 or a report .json")],
    model: Annotated[
        str | None, typer.Option(help="Model for agent specs (default: from agent.yaml)")
    ] = None,
) -> None:
    """Compare two agent versions (offline) or two saved reports (e.g. Sonnet vs Haiku runs)."""
    try:
        report_a, report_b = _report_for(a, model), _report_for(b, model)
    except (AgentConfigError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc

    result = compare(report_a, report_b)
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/] {warning}")
    table = Table(title="A vs B")
    table.add_column("metric")
    table.add_column(f"A: {result.a_label}", justify="right")
    table.add_column(f"B: {result.b_label}", justify="right")
    table.add_column("better", justify="center")
    for d in result.metrics:
        table.add_row(d.label, d.a, d.b, d.better.upper() or "=")
    console.print(table)
    console.print(f"Fixed in B: {', '.join(result.fixed) or 'none'}")
    console.print(f"Broken in B: {', '.join(result.broken) or 'none'}")

    out_dir = _path("GARDEN_REPORTS_DIR", "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_b.created_at.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"compare_{stamp}.md"
    out.write_text(comparison_markdown(result))
    console.print(f"Comparison: {out}")


@app.command()
def runs(
    limit: Annotated[int, typer.Option(help="How many recent runs to show")] = 20,
) -> None:
    """Monitoring: recent runs from logs/runs.jsonl with status, latency and cost."""
    log_path = _path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
    recent = load_runs(log_path, limit=max(limit, 1))
    if not recent:
        console.print(f"No runs logged yet in {log_path}.")
        return
    s = summarize_runs(recent)
    console.print(
        f"{s.n_runs} runs · ok {fmt(s.ok_rate, 'pct')} · errors {fmt(s.error_rate, 'pct')} · "
        f"latency p50/p95 {fmt(s.latency_p50_s, 'num')}/{fmt(s.latency_p95_s, 'num')} s · "
        f"cost {fmt(s.total_cost_usd, 'usd')}"
    )
    table = Table()
    for column in ("time (UTC)", "status", "agent", "model", "question", "s", "cost"):
        table.add_column(column)
    for run in recent:
        table.add_row(
            f"{run.started_at:%Y-%m-%d %H:%M}",
            run.status,
            f"{run.agent}@{run.agent_version}",
            run.model,
            run.question[:60],
            f"{run.latency_s:.1f}",
            fmt(run.cost_usd, "usd"),
        )
    console.print(table)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port for the local web server")] = 8000,
) -> None:
    """Start the web app at http://localhost:<port> (needs: uv sync --extra web)."""
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        console.print(
            "[red]The web app needs the optional 'web' extra.[/] Install it with:\n"
            "  uv sync --extra web    (then: uv run --extra web garden ui)"
        )
        raise typer.Exit(code=1)
    import uvicorn

    console.print(f"GridAgent: open http://localhost:{port} in your browser (Ctrl+C to stop)")
    # 127.0.0.1 only: the app can spend API credit, so it must not be reachable
    # from other machines on the network.
    uvicorn.run("gridagent.web.api:app", host="127.0.0.1", port=port, log_level="warning")
