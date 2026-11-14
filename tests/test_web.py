"""Web API tests with FastAPI's TestClient (no server, no network, no model calls).

Skipped when the optional `web` extra is not installed.
"""

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from gridagent import services  # noqa: E402
from gridagent.llm.base import TokenUsage  # noqa: E402
from gridagent.llm.fake import FakeLLM, text, tool_use  # noqa: E402
from gridagent.web.api import Settings, create_app  # noqa: E402

from conftest import FIXTURE_DIR  # noqa: E402

ROOT = Path(__file__).parent.parent
ANSWER = "The average was **161.73 EUR/MWh**. Source: SMARD.de via Energy-Charts.info"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        agents_dir=ROOT / "agents",
        reports_dir=tmp_path / "reports",
        log_path=tmp_path / "logs" / "runs.jsonl",
        cache_dir=tmp_path / "cache",
        fixture_dir=FIXTURE_DIR,
        budget_usd=1.0,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def fake_model(monkeypatch: pytest.MonkeyPatch, cost_tokens: int = 0) -> None:
    """Replace the real model with a scripted one that fetches prices and computes a mean."""
    usage = TokenUsage(input_tokens=cost_tokens)

    def build(cfg, model=None):
        return FakeLLM(
            [
                [tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"})],
                [
                    tool_use(
                        "compute_stats",
                        {
                            "ref": "price_1",
                            "operation": "mean",
                            "source": None,
                            "relative_to": None,
                            "per_day": False,
                        },
                    )
                ],
                [text(ANSWER)],
            ],
            model_name="claude-haiku-5-5",
            usage=usage,
        )

    monkeypatch.setattr(services, "build_llm", build)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_index_and_static_files(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "<title>GridAgent</title>" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_config_lists_agents_newest_first(client: TestClient) -> None:
    config = client.get("/api/config").json()
    specs = [a["spec"] for a in config["agents"]]
    assert specs[:2] == ["energy_qa@0.2.0", "energy_qa@0.1.0"]
    assert config["agents"][0]["model"] == "claude-haiku-5-5"
    assert config["agents"][0]["examples"]  # starter questions from the eval set
    assert config["budget"] == {"limit_usd": 1.0, "spent_usd": 0.0}


def test_ask_requires_api_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    response = client.post("/api/ask", json={"agent": "energy_qa", "question": "Price yesterday?"})
    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_ask_returns_answer_trace_chart_and_logs_it(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch)
    response = client.post(
        "/api/ask", json={"agent": "energy_qa", "question": "Average price yesterday?"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "161.73" in body["run"]["final_answer"]
    assert [row["kind"] for row in body["trace"]] == ["model", "tool", "model", "tool", "model"]
    [chart] = body["charts"]
    assert chart["title"].startswith("Day-ahead price")
    assert len(chart["points"]) == 96 and chart["unit"] == "EUR / MWh"
    assert services.load_runs(settings.log_path)[0].run_id == body["run"]["run_id"]


def test_ask_validates_input(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch)
    assert client.post("/api/ask", json={"agent": "energy_qa", "question": "x"}).status_code == 422
    too_long = {"agent": "energy_qa", "question": "a" * 501}
    assert client.post("/api/ask", json=too_long).status_code == 422
    bad_model = {"agent": "energy_qa", "question": "hello there", "model": "gpt-4"}
    assert client.post("/api/ask", json=bad_model).status_code == 422
    unknown = {"agent": "nope", "question": "hello there"}
    assert client.post("/api/ask", json=unknown).status_code == 404


def test_spending_limit_blocks_live_calls(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch, cost_tokens=4_000_000)  # each run costs $0.40 on Haiku prices x3
    request = {"agent": "energy_qa", "question": "Average price yesterday?"}
    assert client.post("/api/ask", json=request).status_code == 200  # spends $1.20
    blocked = client.post("/api/ask", json=request)
    assert blocked.status_code == 429
    assert "Spending limit" in blocked.json()["detail"]


def test_demo_is_free_and_charts_generation(client: TestClient) -> None:
    body = client.post(
        "/api/demo", json={"agent": "energy_qa", "case_id": "wind_share_last_week"}
    ).json()
    assert body["run"]["status"] == "ok"
    assert "23.3%" in body["run"]["final_answer"]
    assert body["charts"][0]["title"] == "Generation: wind"
    assert len(body["charts"][0]["points"]) <= services.MAX_CHART_POINTS
    assert body["budget"]["spent_usd"] == 0.0


def test_demo_unknown_case(client: TestClient) -> None:
    response = client.post("/api/demo", json={"agent": "energy_qa", "case_id": "nope"})
    assert response.status_code == 404


def test_demo_cases(client: TestClient) -> None:
    cases = client.get("/api/demo-cases", params={"agent": "energy_qa"}).json()
    assert len(cases) == 29 and {"id", "question", "category"} <= set(cases[0])


def test_runs_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch)
    client.post("/api/ask", json={"agent": "energy_qa", "question": "Average price yesterday?"})
    data = client.get("/api/runs").json()
    assert data["summary"]["n_runs"] == 1
    assert data["runs"][0]["status"] == "ok" and data["runs"][0]["trace"]


def test_eval_run_then_reports_and_compare(client: TestClient) -> None:
    first = client.post("/api/evals/run", json={"agent": "energy_qa@0.1.0"}).json()
    second = client.post("/api/evals/run", json={"agent": "energy_qa@0.2.0"}).json()
    assert first["pass_rate"] == 1.0 and first["mode"] == "oracle"
    listed = client.get("/api/reports").json()
    assert {r["name"] for r in listed} == {first["name"], second["name"]}
    full = client.get(f"/api/reports/{first['name']}").json()
    assert len(full["report"]["cases"]) == 29
    comparison = client.get("/api/compare", params={"a": first["name"], "b": second["name"]})
    assert comparison.status_code == 200
    assert comparison.json()["broken"] == []


def test_report_names_cannot_escape_the_reports_folder(client: TestClient) -> None:
    assert client.get("/api/reports/..%2F..%2Fpyproject.toml").status_code in (400, 404)
    assert client.get("/api/compare", params={"a": "../x.json", "b": "y.json"}).status_code == 400


def test_agent_detail(client: TestClient) -> None:
    data = client.get("/api/agents/energy_qa").json()
    assert data["readme"].startswith("# energy_qa")
    assert [v["version"] for v in data["versions"]] == ["0.2.0", "0.1.0"]
    assert "claude-haiku-5-5" in data["versions"][0]["config_yaml"]
    assert client.get("/api/agents/nope").status_code == 404


def test_downsample_keeps_charts_small() -> None:
    from gridagent.data.fixture_source import FixtureDataSource
    from gridagent.data.sources import source_series

    october = FixtureDataSource(FIXTURE_DIR).public_power(date(2026, 10, 1), date(2026, 10, 31))
    points = services.downsample(source_series(october, "solar"))
    assert len(points) <= services.MAX_CHART_POINTS
    assert max(p.v for p in points) > 0
