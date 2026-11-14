"""Website API tests with FastAPI's TestClient (no server, no network, no model calls).

Skipped when the optional `web` extra is not installed.
"""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from gridagent import services  # noqa: E402
from gridagent.llm.base import LLMError, TokenUsage  # noqa: E402
from gridagent.llm.fake import FakeLLM, text, tool_use  # noqa: E402
from gridagent.web.api import FAILED_ANSWER, Settings, create_app  # noqa: E402

from conftest import FIXTURE_DIR  # noqa: E402

ROOT = Path(__file__).parent.parent
ANSWER = "The average was **161.73 EUR/MWh** on 13 Nov 2026. Source: SMARD.de via Energy-Charts"
JARGON = ("model", "token", "tool", "cost", "agent", "oracle", "eval", "stop_reason", "usd")
STATS = {"ref": "price_1", "operation": "mean", "source": None, "relative_to": None,
         "per_day": False}  # fmt: skip
PRICE_SCRIPT = [
    [tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"})],
    [tool_use("compute_stats", STATS)],
    [text(ANSWER)],
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        agent="energy_qa",
        use_fixtures=True,
        agents_dir=ROOT / "agents",
        log_path=tmp_path / "logs" / "runs.jsonl",
        cache_dir=tmp_path / "cache",
        fixture_dir=FIXTURE_DIR,
        budget_usd=1.0,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def fake_model(monkeypatch: pytest.MonkeyPatch, turns=None, input_tokens: int = 0) -> None:
    """Replace the real model with a scripted one (default: price mean for yesterday)."""
    script = turns or PRICE_SCRIPT
    usage = TokenUsage(input_tokens=input_tokens)

    def build(cfg, model=None):
        return FakeLLM(list(script), model_name="claude-haiku-5-5", usage=usage)

    monkeypatch.setattr(services, "build_llm", build)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


def ask(client: TestClient, question: str = "What was the average price yesterday?"):
    return client.post("/api/ask", json={"question": question})


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_index_and_static_files(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "<title>GridAgent" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_website_has_no_developer_pages(client: TestClient) -> None:
    page = client.get("/").text.lower()
    for word in ("evaluation", "oracle", "token", "agent garden", "claude", "haiku", "sonnet"):
        assert word not in page
    for path in ("/api/runs", "/api/reports", "/api/agents/energy_qa", "/api/demo"):
        assert client.get(path).status_code in (404, 405)


def test_config(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config = client.get("/api/config").json()
    assert config["available"] is True
    assert config["recorded_data"] is True and config["today"] == "2026-11-14"
    assert len(config["examples"]) >= 4
    keys = " ".join(config).lower()
    assert not any(word in keys for word in ("model", "agent", "budget", "cost"))


def test_unavailable_without_api_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/config").json()["available"] is False
    response = ask(client)
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]


def test_answer_payload_is_plain_language(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch)
    response = ask(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "answered"
    assert "161.73" in body["answer"]
    assert body["steps"] == [
        "Retrieved day-ahead electricity prices (Germany–Luxembourg) in 15-minute resolution "
        "for 13 Nov 2026",
        "Calculated the average of the price",
    ]
    assert body["sources"] == [services.PRICE_SOURCE]
    [chart] = body["charts"]
    assert len(chart["points"]) == 96 and chart["unit"] == "EUR / MWh"
    expected_keys = {"id", "question", "answer", "status", "asked_at", "steps", "sources", "charts"}
    assert set(body) == expected_keys
    visible = " ".join(body["steps"]).lower()
    assert not any(word in visible for word in JARGON)
    assert services.load_runs(settings.log_path)[0].run_id == body["id"]  # still logged


def test_failures_hide_internal_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch, turns=[LLMError("Model API error (HTTP 500): internal stack trace")])
    body = ask(client).json()
    assert body["status"] == "failed"
    assert body["answer"] == FAILED_ANSWER
    assert "500" not in str(body) and "stack" not in str(body)
    assert body["steps"] == [] and body["charts"] == []


def test_declined_question(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch, turns=[[text("I can't give investment advice.")]])
    body = ask(client, "Should I buy RWE shares?").json()
    assert body["status"] == "answered"  # a polite decline is a normal answer
    assert body["sources"] == [] and body["charts"] == []


def test_question_validation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch)
    assert ask(client, "x").status_code == 422
    assert ask(client, "a" * 501).status_code == 422


def test_usage_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch, input_tokens=4_000_000)  # $0.40 per call at Haiku prices, x3 calls
    assert ask(client).status_code == 200
    blocked = ask(client)
    assert blocked.status_code == 429
    assert "usage limit" in blocked.json()["detail"]
