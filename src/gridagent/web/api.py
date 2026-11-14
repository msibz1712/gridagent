"""GridAgent web API (FastAPI) and static front end.

Start it with `garden ui` (listens on localhost only). The API is a thin layer:
each endpoint validates the request with pydantic and calls `gridagent.services`.

    GET  /api/health            liveness check (for Docker / monitoring)
    GET  /api/config            agents, models, key status, budget, example questions
    POST /api/ask               live question to the real model (costs money)
    POST /api/demo              free demo: the scripted oracle answers an eval question
    GET  /api/runs              run history + summary metrics
    GET  /api/reports           eval reports (summaries)
    GET  /api/reports/{name}    one full report
    GET  /api/compare?a=&b=     compare two reports
    POST /api/evals/run         run a free offline eval and save the report
    GET  /api/agents/{name}     README, versions, config and prompt of an agent
"""

import os
from dataclasses import dataclass, field
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
from gridagent.evals.report import METRIC_ROWS, compare, fmt, write_report
from gridagent.evals.runner import EvalReport, estimate_live_cost
from gridagent.evals.session import choose_offline_mode, evaluate, load_agent_eval_set
from gridagent.garden.registry import discover, resolve

STATIC_DIR = Path(__file__).parent / "static"
MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5"]


@dataclass
class Settings:
    """Where things live. Defaults match the CLI; override with GARDEN_* env vars."""

    agents_dir: Path = field(default_factory=lambda: _env("GARDEN_AGENTS_DIR", "agents"))
    reports_dir: Path = field(default_factory=lambda: _env("GARDEN_REPORTS_DIR", "reports"))
    log_path: Path = field(default_factory=lambda: _env("GARDEN_LOG_DIR", "logs") / "runs.jsonl")
    cache_dir: Path = field(
        default_factory=lambda: _env("GARDEN_CACHE_DIR", ".cache/energy_charts")
    )
    fixture_dir: Path = field(default_factory=lambda: Path("tests/fixtures"))
    budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("GARDEN_UI_BUDGET_USD", "1.00"))
    )


def _env(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


# ---------- request bodies ----------


class AskRequest(BaseModel):
    agent: str
    question: str = Field(min_length=3, max_length=500)
    model: str | None = Field(None, pattern=r"^claude-[a-z0-9-]+$")
    use_fixtures: bool = True


class DemoRequest(BaseModel):
    agent: str
    case_id: str


class EvalRequest(BaseModel):
    agent: str
    model: str | None = Field(None, pattern=r"^claude-[a-z0-9-]+$")


# ---------- app ----------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    budget = services.Budget(limit_usd=settings.budget_usd)
    app = FastAPI(title="GridAgent", version=__version__, docs_url="/api/docs")

    def load(spec: str) -> LoadedAgent:
        try:
            return resolve(settings.agents_dir, spec)
        except AgentConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def fixture_today(agent: LoadedAgent):
        try:
            return load_agent_eval_set(agent)[0].today
        except AgentConfigError:
            return berlin_today()

    def run_payload(log: RunLog, data: services.DataProvider | None) -> dict[str, Any]:
        charts = services.charts_for_run(log, data) if data is not None else []
        return {
            "run": log.model_dump(mode="json"),
            "trace": services.trace_rows(log),
            "charts": [c.model_dump() for c in charts],
            "budget": {"limit_usd": budget.limit_usd, "spent_usd": budget.spent_usd},
        }

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        agents = []
        for entry in discover(settings.agents_dir):
            for version in reversed(entry.versions):
                if version.agent is None:
                    continue
                cfg = version.agent.config
                agents.append({
                    "spec": version.agent.spec, "name": cfg.name, "version": cfg.version,
                    "model": cfg.model.name, "description": cfg.description,
                    "owner": cfg.owner, "tools": cfg.tools,
                    "fixture_today": str(fixture_today(version.agent)),
                    "examples": _examples(version.agent),
                })  # fmt: skip
        return {
            "version": __version__,
            "agents": agents,
            "models": [
                {"id": m, "est_cost_per_question_usd": estimate_live_cost(m, 1)} for m in MODELS
            ],
            "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "budget": {"limit_usd": budget.limit_usd, "spent_usd": budget.spent_usd},
            "today": str(berlin_today()),
        }

    @app.post("/api/ask")
    def ask(request: AskRequest) -> dict[str, Any]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise HTTPException(
                status_code=400, detail="ANTHROPIC_API_KEY is not set on the server."
            )
        agent = load(request.agent)
        model = request.model or agent.config.model.name
        worst_case = estimate_live_cost(model, 1) or 0.05
        if not budget.allows_another_call(worst_case):
            raise HTTPException(
                status_code=429,
                detail=f"Spending limit of ${budget.limit_usd:.2f} reached for this server "
                "session. Restart it or raise GARDEN_UI_BUDGET_USD.",
            )
        data = services.build_data(request.use_fixtures, settings.fixture_dir, settings.cache_dir)
        today = fixture_today(agent) if request.use_fixtures else berlin_today()
        log = services.ask(agent, request.question, model, data, today, settings.log_path)
        budget.add(log.cost_usd)
        return run_payload(log, data)

    @app.post("/api/demo")
    def demo(request: DemoRequest) -> dict[str, Any]:
        agent = load(request.agent)
        try:
            eval_set, _ = load_agent_eval_set(agent)
        except AgentConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        case = next((c for c in eval_set.cases if c.id == request.case_id), None)
        if case is None:
            raise HTTPException(status_code=404, detail=f"No eval case '{request.case_id}'.")
        log = services.oracle_demo(agent, case, settings.fixture_dir, eval_set.today)
        data = services.build_data(True, settings.fixture_dir, settings.cache_dir)
        return run_payload(log, data)

    @app.get("/api/demo-cases")
    def demo_cases(agent: str) -> list[dict[str, str]]:
        try:
            eval_set, _ = load_agent_eval_set(load(agent))
        except AgentConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [{"id": c.id, "question": c.question, "category": c.category}
                for c in eval_set.cases]  # fmt: skip

    @app.get("/api/runs")
    def runs(limit: int = 200) -> dict[str, Any]:
        logs = services.load_runs(settings.log_path, limit=min(max(limit, 1), 1000))
        return {
            "summary": services.summarize_runs(logs).model_dump(),
            "runs": [
                {**services.run_row(r), "run": r.model_dump(mode="json"),
                 "trace": services.trace_rows(r)}
                for r in logs
            ],
        }  # fmt: skip

    @app.get("/api/reports")
    def reports() -> list[dict[str, Any]]:
        loaded = services.load_reports(services.list_reports(settings.reports_dir))
        return [_report_summary(name, report) for name, report in loaded.items()]

    @app.get("/api/reports/{name}")
    def report(name: str) -> dict[str, Any]:
        report = _find_report(settings, name)
        return {**_report_summary(name, report), "report": report.model_dump(mode="json")}

    @app.get("/api/compare")
    def compare_reports(a: str, b: str) -> dict[str, Any]:
        return compare(_find_report(settings, a), _find_report(settings, b)).model_dump()

    @app.post("/api/evals/run")
    def run_eval(request: EvalRequest) -> dict[str, Any]:
        agent = load(request.agent)
        model = request.model or agent.config.model.name
        mode = choose_offline_mode(agent, model)
        try:
            result, _ = evaluate(agent, model, mode, settings.fixture_dir)
        except AgentConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _, json_path = write_report(result, settings.reports_dir)
        return _report_summary(json_path.name, result)

    @app.get("/api/agents/{name}")
    def agent_detail(name: str) -> dict[str, Any]:
        entry = next((e for e in discover(settings.agents_dir) if e.name == name), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"No agent '{name}'.")
        readme = entry.directory / "README.md"
        versions = []
        for v in reversed(entry.versions):
            item: dict[str, Any] = {"version": v.version, "valid": v.agent is not None,
                                    "error": v.error}  # fmt: skip
            if v.agent is not None:
                item["config_yaml"] = (v.agent.directory / "agent.yaml").read_text()
                item["system_prompt"] = v.agent.system_prompt
                item["model"] = v.agent.config.model.name
            versions.append(item)
        return {
            "name": name,
            "readme": readme.read_text() if readme.is_file() else "",
            "versions": versions,
        }

    # ---------- front end ----------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _examples(agent: LoadedAgent, limit: int = 6) -> list[str]:
    """A few answerable questions from the eval set, as starter chips in the UI."""
    try:
        eval_set, _ = load_agent_eval_set(agent)
    except AgentConfigError:
        return []
    return [c.question for c in eval_set.cases if c.outcome == "answer"][:limit]


def _report_summary(name: str, report: EvalReport) -> dict[str, Any]:
    m = report.metrics
    return {
        "name": name,
        "agent": f"{report.agent}@{report.agent_version}",
        "model": report.model,
        "mode": report.mode,
        "created_at": report.created_at.isoformat(),
        "n_cases": m.n_cases,
        "pass_rate": m.pass_rate,
        "metrics": [
            {
                "key": key,
                "label": label,
                "value": getattr(m, key),
                "display": fmt(getattr(m, key), kind),
            }
            for key, label, kind in METRIC_ROWS
        ],
        "notes": report.notes,
    }


def _find_report(settings: Settings, name: str) -> EvalReport:
    path = (settings.reports_dir / name).resolve()
    # Only files directly inside the reports folder: no path traversal via `name`.
    if path.parent != settings.reports_dir.resolve() or path.suffix != ".json":
        raise HTTPException(status_code=400, detail="Invalid report name.")
    reports = services.load_reports([path]) if path.is_file() else {}
    if not reports:
        raise HTTPException(status_code=404, detail=f"No report '{name}'.")
    return reports[path.name]


app = create_app()  # used by `uvicorn gridagent.web.api:app`
