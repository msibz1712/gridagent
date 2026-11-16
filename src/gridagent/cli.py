"""Command-line interface for the Agent Garden (`garden`).

Commands: `list`, `tools`, `validate`, `new`, `run`, `chat`, `runs`, `eval`, `compare`,
`ui`, `version`, and `db init|status|sync` for the optional database.

Paths are relative to the current directory and can be overridden with
environment variables (useful in Docker):
  GARDEN_AGENTS_DIR (default: agents), GARDEN_TEMPLATE_DIR (default:
  templates/agent_template), GARDEN_LOG_DIR (default: logs),
  GARDEN_CACHE_DIR (default: .cache)
Database (optional, `db` extra): DATABASE_URL (owner: schema + sync),
  DATABASE_URL_READER (read-only role used for answering, if set).
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
from gridagent.agent.conversation import ConversationLimitError
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
from gridagent.services import (
    build_data,
    build_llm,
    load_runs,
    reader_database_url,
    summarize_runs,
)
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
    data = build_data(fixtures, FIXTURE_DIR, _path("GARDEN_CACHE_DIR", ".cache"), _db_url())
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
    case_ids: Annotated[
        list[str] | None, typer.Option("--case", help="Only this case id (repeatable)")
    ] = None,
    categories: Annotated[
        list[str] | None, typer.Option("--category", help="Only this category (repeatable)")
    ] = None,
    max_cost: Annotated[
        float, typer.Option(help="With --live: hard limit in USD; no new case starts above it")
    ] = 0.50,
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

    def only(case) -> bool:
        return (not case_ids or case.id in case_ids) and (
            not categories or case.category in categories
        )

    cases, skipped = supported_cases(loaded, eval_set)
    cases = [c for c in cases if only(c)]
    unknown = sorted(set(case_ids or []) - {c.id for c in eval_set.cases})
    if unknown or not cases:
        console.print(f"[red]No cases selected[/] (unknown ids: {unknown or '-'})")
        raise typer.Exit(code=1)

    model_name = model or loaded.config.model.name
    if live:
        _require_api_key()
        mode = "live"
        turns = sum(1 + len(c.previous) for c in cases)
        estimate = estimate_live_cost(model_name, turns)
        cost = f"about ${estimate:.2f}" if estimate is not None else "unknown (no price data)"
        console.print(
            f"Live eval: {len(cases)} questions ({turns} model turns) on [bold]{model_name}[/]\n"
            f"estimated cost {cost}; hard limit ${max_cost:.2f}"
        )
        if not yes and not typer.confirm("Continue?", default=False):
            raise typer.Exit(code=0)
    else:
        mode = "oracle" if oracle else choose_offline_mode(loaded, model_name)

    skipped_text = f", {len(skipped)} skipped (need a newer version)" if skipped else ""
    console.print(f"Evaluating {loaded.spec} ({mode}, {len(cases)} cases{skipped_text})")
    try:
        report, rec_path = evaluate(
            loaded, model_name, mode, FIXTURE_DIR, record=record, progress=_print_progress,
            only=only, max_cost_usd=max_cost if live else None,
        )  # fmt: skip
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
def chat(
    agent: Annotated[str, typer.Argument(help="Agent, e.g. energy_qa or energy_qa@0.4.0")],
    model: Annotated[str | None, typer.Option(help="Override the model from agent.yaml")] = None,
    fixtures: Annotated[
        bool, typer.Option("--fixtures", help="Use recorded fixture data instead of the live API")
    ] = False,
    today: Annotated[
        str | None, typer.Option(help="Pretend today is YYYY-MM-DD (reproducible runs)")
    ] = None,
) -> None:
    """Have a conversation: follow-up questions build on earlier answers.

    Type /new for a new conversation, exit (or an empty line) to stop.
    """
    _require_api_key()
    try:
        loaded = resolve(_agents_dir(), agent)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load agent '{agent}':[/]\n{exc}")
        raise typer.Exit(code=1) from exc
    data = build_data(fixtures, FIXTURE_DIR, _path("GARDEN_CACHE_DIR", ".cache"), _db_url())
    fixed_today = date.fromisoformat(today) if today else None
    runner = Agent(loaded, build_llm(loaded.config, model), data,
                   today=lambda: fixed_today or berlin_today())  # fmt: skip
    conversation = runner.new_conversation()
    log_path = _path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
    total_cost = 0.0
    console.print(f"Chatting with {loaded.spec}. /new = new conversation, exit = stop.")

    while True:
        try:
            question = console.input("[bold cyan]You:[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in ("", "exit", "quit"):
            break
        if question == "/new":
            conversation = runner.new_conversation()
            console.print("[dim]New conversation started.[/]")
            continue
        try:
            with console.status("Thinking..."):
                log = runner.run(question, conversation)
        except ConversationLimitError as exc:
            console.print(f"[yellow]{exc}[/] Type /new.")
            continue
        append_run_log(log_path, log)
        total_cost += log.cost_usd or 0.0
        style = "green" if log.status == "ok" else "red"
        console.print(
            Panel(log.final_answer, title=f"GridAgent ({log.status})", border_style=style)
        )
        console.print(
            f"[dim]turn {log.turn} · {log.latency_s:.1f}s · {fmt(log.cost_usd, 'usd')} · "
            f"{conversation.turns_left} questions left in this conversation[/]"
        )
    console.print(f"[dim]Session cost {fmt(total_cost, 'usd')}. Runs logged to {log_path}.[/]")


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
        f"cost {fmt(s.total_cost_usd, 'usd')} · "
        f"answers with unbacked numbers {fmt(s.unsupported_rate, 'pct')}"
    )
    table = Table()
    for column in ("time (UTC)", "status", "agent", "model", "question", "s", "cost", "unbacked"):
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
            "-"
            if run.unsupported_numbers is None
            else ", ".join(run.unsupported_numbers) or "none",
        )
    console.print(table)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port for the local web server")] = 8000,
    host: Annotated[
        str,
        typer.Option(
            help="Interface to listen on. Keep 127.0.0.1 on your machine; use 0.0.0.0 only "
            "inside a container whose port is published to 127.0.0.1 on the host."
        ),
    ] = "127.0.0.1",
) -> None:
    """Start the web app at http://localhost:<port> (needs: uv sync --extra web)."""
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("uvicorn") is None:
        console.print(
            "[red]The web app needs the optional 'web' extra.[/] Install it with:\n"
            "  uv sync --extra web    (then: uv run --extra web garden ui)"
        )
        raise typer.Exit(code=1)
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        # The app can spend API credit: only expose it where the network is controlled.
        console.print(
            f"[yellow]Listening on {host}: reachable from other machines unless a container "
            "or firewall restricts it.[/]"
        )
    console.print(f"GridAgent: open http://localhost:{port} in your browser (Ctrl+C to stop)")
    uvicorn.run("gridagent.web.api:app", host=host, port=port, log_level="warning")


# ---------- database (optional `db` extra) ----------

db_app = typer.Typer(help="Optional PostgreSQL store (e.g. Supabase) in front of the APIs.")
app.add_typer(db_app, name="db")


def _db_url() -> str | None:
    return reader_database_url()


def _owner_connection():
    if importlib.util.find_spec("psycopg") is None:
        console.print("[red]Needs the optional 'db' extra:[/] uv sync --extra db")
        raise typer.Exit(code=1)
    url = os.environ.get("DATABASE_URL")
    if not url:
        console.print("[red]DATABASE_URL is not set.[/] Put it in .env (never commit it).")
        raise typer.Exit(code=1)
    from gridagent.db.store import connect

    return connect(url)


@db_app.command("init")
def db_init() -> None:
    """Create the `energy` schema and tables (safe to run again)."""
    from gridagent.db.store import init_schema

    with _owner_connection() as conn:
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
    with _owner_connection() as conn:
        init_schema(conn)
        create_reader(conn, password)
    set_env_line(env_file, "DATABASE_URL_READER", reader_url(os.environ["DATABASE_URL"], password))
    console.print(f"Read-only role ready; DATABASE_URL_READER written to {env_file}.")


@db_app.command("status")
def db_status() -> None:
    """Rows, size and synced date ranges per table."""
    from gridagent.db.store import database_bytes, init_schema, register_exports, status

    with _owner_connection() as conn:
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
    with _owner_connection() as conn:
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
            conn, LiveDataSource(_path("GARDEN_CACHE_DIR", ".cache")), start, end,
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
    return _path("GARDEN_CACHE_DIR", ".cache") / "mastr" / "register.json.gz"


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
    with _owner_connection() as conn:
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


@app.command()
def brief(
    day: Annotated[str | None, typer.Option(help="Day to review (default: yesterday).")] = None,
    explain: Annotated[
        bool, typer.Option(help="Let the model explain the biggest price events (costs ~$0.003).")
    ] = True,
    max_cost: Annotated[float, typer.Option(help="Limit for the explanations, USD.")] = 0.02,
    fixtures: Annotated[bool, typer.Option("--fixtures", help="Use recorded data.")] = False,
    today: Annotated[str | None, typer.Option(help="Pretend today is YYYY-MM-DD.")] = None,
) -> None:
    """Daily briefing: key figures, notable events and the plan for days with known prices."""
    from datetime import timedelta

    from gridagent.data.client import EnergyChartsError, NoDataError
    from gridagent.monitor.daily import build_briefing
    from gridagent.monitor.explain import explain_events, save_briefing

    now = date.fromisoformat(today) if today else berlin_today()
    reviewed = date.fromisoformat(day) if day else now - timedelta(days=1)
    data = build_data(fixtures, FIXTURE_DIR, _path("GARDEN_CACHE_DIR", ".cache"), _db_url())
    try:
        briefing = build_briefing(data, reviewed, now)
    except (NoDataError, EnergyChartsError) as exc:
        console.print(f"[red]No briefing for {reviewed}:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if explain and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[yellow]ANTHROPIC_API_KEY not set: briefing without explanations; "
                      "the story is the template version.[/]")  # fmt: skip
        explain = False
    if explain:
        loaded = resolve(_agents_dir(), "energy_qa")
        log_path = _path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
        with console.status("Explaining the biggest price events..."):
            explain_events(briefing, loaded, lambda: build_llm(loaded.config), data, now,
                           max_cost, on_run=lambda log: append_run_log(log_path, log))  # fmt: skip

    from gridagent.monitor.writer import template_story, write_story

    if explain:
        writer = resolve(_agents_dir(), "news_writer")
        with console.status("Writing the story..."):
            story = write_story(briefing, writer, build_llm(writer.config))
        briefing.cost_usd = round(briefing.cost_usd + story.cost_usd, 4)
    else:
        story = template_story(briefing)
    briefing.story = story.model_dump()

    path = save_briefing(_path("GARDEN_REPORTS_DIR", "reports") / "briefings", briefing)
    by = "the model" if story.written_by == "model" else "the template"
    console.print(Panel("\n\n".join([story.standfirst, *story.paragraphs]),
                        title=story.headline, subtitle=f"written by {by}"))  # fmt: skip
    for problem in story.problems:
        console.print(f"[yellow]Model draft rejected:[/] {problem}")
    table = Table("figure", "value", "4-week normal", title=f"Briefing for {reviewed}")
    for f in briefing.figures:
        change = f" ({f.change_pct:+.0f} %)" if f.change_pct is not None else ""
        at = f" at {f.at}" if f.at else ""
        table.add_row(f.name, f"{f.value} {f.unit}{at}{change}", f"{f.normal}")
    console.print(table)
    for event in briefing.events:
        console.print(f"[bold]• {event.title}[/]")
        if event.explanation:
            console.print(Panel(event.explanation, border_style="dim"))
    for expected in briefing.outlooks:
        console.print(f"[cyan]{expected.day} forecast:[/] {expected.headline}; peak residual "
                      f"load {expected.peak_residual_load_gw} GW")  # fmt: skip
    for plan in briefing.plans:
        console.print(f"[cyan]{plan.day}:[/] cheapest {plan.cheapest_hours} "
                      f"({plan.cheapest_price} EUR/MWh), dearest {plan.dearest_hours} "
                      f"({plan.dearest_price}); 1 MW / 2 MWh battery: "
                      f"{plan.battery_profit_eur_per_mw} EUR")  # fmt: skip
    for missing in briefing.not_available:
        console.print(f"[yellow]Not available:[/] {missing}")
    console.print(f"[dim]Saved to {path} (model cost: ${briefing.cost_usd:.4f})[/]")


@app.command()
def anomalies(
    start: Annotated[str, typer.Option(help="First day to check (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Last day to check.")],
    top: Annotated[int, typer.Option(help="Stories shown per day.")] = 2,
    history_days: Annotated[int, typer.Option(help="Days of history for records.")] = 730,
    fixtures: Annotated[bool, typer.Option("--fixtures", help="Use recorded data.")] = False,
) -> None:
    """Backtest the anomaly detector: the best stories of every day in a period (free)."""
    from datetime import timedelta

    from gridagent.monitor.anomalies import day_stories, load_history

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    data = build_data(fixtures, FIXTURE_DIR, _path("GARDEN_CACHE_DIR", ".cache"), _db_url())
    span = (last - first).days + history_days
    with console.status("Loading daily history..."):
        history = load_history(data, last, days=span)
    day = first
    while day <= last:
        stories = day_stories(data, day, {d: h for d, h in history.items() if d <= day})
        best = stories[0].score if stories else 0
        console.print(f"[bold]{day:%a %d %b %Y}[/]  top score {best:.2f}")
        for story in stories[:top]:
            tag = {True: " [dim](explained)[/]", False: " [orange3](unexplained)[/]"}.get(
                story.explained_by_fundamentals, ""
            )
            console.print(f"  {story.score:.2f}  {story.kind:<15} {story.title}{tag}")
        day += timedelta(days=1)
