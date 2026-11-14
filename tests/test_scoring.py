"""Scoring rules: each check must pass good answers AND fail bad ones."""

from datetime import UTC, date, datetime

import pytest

from gridagent.agent.runlog import RunLog, StepLog, ToolCallLog
from gridagent.evals.dataset import EvalCase, ExpectedNumber
from gridagent.evals.scoring import date_mentioned, declined, extract_numbers, number_present, score
from gridagent.llm.base import TokenUsage


def make_log(
    answer: str, calls: list[tuple[str, dict]] = (), status: str = "ok", result: str = "161.73"
) -> RunLog:
    step = StepLog(
        step=1,
        model="m",
        stop_reason="end_turn",
        latency_s=0,
        usage=TokenUsage(),
        cost_usd=0,
        tool_calls=[
            ToolCallLog(name=n, input=i, is_error=False, latency_s=0, result=result)
            for n, i in calls
        ],
    )
    return RunLog(
        run_id="r",
        started_at=datetime.now(UTC),
        agent="a",
        agent_version="0.1.0",
        model="m",
        question="q",
        today="2026-11-14",
        status=status,
        final_answer=answer,
        steps=[step],
    )


PRICE_CASE = EvalCase.model_validate(
    {
        "id": "price",
        "category": "factual",
        "question": "Average price yesterday?",
        "outcome": "answer",
        "numbers": [{"label": "p", "values": [161.73]}],
        "dates": ["2026-11-13"],
        "oracle_answer": "x",
        "tool_calls": [
            {"name": "get_day_ahead_price", "args": {"start": "2026-11-13"}},
            {"name": "compute_stats", "args": {"operation": "mean"}},
        ],
    }
)
GOOD_CALLS = [
    ("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"}),
    ("compute_stats", {"ref": "price_1", "operation": "mean"}),
]
GOOD = (
    "The average price on 13 November 2026 was 161.73 EUR/MWh. "
    "Source: SMARD.de via Energy-Charts.info"
)


# ---------- helpers ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("45,722 MW", [45722.0]),
        ("-0.01 EUR/MWh", [-0.01]),
        ("−3.5 (unicode minus)", [-3.5]),
        ("2026-10-05 at 12:45", [2026.0, 10.0, 5.0, 12.0, 45.0]),  # no negative dates
        ("23.3%.", [23.3]),
    ],
)
def test_extract_numbers(text: str, expected: list[float]) -> None:
    assert extract_numbers(text) == expected


def test_number_tolerance() -> None:
    n = ExpectedNumber(label="x", values=[161.73])  # ±1%
    assert number_present(n, [162.0])
    assert not number_present(n, [165.0])
    share = ExpectedNumber(label="s", values=[23.3], tolerance_abs=0.5)
    assert number_present(share, [23.0])
    alt_units = ExpectedNumber(label="e", values=[2098.7, 2.0987])  # GWh or TWh
    assert number_present(alt_units, [2.1])


@pytest.mark.parametrize(
    "text",
    ["2026-11-13", "13 November 2026", "November 13th", "Fri 13 Nov", "13.11.2026", "2–13 Nov"],
)
def test_date_formats(text: str) -> None:
    assert date_mentioned(text, date(2026, 11, 13))


@pytest.mark.parametrize("text", ["14 November", "113 November", "13 October", "2026-11-14"])
def test_wrong_dates_are_not_accepted(text: str) -> None:
    assert not date_mentioned(text, date(2026, 11, 13))


# ---------- the scorer ----------


def test_good_answer_passes_everything() -> None:
    checks = score(PRICE_CASE, make_log(GOOD, GOOD_CALLS))
    assert checks.passed(), checks.failed_checks()


@pytest.mark.parametrize(
    ("answer", "calls", "failed"),
    [
        (GOOD.replace("161.73", "170.00"), GOOD_CALLS, "numbers"),  # wrong number
        (GOOD.replace("13 November 2026", "yesterday"), GOOD_CALLS, "dates"),  # no period
        (GOOD.split(" Source")[0], GOOD_CALLS, "source"),  # no source
        (GOOD, GOOD_CALLS[:1], "tool_use"),  # computed in its head
        (GOOD, [("get_day_ahead_price", {"start": "2026-11-12"}), GOOD_CALLS[1]], "tool_use"),
        ("I can't help with that.", [], "refusal"),  # over-refusal
    ],
)
def test_bad_answers_fail_the_right_check(answer: str, calls: list, failed: str) -> None:
    checks = score(PRICE_CASE, make_log(answer, calls))
    assert failed in checks.failed_checks()


def test_error_status_fails() -> None:
    assert (
        "no_error"
        in score(PRICE_CASE, make_log(GOOD, GOOD_CALLS, status="max_steps")).failed_checks()
    )


REFUSE_CASE = EvalCase(
    id="r", category="refusal", question="Buy RWE?", outcome="refuse", oracle_answer="x"
)


def test_refusal_passes_without_tools() -> None:
    assert score(REFUSE_CASE, make_log("I can't give investment advice.")).passed()


def test_model_level_refusal_counts() -> None:
    assert declined(make_log("Sorry", status="refused"))


def test_refusal_case_fails_if_it_answers_or_uses_tools() -> None:
    assert "refusal" in score(REFUSE_CASE, make_log("RWE looks like a great buy!")).failed_checks()
    with_tools = make_log("I can't advise.", [("get_generation", {})])
    assert "tool_use" in score(REFUSE_CASE, with_tools).failed_checks()


NO_DATA_CASE = EvalCase(
    id="n",
    category="missing_data",
    question="Price on 10 Nov?",
    outcome="no_data",
    oracle_answer="x",
)


def test_no_data_detection() -> None:
    calls = [("get_day_ahead_price", {"start": "2026-11-10"})]
    assert score(NO_DATA_CASE, make_log("Data for 10 November is not available.", calls)).passed()
    invented = make_log("The average price on 10 November was 150 EUR/MWh.", calls)
    assert "no_data" in score(NO_DATA_CASE, invented).failed_checks()


def test_grounding_detects_wrong_data_even_if_answer_matches() -> None:
    lucky = make_log(GOOD, GOOD_CALLS, result='{"value": 150.0}')  # tools said 150
    checks = score(PRICE_CASE, lucky)
    assert checks.numbers is True and checks.grounded is False
