"""GridAgent website: a FastAPI backend plus a static single-page front end.

The website is for people who want answers about Germany's electricity
system (researchers, consultants, analysts). It deliberately shows no agent,
model, token or evaluation details; those live in the `garden` CLI.

    GET  /api/health   liveness check (for Docker / monitoring)
    GET  /api/config   whether the service is available + example questions
    POST /api/ask      answer a question (optionally a follow-up in a conversation)
    DELETE /api/conversations/{id}   forget a conversation now

Configuration (environment variables):
    GARDEN_WEB_AGENT        agent spec to serve (default: energy_qa = latest version)
    GARDEN_WEB_FIXTURES     "1" to use recorded data instead of the live API
    GARDEN_UI_BUDGET_USD    spending limit for model calls per server process (default 1.00)
    GARDEN_AGENTS_DIR, GARDEN_LOG_DIR, GARDEN_CACHE_DIR   as for the CLI
"""

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gridagent import __version__, services
from gridagent.agent.config import AgentConfigError, LoadedAgent
from gridagent.agent.conversation import ConversationLimitError, ConversationStore
from gridagent.agent.runlog import RunLog
from gridagent.data.client import EnergyChartsError, NoDataError, berlin_today
from gridagent.evals.runner import estimate_live_cost
from gridagent.evals.session import load_agent_eval_set
from gridagent.garden.registry import resolve
from gridagent.monitor.run import run_briefing
from gridagent.site.build import build_site
from gridagent.tools.toolbox import ToolBox

STATIC_DIR = Path(__file__).parent / "static"
AUTO_BRIEFING_EVERY_SECONDS = 30 * 60
DAILY_AI_WORST_CASE_USD = 0.05  # explanations (limited to 0.03) plus the story
WAIT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10"><title>GridAgent: updating</title>
<link rel="stylesheet" href="/dashboard/style.css"></head>
<body><main class="wrap hero"><p class="eyebrow">GridAgent</p><h1>Updating the briefing.</h1>
<p class="lead">This takes up to two minutes. The page reloads by itself and shows the new
briefing when it is ready.</p></main></body></html>"""

EXAMPLES = [
    "Why were prices so high last Thursday evening?",
    "What could a 10 MW / 20 MWh battery have earned last week?",
    "How much onshore wind was added in NRW this year?",
    "What share of electricity came from wind last week?",
]
FAILED_ANSWER = (
    "Sorry, I couldn't answer this question right now. Please try again in a moment, "
    "or rephrase the question."
)


@dataclass
class Settings:
    """Where things live. Defaults match the CLI; override with environment variables."""

    agent: str = field(default_factory=lambda: os.environ.get("GARDEN_WEB_AGENT", "energy_qa"))
    use_fixtures: bool = field(
        default_factory=lambda: os.environ.get("GARDEN_WEB_FIXTURES", "0") == "1"
    )
    agents_dir: Path = field(default_factory=lambda: _env("GARDEN_AGENTS_DIR", "agents"))
    log_path: Path = field(default_factory=lambda: _env("GARDEN_LOG_DIR", "logs") / "runs.jsonl")
    cache_dir: Path = field(default_factory=lambda: _env("GARDEN_CACHE_DIR", ".cache"))
    fixture_dir: Path = field(default_factory=lambda: Path("tests/fixtures"))
    budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("GARDEN_UI_BUDGET_USD", "1.00"))
    )
    max_turns: int = 10  # questions per conversation
    reports_dir: Path = field(default_factory=lambda: _env("GARDEN_REPORTS_DIR", "reports"))
    # Daily briefing in the background (live data only): every 30 minutes, check whether
    # yesterday's briefing exists, and build it if not. With a key and GARDEN_DAILY_AI=1
    # the model writes the story (~$0.005 a day, counted against the budget).
    auto_briefing: bool = field(
        default_factory=lambda: os.environ.get("GARDEN_AUTO_BRIEFING", "1") == "1"
    )
    daily_ai: bool = field(default_factory=lambda: os.environ.get("GARDEN_DAILY_AI", "1") == "1")


def _env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    # Continue this conversation; null (or an expired id) starts a new one.
    conversation_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    budget = services.Budget(limit_usd=settings.budget_usd)
    conversations = ConversationStore(max_turns=settings.max_turns)
    data = services.build_data(
        settings.use_fixtures, settings.fixture_dir, settings.cache_dir,
        services.reader_database_url(),
    )  # fmt: skip
    app = FastAPI(title="GridAgent", version=__version__, docs_url="/api/docs")

    def load_agent() -> LoadedAgent:
        try:
            return resolve(settings.agents_dir, settings.agent)
        except AgentConfigError as exc:
            raise HTTPException(status_code=503, detail="The service is not configured.") from exc

    def today_for(agent: LoadedAgent):
        """Real today for live data; the recording date when serving recorded data."""
        if not settings.use_fixtures:
            return berlin_today()
        try:
            return load_agent_eval_set(agent)[0].today
        except AgentConfigError:
            return berlin_today()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        agent = load_agent()
        return {
            "version": __version__,
            "available": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "today": str(today_for(agent)),
            "recorded_data": settings.use_fixtures,
            "examples": EXAMPLES,
        }

    @app.post("/api/ask")
    def ask(request: AskRequest) -> dict[str, Any]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(status_code=503, detail="The assistant is not available right now.")
        agent = load_agent()
        worst_case = estimate_live_cost(agent.config.model.name, 1) or 0.05
        if not budget.allows_another_call(worst_case):
            raise HTTPException(
                status_code=429,
                detail="The service has reached its usage limit for now. Please try again later.",
            )
        conversation = conversations.get(request.conversation_id)
        new_conversation = conversation is None
        if conversation is None:
            today = today_for(agent)
            toolbox = ToolBox(data, today=lambda: today, enabled=agent.config.tools)
            conversation = conversations.start(toolbox, today)
        try:
            conversation.check_capacity()
        except ConversationLimitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        log = services.ask(agent, request.question, None, data, conversation.today,
                           settings.log_path, conversation)  # fmt: skip
        budget.add(log.cost_usd)
        payload = answer_payload(log, data, conversation.toolbox)
        return payload | {
            "conversation_id": conversation.id,
            "turn": log.turn,
            "turns_left": conversation.turns_left,
            # True if the client sent an id that had expired: the UI can say so.
            "new_conversation": new_conversation and request.conversation_id is not None,
        }

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def end_conversation(conversation_id: str) -> None:
        """Forget a conversation now (the "New conversation" button), not after 30 minutes."""
        conversations.delete(conversation_id)

    # ---------- the dashboard: the briefing as pages, rebuilt after every run ----------

    site_dir = settings.cache_dir / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    refreshing = threading.Lock()

    def refresh(reviewed: date | None = None) -> str:
        """Build (or rebuild) a briefing and the dashboard. Returns what happened."""
        if not refreshing.acquire(blocking=False):
            return "A briefing is already being built."
        try:
            today = berlin_today()
            day = reviewed or today - timedelta(days=1)
            use_model = (settings.daily_ai and bool(os.environ.get("ANTHROPIC_API_KEY"))
                         and budget.allows_another_call(DAILY_AI_WORST_CASE_USD))  # fmt: skip
            briefing, _ = run_briefing(
                data, day, today, agents_dir=settings.agents_dir,
                reports_dir=settings.reports_dir, log_path=settings.log_path,
                use_model=use_model, max_cost_usd=0.03, build_llm=services.build_llm,
            )  # fmt: skip
            budget.add(briefing.cost_usd)
            build_site(settings.reports_dir / "briefings", site_dir, ai_story=True, app=True)
            return f"Briefing for {day} ready."
        except (NoDataError, EnergyChartsError, FileNotFoundError) as exc:
            return f"No briefing yet: {exc}"
        finally:
            refreshing.release()

    def rebuild_pages() -> None:
        try:
            build_site(settings.reports_dir / "briefings", site_dir, ai_story=True, app=True)
        except FileNotFoundError:
            pass  # no briefing yet

    def daily_loop() -> None:
        while True:
            yesterday = berlin_today() - timedelta(days=1)
            if not (settings.reports_dir / "briefings" / f"{yesterday}.json").is_file():
                refresh(yesterday)
            time.sleep(AUTO_BRIEFING_EVERY_SECONDS)

    rebuild_pages()  # pages for briefings made elsewhere (e.g. `garden brief`)
    if settings.auto_briefing and not settings.use_fixtures:
        threading.Thread(target=daily_loop, name="daily-briefing", daemon=True).start()
    app.state.refresh = refresh  # for tests

    @app.post("/refresh", include_in_schema=False)
    def refresh_now() -> RedirectResponse:
        """Start a briefing in the background (it can take a minute or two) and show a page
        that reloads itself until it is done: no waiting on a hanging button, no JavaScript."""

        def run() -> None:
            app.state.last_refresh = app.state.refresh()

        if not refreshing.locked():
            app.state.refresh_thread = threading.Thread(target=run, daemon=True)
            app.state.refresh_thread.start()
        return RedirectResponse("/refresh/wait", status_code=303)

    @app.get("/refresh/wait", include_in_schema=False, response_model=None)
    def refresh_wait() -> HTMLResponse | RedirectResponse:
        thread = getattr(app.state, "refresh_thread", None)
        if thread is None or not thread.is_alive():
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(WAIT_PAGE)

    app.mount("/dashboard", StaticFiles(directory=site_dir, html=True), name="dashboard")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        """The dashboard when a briefing exists, otherwise the chat."""
        return RedirectResponse("/dashboard/" if (site_dir / "index.html").is_file() else "/ask")

    @app.get("/ask", include_in_schema=False)
    def chat() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def answer_payload(
    log: RunLog, data: services.DataProvider, toolbox: ToolBox | None = None
) -> dict[str, Any]:
    """What an end user sees: no model, token, cost or tool vocabulary."""
    if log.status == "ok":
        status, answer = "answered", log.final_answer
    elif log.status == "refused":
        status, answer = "declined", log.final_answer
    else:  # internal details stay in the run log, not on the website
        status, answer = "failed", FAILED_ANSWER
    failed = status == "failed"
    return {
        "id": log.run_id,
        "question": log.question,
        "answer": answer,
        "status": status,
        "asked_at": datetime.now(UTC).isoformat(),
        "steps": [] if failed else services.method_steps(log, toolbox),
        "sources": [] if failed else services.sources_for_run(log),
        "charts": []
        if failed
        else [c.model_dump() for c in services.charts_for_run(log, data, toolbox)],
    }


app = create_app()  # used by `uvicorn gridagent.web.api:app`
