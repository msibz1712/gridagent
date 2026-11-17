"""Agent commands: list, tools, validate, new, run, chat, runs, ui, version."""

import importlib.util
import json
from datetime import date
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from gridagent import __version__
from gridagent.agent.config import AgentConfigError
from gridagent.agent.conversation import ConversationLimitError
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.cli.common import (
    FIXTURE_DIR,
    agents_dir,
    app,
    console,
    db_url,
    env_path,
    require_api_key,
)
from gridagent.data.client import berlin_today
from gridagent.evals.report import (
    fmt,
)
from gridagent.garden.registry import discover, resolve
from gridagent.garden.scaffold import create_agent
from gridagent.services import (
    build_data,
    build_llm,
    load_runs,
    summarize_runs,
)
from gridagent.tools.definitions import ALL_TOOLS


@app.command()
def version() -> None:
    """Print the installed gridagent version."""
    console.print(f"gridagent [bold green]{__version__}[/]")


@app.command("list")
def list_agents() -> None:
    """Show registered agents, their versions and whether each config is valid."""
    entries = discover(agents_dir())
    if not entries:
        console.print(f"No agents in {agents_dir()}. Create one with: garden new <name>")
        return
    table = Table(title=f"Agent Garden ({agents_dir()})")
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
    entries = [e for e in discover(agents_dir()) if agent is None or e.name == agent]
    if agent and not entries:
        console.print(f"[red]No agent '{agent}' in {agents_dir()}[/]")
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
    template_dir = env_path("GARDEN_TEMPLATE_DIR", "templates/agent_template")
    try:
        created = create_agent(agents_dir(), template_dir, name, owner, description)
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
    require_api_key()

    try:
        loaded = resolve(agents_dir(), agent)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load agent '{agent}':[/]\n{exc}")
        raise typer.Exit(code=1) from exc

    cfg = loaded.config
    llm = build_llm(cfg, model)
    data = build_data(fixtures, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    fixed_today = date.fromisoformat(today) if today else None

    with console.status(f"Running {cfg.name} {cfg.version} on {llm.model_name}..."):
        log = Agent(loaded, llm, data, today=lambda: fixed_today or berlin_today()).run(question)

    log_path = env_path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
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
    require_api_key()
    try:
        loaded = resolve(agents_dir(), agent)
    except AgentConfigError as exc:
        console.print(f"[red]Could not load agent '{agent}':[/]\n{exc}")
        raise typer.Exit(code=1) from exc
    data = build_data(fixtures, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    fixed_today = date.fromisoformat(today) if today else None
    runner = Agent(loaded, build_llm(loaded.config, model), data,
                   today=lambda: fixed_today or berlin_today())  # fmt: skip
    conversation = runner.new_conversation()
    log_path = env_path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
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
    log_path = env_path("GARDEN_LOG_DIR", "logs") / "runs.jsonl"
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
