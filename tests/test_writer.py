"""The news writer: fact sheet, desk check, one revision, template fallback."""

import json
from datetime import date
from pathlib import Path

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.garden.registry import resolve
from gridagent.llm.fake import FakeLLM, text, tool_use
from gridagent.monitor.daily import build_briefing
from gridagent.monitor.writer import (
    NewsStory,
    check_story,
    fact_sheet,
    template_story,
    write_story,
)

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
WRITER = resolve(AGENTS, "news_writer@0.3.0")


@pytest.fixture(scope="module")
def briefing():
    return build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 29),
                          today=date(2026, 10, 30), history={})  # fmt: skip


def draft(**changes) -> dict:
    story = {
        "headline": "Evening prices nearly double as wind fades",
        "standfirst": "Between 13:00 and 20:00 the price averaged 394.56 €/MWh, against a usual "
        "183.05 for those hours on comparable weekdays.",
        "paragraphs": [
            "The day-ahead price averaged 314.85 €/MWh, against 157.82 on a normal day.",
            "Wind produced 100.70 GWh, against 505.51; the price was higher than the residual "
            "load explains.",
            "For Friday 30 October the forecast shows 98 GWh of wind.",
        ],
        "in_brief": [],
    }
    return story | changes


def sheet(briefing) -> str:
    return json.dumps(fact_sheet(briefing), ensure_ascii=False)


# ---------- the desk check ----------


def test_a_clean_draft_passes(briefing) -> None:
    assert check_story(NewsStory(**draft()), sheet(briefing)) == []


@pytest.mark.parametrize(("changes", "problem"), [
    ({"standfirst": "The price was 412.50 €/MWh."}, "Numbers not in the fact sheet: 412.50"),
    ({"headline": "Low wind caused a price spike"}, "Causal wording (caused)"),
    ({"headline": "Prices skyrocketed in the evening"}, "Hype words (skyrocketed)"),
    ({"headline": 'Traders said "ouch"'}, "Quotation marks"),
    ({"headline": "Evening " * 12}, "Headline is"),
    ({"paragraphs": ["Only one paragraph."]}, "1 paragraphs"),
    ({"headline": "Wind output sets a record"}, "Hype words (record)"),
    ({"headline": "The fact sheet does not show the cause"}, "Wording about the inputs"),
    ({"headline": "Highest since at least 2024, the start of the data"}, "start of the data"),
    ({"paragraphs": ["Wind " * 95, "Two.", "Three."]}, "Paragraph 1 has 95 words"),
    ({"paragraphs": ["314.85, 157.82, 100.70, 505.51, 394.56, 183.05 and 98.", "Two.",
                     "Three."]}, "Paragraph 1 has 7 numbers"),
])  # fmt: skip
def test_the_desk_rejects(briefing, changes: dict, problem: str) -> None:
    problems = check_story(NewsStory(**draft(**changes)), sheet(briefing))
    assert any(problem in p for p in problems), problems


def test_every_is_not_the_hype_word_ever(briefing) -> None:
    story = NewsStory(**draft(headline="Prices rose in every evening hour"))
    assert check_story(story, sheet(briefing)) == []


def test_the_template_always_passes(briefing) -> None:
    story = template_story(briefing)
    assert story.written_by == "template" and check_story(story, sheet(briefing)) == []


# ---------- writing ----------


def test_a_good_draft_is_published_with_its_cost(briefing) -> None:
    llm = FakeLLM([[tool_use("publish_story", draft(), "t1")]], model_name="claude-haiku-5-5")
    story = write_story(briefing, WRITER, llm)
    assert story.written_by == "model" and story.headline.startswith("Evening prices")
    assert story.cost_usd > 0 and len(llm.requests) == 1


def test_a_rejected_draft_is_revised_once(briefing) -> None:
    bad = draft(headline="Wind shortfall caused a spike")
    llm = FakeLLM(
        [[tool_use("publish_story", bad, "t1")], [tool_use("publish_story", draft(), "t2")]]
    )
    story = write_story(briefing, WRITER, llm)
    assert story.written_by == "model" and story.headline == draft()["headline"]
    feedback = llm.requests[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] and "Causal wording" in feedback["content"]


def test_two_failed_drafts_mean_the_template(briefing) -> None:
    bad = draft(standfirst="The price was 412.50 €/MWh.")
    llm = FakeLLM([[tool_use("publish_story", bad, "t1")], [tool_use("publish_story", bad, "t2")]])
    story = write_story(briefing, WRITER, llm)
    assert story.written_by == "template" and "412.50" in story.problems[0]


def test_a_text_answer_is_asked_to_publish(briefing) -> None:
    llm = FakeLLM([[text("Here is the story...")], [tool_use("publish_story", draft(), "t1")]])
    assert write_story(briefing, WRITER, llm).written_by == "model"


def test_the_writer_is_a_registered_agent_without_data_tools() -> None:
    assert WRITER.config.tools == [] and "publish_story" in WRITER.system_prompt


def test_a_cut_off_answer_is_never_published(briefing) -> None:
    """Live run of 0.1.0: thinking used up max_tokens, the draft had only a headline."""
    from gridagent.llm.base import ModelResponse, TokenUsage

    cut = ModelResponse(content=[tool_use("publish_story", {"headline": "Prices fall"}, "t1")],
                        stop_reason="max_tokens", usage=TokenUsage(input_tokens=1, output_tokens=1),
                        model="fake-model")  # fmt: skip
    story = write_story(briefing, WRITER, FakeLLM([cut]))
    assert story.written_by == "template" and "max_tokens" in story.problems[0]


def test_explanations_with_causal_wording_are_not_published(briefing) -> None:
    from gridagent.monitor.explain import explain_events

    causal = "The main driver was very strong wind, which pushed prices down due to low demand."
    llm = lambda: FakeLLM([[text(causal)]])  # noqa: E731
    energy_qa = resolve(AGENTS, "energy_qa@0.11.0")
    explain_events(briefing, energy_qa, llm, FixtureDataSource(FIXTURE_DIR), date(2026, 10, 30),
                   max_cost_usd=1.0)  # fmt: skip
    first = briefing.events[0]
    assert first.explanation is None and "due to" in first.explanation_problems[0]
