"""Every number in an answer must be traceable to data (tools/grounding.py, compare_values)."""

import json
from datetime import date

import pytest
from test_loop import AGENTS_DIR

from gridagent.agent.loop import Agent
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.evals.dataset import EvalCase
from gridagent.evals.scoring import score
from gridagent.garden.registry import resolve
from gridagent.llm.fake import FakeLLM, text, tool_use
from gridagent.tools.grounding import rounding_tolerance, unsupported_numbers
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

TOOL = ['{"value": 13.449, "unit": "ct/kWh"}', '{"value": 13.39, "official": true}']


# ---------- the rule ----------


def test_catches_the_mental_arithmetic_seen_in_live_runs() -> None:
    """Real 0.3.0 answer: the difference and percentage were computed in the model's head."""
    answer = "The official value is 13.39 ct/kWh, so the two differ by 0.06 ct/kWh (about 0.4%)."
    assert unsupported_numbers(answer, TOOL, []) == ["0.06", "0.4"]


@pytest.mark.parametrize(
    ("answer", "tool_value"),
    [
        ("Wind supplied 23% of generation.", 23.31),  # rounded
        ("Wind supplied 23.3% of generation.", 23.31),
        ("about 1,200 MW", 1234.0),  # trailing zeros: rounded to hundreds
        ("1.25 TWh", 1250.4),  # GWh -> TWh, the allowed factor of 1000
        ("45.7 GW", 45722.0),  # MW -> GW
        ("0.059 ct/kWh lower", -0.059),  # sign stated in words
    ],
)
def test_rounding_unit_factor_and_sign_are_accepted(answer: str, tool_value: float) -> None:
    assert unsupported_numbers(answer, [json.dumps({"value": tool_value})], []) == []


def test_dates_times_years_and_names_are_not_quantities() -> None:
    answer = (
        "From 2 to 8 November 2026 (2026-11-02, 00:00-23:45, Nov 2-8, 05.11.2026) "
        "in 50Hertz, using 15-minute values in Q4: CO2."
    )
    assert unsupported_numbers(answer, [], []) == []


def test_numbers_from_the_question_are_backed() -> None:
    answer = "The price was above 300 EUR/MWh for 27.5 hours."
    assert unsupported_numbers(answer, ['{"value": 27.5}'], ["Hours above 300 EUR/MWh?"]) == []


def test_wrongly_rounded_value_is_caught() -> None:
    """7.745 is reported as 7.75 by the tool; writing 7.74 misquotes it (found in our evals)."""
    assert unsupported_numbers("Solar: 7.74%.", ['{"value": 7.75}'], []) == ["7.74"]


def test_rounding_tolerance() -> None:
    assert rounding_tolerance("23.3") == pytest.approx(0.05)
    assert rounding_tolerance("23") == 0.5
    assert rounding_tolerance("1200") == 50
    assert rounding_tolerance("0") == 0.5


# ---------- compare_values ----------


def toolbox() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))


def compare(box: ToolBox, a: float, b: float):
    return box.execute(
        "compare_values", {"a": a, "b": b, "a_label": "a", "b_label": "b", "unit": "u"}
    )


def test_compare_values_computes_difference_percent_ratio_and_sum() -> None:
    box = toolbox()
    box.results.append('{"value": 13.449} {"value": 13.39}')
    result = json.loads(compare(box, 13.449, 13.39).content)
    assert result["difference"] == 0.059  # small differences keep their digits
    assert result["percent_difference"] == 0.4406
    assert result["ratio"] == 1.004 and result["sum"] == 26.84


def test_compare_values_rejects_numbers_the_tools_never_produced() -> None:
    box = toolbox()
    box.results.append('{"value": 13.39}')
    result = compare(box, 13.5, 13.39)  # 13.5 is made up
    assert result.is_error and "a = 13.5 does not appear" in result.content


def test_compare_values_accepts_question_numbers_and_handles_zero() -> None:
    box = toolbox()
    box.questions.append("Was it more than 100 EUR/MWh?")
    box.results.append('{"value": 0.0}')
    result = json.loads(compare(box, 100, 0).content)
    assert result["percent_difference"] is None and result["ratio"] is None


# ---------- end to end: run log and eval check ----------

PRICE = tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"}, "t1")
MEAN = tool_use("compute_stats", {"ref": "price_1", "operation": "mean", "source": None,
                                  "relative_to": None, "per_day": False, "threshold": None,
                                  "weight_ref": None, "weight_source": None}, "t2")  # fmt: skip


def run(answers: list[str]) -> list:
    agent = resolve(AGENTS_DIR, "energy_qa@0.5.0")
    turns = [[PRICE], [MEAN], [text(answers[0])]] + [[text(a)] for a in answers[1:]]
    runner = Agent(
        agent, FakeLLM(turns), FixtureDataSource(FIXTURE_DIR), lambda: date(2026, 11, 14)
    )
    conversation = runner.new_conversation()
    return [runner.run(f"question {i}", conversation) for i in range(len(answers))]


def test_run_log_records_unsupported_numbers_and_the_eval_fails_them() -> None:
    (log,) = run(["The average was 161.73 EUR/MWh, about 12 EUR/MWh more than usual."])
    assert log.unsupported_numbers == ["12"]
    case = EvalCase(id="x", category="factual", question="Average price?", outcome="answer",
                    numbers=[{"label": "mean", "values": [161.73]}],
                    oracle_answer="-")  # fmt: skip
    checks = score(case, log)
    assert checks.numbers is True and checks.supported is False and not checks.passed()


def test_follow_up_may_repeat_numbers_from_earlier_turns() -> None:
    first, second = run(["The average was 161.73 EUR/MWh.", "Yes: it was 161.73 EUR/MWh."])
    assert first.unsupported_numbers == [] and second.unsupported_numbers == []


def test_run_summary_counts_only_checked_runs() -> None:
    from gridagent.services import summarize_runs

    clean, unbacked = run(["The average was 161.73 EUR/MWh.", "Roughly 12 EUR/MWh more."])
    old = clean.model_copy(update={"unsupported_numbers": None})  # log from before the check
    assert summarize_runs([clean, unbacked, old]).unsupported_rate == 0.5
    assert summarize_runs([old]).unsupported_rate is None
