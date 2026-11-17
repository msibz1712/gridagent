"""Evaluation commands: eval (energy_qa), eval-stories (news_writer), compare."""

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from gridagent.agent.config import AgentConfigError
from gridagent.cli.common import FIXTURE_DIR, agents_dir, app, console, env_path, require_api_key
from gridagent.evals.report import (
    METRIC_ROWS,
    compare,
    comparison_markdown,
    fmt,
    load_report,
    write_report,
)
from gridagent.evals.runner import (
    CaseResult,
    EvalReport,
    Mode,
    estimate_live_cost,
    supported_cases,
)
from gridagent.evals.session import choose_offline_mode, evaluate, load_agent_eval_set
from gridagent.garden.registry import resolve


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
        loaded = resolve(agents_dir(), agent)
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
    mode: Mode
    if live:
        require_api_key()
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

    md_path, json_path = write_report(report, env_path("GARDEN_REPORTS_DIR", "reports"))
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
    loaded = resolve(agents_dir(), target)
    model_name = model or loaded.config.model.name
    mode = choose_offline_mode(loaded, model_name)
    report, _ = evaluate(loaded, model_name, mode, FIXTURE_DIR)
    return report


@app.command("eval-stories")
def eval_stories_command(
    agent: Annotated[str, typer.Argument(help="news_writer or news_writer@0.3.0")] = "news_writer",
    live: Annotated[bool, typer.Option("--live", help="Real model calls (costs money)")] = False,
    record: Annotated[
        bool, typer.Option("--record", help="With --live: save responses for free replay")
    ] = False,
    template: Annotated[
        bool, typer.Option("--template", help="Score the code-only template (the baseline)")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the cost confirmation")] = False,
    min_pass_rate: Annotated[
        float | None, typer.Option(help="Exit with code 1 if pass rate is below this (0-1)")
    ] = None,
    case_ids: Annotated[
        list[str] | None, typer.Option("--case", help="Only this case id (repeatable)")
    ] = None,
) -> None:
    """Evaluate the news writer's stories (replay by default: free)."""
    from gridagent.evals.stories import (
        StoryCaseResult,
        StoryMode,
        load_story_eval_set,
        run_story_eval,
        save_story_report,
    )
    from gridagent.services import build_llm

    loaded = resolve(agents_dir(), agent)
    eval_path = loaded.directory.parent / "evals" / "cases.yaml"
    mode: StoryMode = "live" if live else "template" if template else "replay"
    n_cases = len(case_ids) if case_ids else len(load_story_eval_set(eval_path)[0].cases)
    if live:
        require_api_key()
        # A story costs about $0.003 (one draft); a revision doubles it. Upper end: 2 calls.
        estimate = n_cases * 2 * 0.004
        console.print(f"Live run: {n_cases} cases, at most about ${estimate:.2f}")
        if not yes and not typer.confirm("Continue?"):
            raise typer.Exit(code=1)

    def show(r: StoryCaseResult) -> None:
        mark = "[green]pass[/]" if r.passed else f"[red]fail[/] {', '.join(r.failed)}"
        console.print(f"  {r.id:<28} {mark}")

    console.print(f"Evaluating {loaded.spec} stories ({mode})")
    try:
        report = run_story_eval(
            loaded, eval_path, mode, record=record, only=case_ids,
            build_llm=lambda: build_llm(loaded.config), progress=show,
        )  # fmt: skip
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    path = save_story_report(report, env_path("GARDEN_REPORTS_DIR", "reports"))
    table = Table("check", "pass rate", title=f"{loaded.spec} stories, {mode}")
    for name, rate in report.check_pass_rates.items():
        table.add_row(name, "n/a" if rate is None else f"{100 * rate:.0f} %")
    console.print(table)
    console.print(
        f"Pass rate {100 * report.pass_rate:.1f} % ({sum(c.passed for c in report.cases)}/"
        f"{len(report.cases)}); published by the model {100 * report.published_rate:.0f} %, "
        f"first draft accepted {100 * report.first_draft_rate:.0f} %; "
        f"cost ${report.total_cost_usd:.4f}. Report: {path}"
    )
    if min_pass_rate is not None and report.pass_rate < min_pass_rate:
        console.print(f"[red]Below the required pass rate {min_pass_rate:.0%}[/]")
        raise typer.Exit(code=1)


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

    out_dir = env_path("GARDEN_REPORTS_DIR", "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_b.created_at.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"compare_{stamp}.md"
    out.write_text(comparison_markdown(result))
    console.print(f"Comparison: {out}")
