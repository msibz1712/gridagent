"""One briefing run end to end: with the model, without it, and what a later run keeps."""

import json
from datetime import date
from pathlib import Path

import pytest
from test_writer import draft

import gridagent.monitor.run as run_module
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.llm.fake import FakeLLM, tool_use
from gridagent.monitor.daily import build_briefing
from gridagent.monitor.run import run_briefing

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
DAY, TODAY = date(2026, 10, 29), date(2026, 10, 30)


@pytest.fixture
def offline(monkeypatch):
    """Fixture data, no long history, and a stand-in for the analyst agent's explanations."""
    data = FixtureDataSource(FIXTURE_DIR)

    def no_history(d, reviewed, today):
        return build_briefing(d, reviewed, today, history={})

    monkeypatch.setattr(run_module, "build_briefing", no_history)
    explained: list[str] = []

    def explain(briefing, analyst, build_llm, data, today, max_cost, on_run) -> None:
        explained.append(analyst.config.name)
        briefing.events[0].explanation = "The spike coincided with low wind."

    monkeypatch.setattr(run_module, "explain_events", explain)
    return data, explained


def writer_llm(config) -> FakeLLM:
    return FakeLLM([[tool_use("publish_story", draft(), tool_id="s1")]],
                   model_name="claude-haiku-5-5")  # fmt: skip


def brief(data, tmp_path: Path, use_model: bool):
    return run_briefing(data, DAY, TODAY, agents_dir=AGENTS, reports_dir=tmp_path,
                        log_path=tmp_path / "runs.jsonl", use_model=use_model,
                        max_cost_usd=0.05, build_llm=writer_llm)  # fmt: skip


def test_with_the_model_both_agents_run_and_the_story_is_saved(offline, tmp_path) -> None:
    data, explained = offline
    briefing, path = brief(data, tmp_path, use_model=True)
    assert explained == ["energy_qa"]  # the analyst explains, then the writer writes
    assert briefing.story["written_by"] == "model" and briefing.story["model"] == "claude-haiku-5-5"
    saved = json.loads(path.read_text())
    assert path == tmp_path / "briefings" / f"{DAY}.json"
    assert saved["story"]["headline"] == draft()["headline"]


def test_a_run_without_the_model_keeps_the_earlier_model_story(offline, tmp_path) -> None:
    data, _ = offline
    brief(data, tmp_path, use_model=True)
    briefing, _ = brief(data, tmp_path, use_model=False)  # e.g. a refresh without a key
    assert briefing.story["written_by"] == "model"
    assert briefing.events[0].explanation == "The spike coincided with low wind."


def test_without_the_model_and_nothing_earlier_the_template_is_used(offline, tmp_path) -> None:
    data, explained = offline
    briefing, _ = brief(data, tmp_path, use_model=False)
    assert briefing.story["written_by"] == "template" and explained == []
