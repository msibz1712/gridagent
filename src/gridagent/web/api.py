"""GridAgent website: a FastAPI backend plus a static single-page front end.

The website is for people who want answers about Germany's electricity
system (researchers, consultants, analysts). It deliberately shows no agent,
model, token or evaluation details; those live in the `garden` CLI.

    GET  /api/health   liveness check (for Docker / monitoring)
    GET  /api/config   whether the service is available + example questions
    POST /api/ask      answer a question: answer, method, sources, charts

Configuration (environment variables):
    GARDEN_WEB_AGENT        agent spec to serve (default: energy_qa = latest version)
    GARDEN_WEB_FIXTURES     "1" to use recorded data instead of the live API
    GARDEN_UI_BUDGET_USD    spending limit for model calls per server process (default 1.00)
    GARDEN_AGENTS_DIR, GARDEN_LOG_DIR, GARDEN_CACHE_DIR   as for the CLI
"""

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from gridagent import __version__, services
from gridagent.agent.config import AgentConfigError, LoadedAgent
from gridagent.agent.runlog import RunLog
from gridagent.data.client import berlin_today
from gridagent.evals.runner import estimate_live_cost
from gridagent.evals.session import load_agent_eval_set
from gridagent.garden.registry import resolve

STATIC_DIR = Path(__file__).parent / "static"

EXAMPLES = [
    "What share of Germany's electricity came from wind last week?",
    "What was the market value of solar power last month, compared with the official value?",
    "How much electricity did Germany import on balance last week?",
    "Which control area produced the most onshore wind last week?",
    "How much solar capacity is installed, and what is the 2030 target?",
    "For how many hours were electricity prices negative last month?",
    "What was the CO2 intensity of German electricity in 2025?",
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


def _env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    budget = services.Budget(limit_usd=settings.budget_usd)
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
        data = services.build_data(settings.use_fixtures, settings.fixture_dir, settings.cache_dir)
        log = services.ask(agent, request.question, None, data, today_for(agent), settings.log_path)
        budget.add(log.cost_usd)
        return answer_payload(log, data)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def answer_payload(log: RunLog, data: services.DataProvider) -> dict[str, Any]:
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
        "steps": [] if failed else services.method_steps(log),
        "sources": [] if failed else services.sources_for_run(log),
        "charts": [] if failed else [c.model_dump() for c in services.charts_for_run(log, data)],
    }


app = create_app()  # used by `uvicorn gridagent.web.api:app`
