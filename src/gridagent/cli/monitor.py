"""Daily monitor commands: brief, today, anomalies, site build, forecast-skill, price-backtest."""

import os
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from gridagent.cli.common import FIXTURE_DIR, agents_dir, app, console, db_url, env_path
from gridagent.data.client import berlin_today
from gridagent.services import (
    build_data,
    build_llm,
)


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
    from gridagent.monitor.run import run_briefing
    from gridagent.monitor.writer import NewsStory

    now = date.fromisoformat(today) if today else berlin_today()
    reviewed = date.fromisoformat(day) if day else now - timedelta(days=1)
    data = build_data(fixtures, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    if explain and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[yellow]ANTHROPIC_API_KEY not set: briefing without explanations; "
                      "the story is the template version.[/]")  # fmt: skip
        explain = False
    try:
        with console.status("Building the briefing..."):
            briefing, path = run_briefing(
                data, reviewed, now, agents_dir=agents_dir(),
                reports_dir=env_path("GARDEN_REPORTS_DIR", "reports"),
                log_path=env_path("GARDEN_LOG_DIR", "logs") / "runs.jsonl",
                use_model=explain, max_cost_usd=max_cost, build_llm=build_llm,
            )  # fmt: skip
    except (NoDataError, EnergyChartsError) as exc:
        console.print(f"[red]No briefing for {reviewed}:[/] {exc}")
        raise typer.Exit(code=1) from exc
    if briefing.story is None:  # run_briefing always writes one (model or template)
        raise RuntimeError(f"The briefing for {reviewed} has no story.")
    story = NewsStory(**briefing.story)
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
    if briefing.week:
        days = ", ".join(f"{d['date'][5:]} {d['wind_gwh']} GWh ({d['reliability']})"
                         for d in briefing.week["days"])  # fmt: skip
        console.print(f"[cyan]Week ahead, wind:[/] {days}")
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
    data = build_data(fixtures, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    span = (last - first).days + history_days
    with console.status("Loading daily history..."):
        history = load_history(data, last, days=span)
    day = first
    while day <= last:
        stories = day_stories(data, day, {d: h for d, h in history.items() if d <= day})
        best = stories[0].score if stories else 0
        console.print(f"[bold]{day:%a %d %b %Y}[/]  top score {best:.2f}")
        for story in stories[:top]:
            explained = story.explained_by_fundamentals
            tag = ""
            if explained is not None:
                tag = " [dim](explained)[/]" if explained else " [orange3](unexplained)[/]"
            console.print(f"  {story.score:.2f}  {story.kind:<15} {story.title}{tag}")
        day += timedelta(days=1)


site_app = typer.Typer(help="The public website: static pages from the saved briefings.")
app.add_typer(site_app, name="site")


@site_app.command("build")
def site_build(
    out: Annotated[Path, typer.Option(help="Folder to write the site to.")] = Path("site"),
    ai_story: Annotated[
        bool, typer.Option(help="Show the AI-written story and investigations (labelled).")
    ] = True,
    require_legal_notice: Annotated[
        bool, typer.Option(help="Fail without SITE_LEGAL_NOTICE (for publishing).")
    ] = False,
) -> None:
    """Build the public pages from reports/briefings (static: no server, no database)."""
    from gridagent.site.build import build_site

    legal_notice = os.environ.get("SITE_LEGAL_NOTICE", "").strip() or None
    if require_legal_notice and not legal_notice:
        console.print("[red]SITE_LEGAL_NOTICE is not set:[/] a public site needs a legal "
                      "notice (name, address, e-mail). Not publishing.")  # fmt: skip
        raise typer.Exit(code=1)
    briefings = env_path("GARDEN_REPORTS_DIR", "reports") / "briefings"
    try:
        days = build_site(briefings, out, ai_story=ai_story, legal_notice=legal_notice)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"Built {len(days)} day(s) into {out}/ (latest: {days[0]}; "
                  f"AI story {'on' if ai_story else 'off'}).")  # fmt: skip


@app.command()
def today(
    explain: Annotated[
        bool, typer.Option(help="Model-written story and explanations (~$0.005).")
    ] = True,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the page.")] = True,
) -> None:
    """Your morning in one command: yesterday's briefing, built as a local page and opened."""
    import webbrowser

    from gridagent.site.build import build_site

    brief(day=None, explain=explain, max_cost=0.03, fixtures=False, today=None)
    out = env_path("GARDEN_CACHE_DIR", ".cache") / "site"
    build_site(env_path("GARDEN_REPORTS_DIR", "reports") / "briefings", out, ai_story=explain)
    page = (out / "index.html").resolve()
    console.print(f"Page: {page}")
    if open_browser:
        webbrowser.open(page.as_uri())


@app.command("forecast-skill")
def forecast_skill(
    start: Annotated[str, typer.Option(help="First day (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Last day.")],
    fixtures: Annotated[bool, typer.Option("--fixtures", help="Use recorded data.")] = False,
) -> None:
    """How good is the weather forecast 1..7 days ahead? (correlation with actual output)"""
    from gridagent.monitor.forecast_skill import lead_skill

    data = build_data(fixtures, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    leads = range(1, 2) if fixtures else range(1, 8)
    rows = lead_skill(data, date.fromisoformat(start), date.fromisoformat(end), leads)
    table = Table("forecast", "days ahead", "wind: r", "solar: r", title=f"{start} to {end}")
    for row in rows:
        r = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
        table.add_row(row["forecast"], str(row["lead_days"]), r(row["wind_r"]), r(row["solar_r"]))
    console.print(table)
    console.print("[dim]Daily values: forecast daily mean vs actual daily energy (Energy-Charts). "
                  "Weather: Open-Meteo.com, CC BY 4.0.[/]")  # fmt: skip


@app.command("price-backtest")
def price_backtest(
    start: Annotated[str, typer.Option(help="First day to forecast (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option(help="Last day.")],
) -> None:
    """How good is GridAgent's day-ahead price forecast? (against two simple rules, free)"""
    from gridagent.monitor.price_forecast import backtest, load_history

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    data = build_data(False, FIXTURE_DIR, env_path("GARDEN_CACHE_DIR", ".cache"), db_url())
    with console.status("Loading prices, generation and weather forecasts..."):
        result = backtest(load_history(data, first, last), first, last)
    table = Table(
        "forecast", "error per hour (EUR/MWh)", title=f"{start} to {end}, {result['hours']} hours"
    )
    for name, mae in result["mae_eur_mwh"].items():
        table.add_row(name, f"{mae:.2f}")
    console.print(table)
    skill, r = result["skill_vs_best_baseline_pct"], result["daily_mean_correlation"]
    if skill is None:
        console.print("Not enough data to compare with the simple rules.")
    elif skill > 0:
        console.print(f"Better than the best simple rule by {skill} %; daily averages: r = {r}.")
    else:
        console.print(f"Not better than the best simple rule ({skill} %); daily averages: r = {r}.")
