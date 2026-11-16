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
STATS = {
    "ref": "price_1",
    "operation": "mean",
    "source": None,
    "relative_to": None,
    "per_day": False,
}
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
        reports_dir=tmp_path / "reports",  # never the real briefings
        auto_briefing=False,
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
    expected_keys = {
        "id",
        "question",
        "answer",
        "status",
        "asked_at",
        "steps",
        "sources",
        "charts",
        "conversation_id",
        "turn",
        "turns_left",
        "new_conversation",
    }
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


# ---------- conversations ----------

FOLLOW_UP_SCRIPT = [
    [tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"})],
    [tool_use("compute_stats", STATS)],
    [text(ANSWER)],
    # follow-up: reuses price_1 from the first turn, no new fetch
    [tool_use("compute_stats", STATS | {"operation": "max"})],
    [text("The highest price that day was 231.49 EUR/MWh at 17:45. Source: SMARD.de")],
]


def test_follow_up_continues_the_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = list(FOLLOW_UP_SCRIPT)
    llm = FakeLLM(script, model_name="claude-haiku-5-5")
    monkeypatch.setattr(services, "build_llm", lambda cfg, model=None: llm)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    first = ask(client).json()
    assert first["turn"] == 1 and first["turns_left"] == 9
    second = client.post(
        "/api/ask",
        json={
            "question": "And the highest price that day?",
            "conversation_id": first["conversation_id"],
        },
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    assert second["turn"] == 2 and second["turns_left"] == 8
    assert "231.49" in second["answer"]
    # The follow-up's chart comes from data fetched in turn 1.
    [chart] = second["charts"]
    assert chart["title"].startswith("Day-ahead") and len(chart["points"]) == 96
    assert second["steps"] == ["Found the highest value of the price and when it occurred"]


def test_expired_conversation_starts_a_new_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch)
    body = client.post(
        "/api/ask", json={"question": "Average price yesterday?", "conversation_id": "0" * 16}
    ).json()
    assert body["new_conversation"] is True and body["turn"] == 1
    assert body["conversation_id"] != "0" * 16


def test_conversation_limit(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.max_turns = 1
    limited = TestClient(create_app(settings))
    fake_model(monkeypatch, turns=[[text("First answer.")]])
    first = ask(limited).json()
    assert first["turns_left"] == 0
    second = limited.post(
        "/api/ask", json={"question": "And then?", "conversation_id": first["conversation_id"]}
    )
    assert second.status_code == 409
    assert "start a new conversation" in second.json()["detail"]


def test_bad_conversation_id_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_model(monkeypatch)
    response = client.post("/api/ask", json={"question": "hello there", "conversation_id": "../x"})
    assert response.status_code == 422


def test_delete_conversation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_model(monkeypatch)
    conversation_id = ask(client).json()["conversation_id"]
    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204
    # A follow-up with the deleted id starts over.
    fake_model(monkeypatch)
    again = client.post(
        "/api/ask",
        json={"question": "Average price yesterday?", "conversation_id": conversation_id},
    ).json()
    assert again["new_conversation"] is True


# ---------- the dashboard ----------


def saved_briefing(reports_dir: Path) -> None:
    from datetime import date

    from gridagent.data.fixture_source import FixtureDataSource
    from gridagent.monitor.daily import build_briefing
    from gridagent.monitor.explain import save_briefing

    b = build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 29),
                       today=date(2026, 10, 30), history={})  # fmt: skip
    save_briefing(reports_dir / "briefings", b)


def test_home_is_the_chat_until_a_briefing_exists(client: TestClient) -> None:
    page = client.get("/")
    assert page.url.path == "/ask" and "<title>GridAgent" in page.text


def test_home_is_the_dashboard_with_ask_and_refresh(settings: Settings) -> None:
    saved_briefing(settings.reports_dir)
    client = TestClient(create_app(settings))
    page = client.get("/")
    assert page.url.path == "/dashboard/" and "Daily briefing" in page.text
    assert 'href="/ask"' in page.text and 'action="/refresh"' in page.text
    assert client.get("/dashboard/days/2026-10-29.html").status_code == 200
    assert 'href="/dashboard/"' in client.get("/ask").text  # and back from the chat


def test_refresh_runs_in_the_background_with_a_self_reloading_wait_page(
    settings: Settings,
) -> None:
    import threading

    app = create_app(settings)
    release, calls = threading.Event(), []

    def slow_refresh() -> str:
        calls.append(1)
        release.wait(5)
        return "Briefing ready."

    app.state.refresh = slow_refresh
    client = TestClient(app)
    response = client.post("/refresh", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/refresh/wait"
    wait = client.get("/refresh/wait", follow_redirects=False)
    assert wait.status_code == 200 and 'http-equiv="refresh"' in wait.text
    release.set()
    app.state.refresh_thread.join(5)
    done = client.get("/refresh/wait", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert calls == [1] and app.state.last_refresh == "Briefing ready."


def test_wait_page_also_covers_a_briefing_started_by_the_daily_loop(settings: Settings) -> None:
    app = create_app(settings)
    client = TestClient(app)
    with app.state.refreshing:  # the daily loop is building a briefing; no button thread
        wait = client.get("/refresh/wait", follow_redirects=False)
        assert wait.status_code == 200 and 'http-equiv="refresh"' in wait.text
    done = client.get("/refresh/wait", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/"
