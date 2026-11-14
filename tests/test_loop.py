"""The agent loop, driven by a scripted FakeLLM and fixture data (offline)."""

import json
from datetime import date
from pathlib import Path

import pytest

from gridagent.agent.loop import FAILURE_ANSWER, REFUSAL_ANSWER, Agent, build_user_message
from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.garden.registry import resolve
from gridagent.llm.base import LLMError, ModelResponse, TokenUsage
from gridagent.llm.fake import FakeLLM, text, tool_use

from conftest import FIXTURE_DIR

AGENTS_DIR = Path(__file__).parent.parent / "agents"
TODAY = date(2026, 11, 14)

PRICE_CALL = tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"}, "t1")
MEAN_CALL = tool_use(
    "compute_stats",
    {"ref": "price_1", "operation": "mean", "source": None, "relative_to": None, "per_day": False},
    "t2",
)


def make_agent(llm: FakeLLM, **limits) -> Agent:
    loaded = resolve(AGENTS_DIR, "energy_qa@0.1.0")
    if limits:
        loaded.config.limits = loaded.config.limits.model_copy(update=limits)
    return Agent(loaded, llm, FixtureDataSource(FIXTURE_DIR), today=lambda: TODAY)


def test_happy_path_two_tool_steps_then_answer() -> None:
    llm = FakeLLM([[PRICE_CALL], [MEAN_CALL], [text("The average was 161.73 EUR/MWh.")]])
    log = make_agent(llm).run("What was the average day-ahead price yesterday?")

    assert log.status == "ok"
    assert log.final_answer == "The average was 161.73 EUR/MWh."
    assert [c.name for c in log.tool_calls] == ["get_day_ahead_price", "compute_stats"]
    assert len(log.steps) == 3
    assert log.usage.input_tokens == 300  # 3 calls x 100 (FakeLLM default)

    # The tool result the model saw in step 3 contains the computed mean.
    last_request = llm.requests[-1]["messages"]
    tool_result = last_request[-1]["content"][0]
    assert tool_result["tool_use_id"] == "t2"
    assert json.loads(tool_result["content"])["value"] == pytest.approx(161.73, abs=0.01)


def test_first_message_contains_today_and_system_prompt_is_stable() -> None:
    llm = FakeLLM([[text("hi")]])
    make_agent(llm).run("hello")
    request = llm.requests[0]
    assert request["messages"][0]["content"] == build_user_message("hello", TODAY)
    assert "2026-11-14 (Saturday)" in request["messages"][0]["content"]
    assert "2026" not in request["system"]  # no dates in the cached system prompt


def test_parallel_tool_calls_return_in_one_user_message() -> None:
    gen = tool_use(
        "get_generation", {"start": "2026-11-02", "end": "2026-11-08", "sources": None}, "a"
    )
    llm = FakeLLM([[PRICE_CALL, gen], [text("done")]])
    make_agent(llm).run("q")
    results = llm.requests[1]["messages"][-1]
    assert results["role"] == "user"
    assert [r["tool_use_id"] for r in results["content"]] == ["t1", "a"]


def test_tool_error_is_passed_back_and_model_can_recover() -> None:
    bad = tool_use("get_day_ahead_price", {"start": "2026-12-01", "end": "2026-12-01"}, "t0")
    llm = FakeLLM([[bad], [text("No data is available for that date.")]])
    log = make_agent(llm).run("price on 1 Dec?")
    assert log.status == "ok"
    assert log.tool_calls[0].is_error
    assert llm.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_assistant_turns_are_appended_unchanged() -> None:
    thinking = {"type": "thinking", "thinking": "", "signature": "abc"}
    llm = FakeLLM([[thinking, PRICE_CALL], [text("ok")]])
    make_agent(llm).run("q")
    assert llm.requests[1]["messages"][1] == {
        "role": "assistant",
        "content": [thinking, PRICE_CALL],
    }


def test_max_steps_stops_the_loop() -> None:
    llm = FakeLLM([[PRICE_CALL]] * 10)
    log = make_agent(llm, max_steps=3).run("loop forever")
    assert log.status == "max_steps"
    assert len(log.steps) == 3
    assert log.final_answer == FAILURE_ANSWER


def test_refusal_does_not_run_tools() -> None:
    refusal = ModelResponse(
        content=[PRICE_CALL], stop_reason="refusal", usage=TokenUsage(), model="fake"
    )
    log = make_agent(FakeLLM([refusal])).run("something bad")
    assert log.status == "refused"
    assert log.final_answer == REFUSAL_ANSWER
    assert log.tool_calls == []


def test_truncated_answer_is_an_error() -> None:
    cut = ModelResponse(
        content=[text("The avera")], stop_reason="max_tokens", usage=TokenUsage(), model="fake"
    )
    log = make_agent(FakeLLM([cut])).run("q")
    assert log.status == "error"
    assert "max_tokens" in log.error


def test_model_error_is_logged_not_raised() -> None:
    log = make_agent(FakeLLM([LLMError("Could not reach the model API")])).run("q")
    assert log.status == "error"
    assert "Could not reach" in log.error


def test_run_timeout() -> None:
    times = iter([0.0, 0.0, 1.0, 1.0, 1.0, 999.0, 999.0, 999.0])
    loaded = resolve(AGENTS_DIR, "energy_qa@0.1.0")
    agent = Agent(
        loaded,
        FakeLLM([[PRICE_CALL]] * 5),
        FixtureDataSource(FIXTURE_DIR),
        today=lambda: TODAY,
        clock=lambda: next(times),
    )
    log = agent.run("slow")
    assert log.status == "timeout"


def test_cost_uses_model_prices() -> None:
    llm = FakeLLM(
        [[text("hi")]],
        model_name="claude-sonnet-5-5",
        usage=TokenUsage(input_tokens=1_000_000, output_tokens=0),
    )
    log = make_agent(llm).run("q")
    assert log.cost_usd == pytest.approx(2.00)


def test_run_log_is_one_json_line(tmp_path: Path) -> None:
    log = make_agent(FakeLLM([[PRICE_CALL], [MEAN_CALL], [text("ok")]])).run("q")
    path = tmp_path / "runs.jsonl"
    append_run_log(path, log)
    append_run_log(path, log)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    restored = RunLog.model_validate_json(lines[0])
    assert restored.agent == "energy_qa" and restored.agent_version == "0.1.0"
    assert restored.steps[0].tool_calls[0].input == PRICE_CALL["input"]
    assert restored.steps[0].latency_s >= 0
