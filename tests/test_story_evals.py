"""The news_writer eval: each check catches what it is meant to, and the eval set runs."""

from datetime import date
from pathlib import Path

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.evals.stories import load_story_eval_set, run_story_eval, story_checks
from gridagent.garden.registry import resolve
from gridagent.monitor.daily import build_briefing
from gridagent.monitor.writer import NewsStory

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
EVAL_SET = AGENTS / "news_writer" / "evals" / "cases.yaml"


@pytest.fixture(scope="module")
def briefing():
    # lead story: a price spike 13:00-20:00 that the merit order does NOT explain
    return build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 29),
                          today=date(2026, 10, 30), history={})  # fmt: skip


def good(**changes) -> NewsStory:
    story = {
        "headline": "Evening prices more than double as wind fades",
        "standfirst": "From 13:00 to 20:00 the price averaged 394.56 €/MWh, against a usual "
        "183.05 for those hours.",
        "paragraphs": [
            "The afternoon and evening price was 211.51 €/MWh above normal.",
            "Wind and solar covered 6.3 % of demand from 16:00; the price was higher than the "
            "residual load explains, and the data does not show the reason.",
            "For Friday the forecast shows little wind; prices are lowest from 11:00 to 14:00.",
        ],
        "in_brief": ["Prices were also high from 08:00 to 09:00."],
    }
    return NewsStory(**(story | changes))


def test_a_good_story_passes_every_applicable_check(briefing) -> None:
    checks = story_checks(good(), briefing)
    assert [k for k, ok in checks.items() if ok is False] == []
    assert checks["since_at_least"] is None and checks["quiet_day"] is None  # not applicable


@pytest.mark.parametrize(("changes", "failed"), [
    ({"written_by": "template"}, "published"),
    ({"headline": "Wind output falls", "standfirst": "Wind fell against its normal of 505.51.",
      "paragraphs": ["Wind fell.", "The residual load was high.", "The forecast shows wind."]},
     "lead_first"),
    ({"paragraphs": ["Prices rose 211.51 above normal.",
                     "The price was in line with the residual load.",
                     "The forecast shows little wind."]}, "price_reading"),
    ({"paragraphs": ["Prices rose 211.51 above normal.", "Wind was low.",
                     "The forecast shows little wind."]}, "price_reading"),  # no residual load
    ({"standfirst": "Electricity was expensive in the evening."}, "comparison"),
    ({"paragraphs": ["Prices rose 211.51 above normal.",
                     "The price was higher than the residual load explains.",
                     "Prices will fall on Friday."]}, "certainty"),
    ({"paragraphs": ["Prices rose 211.51 above normal.",
                     "The price was higher than the residual load explains.",
                     "Consumers should shift demand."]}, "certainty"),
    ({"paragraphs": ["Prices rose 211.51 above normal.",
                     "The price was higher than the residual load explains.",
                     "Wind was low all day."]}, "outlook"),
    ({"in_brief": ["One.", "Two.", "Three.", "Four."]}, "in_brief"),
])  # fmt: skip
def test_each_check_catches_its_failure(briefing, changes, failed) -> None:
    checks = story_checks(good(**changes), briefing)
    assert checks[failed] is False


def test_rounded_lead_numbers_count(briefing) -> None:
    story = good(standfirst="From 13:00 the price averaged 395 €/MWh, against a usual 183.",
                 headline="Evening prices more than double")  # fmt: skip
    assert story_checks(story, briefing)["lead_first"] is True


def test_quiet_day_records_and_no_invented_outlook(briefing) -> None:
    quiet = briefing.model_copy(update={"events": [], "outlooks": [], "plans": []})
    plain = good(paragraphs=["A quiet day.", "Demand was normal.", "Nothing else happened."],
                 in_brief=[])  # fmt: skip
    checks = story_checks(plain, quiet)
    assert checks["quiet_day"] is True and checks["lead_first"] is None
    assert checks["outlook"] is True and checks["in_brief"] is True
    assert story_checks(good(in_brief=[]), quiet)["quiet_day"] is False
    invented = good(paragraphs=["Quiet.", "Normal.", "Prices are expected to rise."])
    assert story_checks(invented, quiet)["outlook"] is False

    lead = briefing.events[0].model_copy(update={"kind": "record", "facts": {
        "value": 1116.92, "previous_date": None, "compared_back_to": "2024-10-16"}})  # fmt: skip
    record_day = briefing.model_copy(update={"events": [lead]})
    kept = good(standfirst="Wind produced 1116.92 GWh, the most since at least October 2024.")
    lost = good(standfirst="Wind produced 1116.92 GWh, the most ever against any day.")
    assert story_checks(kept, record_day)["since_at_least"] is True
    assert story_checks(lost, record_day)["since_at_least"] is False


def test_the_eval_set_loads_and_the_template_baseline_runs() -> None:
    eval_set, briefings, digest = load_story_eval_set(EVAL_SET)
    assert len(eval_set.cases) >= 12 and set(briefings) == {c.id for c in eval_set.cases}
    report = run_story_eval(resolve(AGENTS, "news_writer@0.3.0"), EVAL_SET, "template")
    assert report.published_rate == 0.0  # the template is never the model's story
    assert report.pass_rate == 1.0  # built from the same facts: the correctness bar to match
    assert len(report.cases) == len(eval_set.cases) and len(digest) == 16


def test_replay_of_the_recorded_live_run_is_stable() -> None:
    for version in ("0.3.0", "0.4.0"):  # both recorded live runs, scored with today's checks
        report = run_story_eval(resolve(AGENTS, f"news_writer@{version}"), EVAL_SET, "replay")
        assert report.pass_rate >= 0.94
    assert report.total_cost_usd > 0 and all(c.model_calls >= 1 for c in report.cases)


# Found by reading the first live run: right stories the first version of a check failed.


def test_numbers_written_as_words_count_for_the_lead(briefing) -> None:
    lead = briefing.events[0].model_copy(
        update={"kind": "streak", "facts": {"days_in_a_row": 3, "threshold": 138.95}}
    )
    streak_day = briefing.model_copy(update={"events": [lead]})
    story = good(
        headline="Price stays in the highest 10 % for a third day",
        standfirst="The price was 143.15, against 130.68 normally.",
        paragraphs=["It was high.", "Wind was low.", "The forecast shows wind."],
    )
    assert story_checks(story, streak_day)["lead_first"] is True


def test_above_or_below_a_forecast_is_a_comparison(briefing) -> None:
    story = good(standfirst="Wind reached 747.85 GWh, 7.9% below its day-ahead forecast of 811.76.")
    assert story_checks(story, briefing)["comparison"] is True


def test_looking_ahead_means_naming_a_forecast_day(briefing) -> None:
    plan_day = date.fromisoformat(briefing.plans[0].day)
    ahead = good(
        paragraphs=[
            "Prices rose 211.51 above normal.",
            "The price was higher than the residual load explains.",
            f"{plan_day:%A}'s prices are lowest, near zero, from 04:00.",
        ]
    )
    assert story_checks(ahead, briefing)["outlook"] is True


def test_a_change_from_the_day_before_is_a_comparison(briefing) -> None:
    story = good(standfirst="Solar fell 143.38 GWh, to 206.88 GWh from 350.26 the day before.")
    assert story_checks(story, briefing)["comparison"] is True
