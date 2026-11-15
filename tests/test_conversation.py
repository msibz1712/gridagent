"""Multi-turn conversations: history, reuse of fetched data, rollback, limits, store."""

import json
from datetime import date
from pathlib import Path

import pytest

from gridagent.agent.conversation import (
    Conversation,
    ConversationLimitError,
    ConversationStore,
)
from gridagent.agent.loop import Agent
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.garden.registry import resolve
from gridagent.llm.base import LLMError, ModelResponse, TokenUsage
from gridagent.llm.fake import FakeLLM, text, tool_use
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
TODAY = date(2026, 11, 14)
WEEK = {"start": "2026-11-02", "end": "2026-11-08", "sources": None, "scope": None}
STATS = {"relative_to": None, "per_day": False, "threshold": None, "weight_ref": None,
         "weight_source": None}  # fmt: skip


def stats(ref: str, source: str, operation: str = "share") -> dict:
    return {"ref": ref, "operation": operation, "source": source, **STATS}


def make_agent(llm: FakeLLM) -> Agent:
    agent = resolve(AGENTS, "energy_qa")
    return Agent(agent, llm, FixtureDataSource(FIXTURE_DIR), today=lambda: TODAY)


def test_follow_up_reuses_data_from_the_first_turn() -> None:
    llm = FakeLLM([
        [tool_use("get_generation", WEEK, "t1")],
        [tool_use("compute_stats", stats("gen_1", "wind"), "t2")],
        [text("Wind: 23.3%.")],
        # follow-up: no new fetch, the model reuses gen_1
        [tool_use("compute_stats", stats("gen_1", "solar"), "t3")],
        [text("Solar: see tool result.")],
    ])  # fmt: skip
    agent = make_agent(llm)
    conversation = agent.new_conversation()

    first = agent.run("What share came from wind last week?", conversation)
    second = agent.run("And solar?", conversation)

    assert first.status == second.status == "ok"
    assert first.conversation_id == second.conversation_id == conversation.id
    assert (first.turn, second.turn) == (1, 2)
    assert conversation.turns == 2
    # The follow-up request carried the whole first turn, unchanged, before the new question.
    follow_up_request = llm.requests[3]["messages"]
    assert follow_up_request[0]["content"].startswith("Today's date in Germany is 2026-11-14")
    assert follow_up_request[-1] == {"role": "user", "content": "Follow-up question: And solar?"}
    # Turn 1 = question, tool call, tool result, tool call, tool result, answer; + new question.
    roles = [m["role"] for m in follow_up_request]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    # ...and the follow-up's calculation ran on the data fetched in turn 1.
    result = json.loads(second.tool_calls[0].result)
    assert result["series"] == "solar/total" and second.tool_calls[0].is_error is False


def test_failed_turn_is_rolled_back() -> None:
    cut_off = ModelResponse(content=[tool_use("get_generation", WEEK, "x")],
                            stop_reason="max_tokens", usage=TokenUsage(), model="fake")  # fmt: skip
    llm = FakeLLM([[text("First answer.")], cut_off, [text("Third answer.")]])
    agent = make_agent(llm)
    conversation = agent.new_conversation()

    agent.run("First?", conversation)
    history_after_first = list(conversation.messages)
    failed = agent.run("Second?", conversation)
    assert failed.status == "error"
    # The dangling tool call (no result) was removed, so the history is valid again.
    assert conversation.messages == history_after_first
    assert conversation.turns == 1

    third = agent.run("Third?", conversation)
    assert third.status == "ok" and third.turn == 2


def test_model_error_also_rolls_back() -> None:
    llm = FakeLLM([[text("ok")], LLMError("API down")])
    agent = make_agent(llm)
    conversation = agent.new_conversation()
    agent.run("one", conversation)
    before = list(conversation.messages)
    assert agent.run("two", conversation).status == "error"
    assert conversation.messages == before


def test_turn_limit() -> None:
    agent = make_agent(FakeLLM([[text("a")], [text("b")]]))
    conversation = agent.new_conversation()
    conversation.max_turns = 2
    agent.run("1", conversation)
    agent.run("2", conversation)
    assert conversation.turns_left == 0
    with pytest.raises(ConversationLimitError, match="limit of 2"):
        agent.run("3", conversation)


def test_single_questions_still_work_without_a_conversation() -> None:
    log = make_agent(FakeLLM([[text("hi")]])).run("hello")
    assert log.status == "ok" and log.turn == 1 and log.conversation_id


# ---------- store ----------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def toolbox() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: TODAY)


def test_store_start_get_delete() -> None:
    store = ConversationStore()
    conversation = store.start(toolbox(), TODAY)
    assert isinstance(conversation, Conversation)
    assert store.get(conversation.id) is conversation
    assert store.get("unknown") is None and store.get(None) is None
    assert store.delete(conversation.id) and store.get(conversation.id) is None


def test_store_expires_idle_conversations() -> None:
    clock = Clock()
    store = ConversationStore(idle_timeout_seconds=60, clock=clock)
    conversation = store.start(toolbox(), TODAY)
    clock.now += 50
    assert store.get(conversation.id) is conversation  # using it refreshes the timer
    clock.now += 50
    assert store.get(conversation.id) is conversation
    clock.now += 61
    assert store.get(conversation.id) is None
    assert len(store) == 0


def test_store_evicts_least_recently_used_when_full() -> None:
    clock = Clock()
    store = ConversationStore(max_conversations=2, clock=clock)
    first = store.start(toolbox(), TODAY)
    clock.now += 1
    second = store.start(toolbox(), TODAY)
    clock.now += 1
    store.get(first.id)  # first is now the most recently used
    clock.now += 1
    store.start(toolbox(), TODAY)
    assert store.get(first.id) is first
    assert store.get(second.id) is None


def test_asking_for_the_same_data_again_reuses_the_ref() -> None:
    box = toolbox()
    first = json.loads(box.execute("get_generation", WEEK).content)
    again = json.loads(box.execute("get_generation", WEEK).content)
    assert first["ref"] == again["ref"] == "gen_1"
    assert "reuse this ref" in again["note"]
    assert len(box.store) == 1  # no second copy of the data
    other = json.loads(box.execute("get_generation", WEEK | {"scope": "total"}).content)
    assert other["ref"] == "gen_2"  # different request -> new data


def test_failed_requests_are_not_remembered() -> None:
    box = toolbox()
    future = {"start": "2026-12-01", "end": "2026-12-02", "sources": None, "scope": None}
    assert box.execute("get_generation", future).is_error
    assert box.execute("get_generation", future).is_error  # tried again, not a cached error
