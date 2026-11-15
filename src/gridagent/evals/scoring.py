"""Rule-based scoring of one agent run against one eval case.

Each check is a small, explicit rule, so a failure is easy to explain:
- numbers:   every expected number appears in the answer (within tolerance)
- supported: every number in the answer is backed by a tool result or the question
             (no arithmetic of its own; see tools/grounding.py)
- grounded:  every expected number also appears in a tool result, i.e. the agent
             fetched and computed the right data (separates "wrong data" from
             "right data, reported wrongly"; in oracle mode this tests the tools)
- dates:     every expected date is mentioned (ISO, "5 October", "Oct 5", ...)
- tool_use:  every expected tool call was made with the expected args;
             refuse cases must make no tool calls at all
- refusal:   the agent declined exactly when it should (no over-refusal)
- no_data:   the answer says the data is not available
- source:    factual answers cite Energy-Charts / SMARD
- no_error:  the run finished without an error status

Checks that don't apply to a case are `None` and are left out of its metrics.
Known limitation: text matching is heuristic. Failures are listed per case in
the report, so a wrong verdict can be spotted and the rule improved.
"""

import calendar
import re
from datetime import date
from typing import Any

from pydantic import BaseModel

from gridagent.agent.runlog import RunLog
from gridagent.evals.dataset import EvalCase, ExpectedNumber, ExpectedToolCall
from gridagent.tools.grounding import extract_numbers

DECLINE_PHRASES = (
    "can't",
    "cannot",
    "can not",
    "unable",
    "not able",
    "won't",
    "outside",
    "only answer",
    "only help",
    "only have data",
    "not something i can",
    "i don't provide",
    "i do not provide",
)
NO_DATA_PHRASES = (
    "not available",
    "no data",
    "unavailable",
    "don't have data",
    "do not have data",
    "no recorded",
    "isn't available",
    "is not yet available",
    "in the future",
    "has not happened",
    "hasn't happened",
    "not yet",
    "only available up to",
    "up to yesterday",
)
SOURCE_PHRASES = (
    "energy-charts", "smard", "fraunhofer", "umweltbundesamt", "uba", "bdew",
    "netztransparenz", "energiebilanzen", "dwd", "wetterdienst", "marktstammdatenregister",
    "mastr",
)  # fmt: skip
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


class CheckResults(BaseModel):
    numbers: bool | None = None
    supported: bool | None = None
    grounded: bool | None = None
    dates: bool | None = None
    tool_use: bool | None = None
    refusal: bool | None = None
    no_data: bool | None = None
    source: bool | None = None
    no_error: bool = True

    def passed(self) -> bool:
        return all(v is not False for v in self.model_dump().values())

    def failed_checks(self) -> list[str]:
        return [name for name, v in self.model_dump().items() if v is False]


# ---------- text helpers ----------


# Tool output is exact (rounded to 2 decimals), so grounding uses a much tighter
# tolerance than answers, where the model may round ("about 23%").
GROUNDING_TOLERANCE_PCT = 0.05
GROUNDING_TOLERANCE_ABS = 0.01


def number_present(expected: ExpectedNumber, found: list[float], strict: bool = False) -> bool:
    """True if any accepted value appears in `found` within tolerance."""
    for target in expected.values:
        if strict:
            allowed = max(abs(target) * GROUNDING_TOLERANCE_PCT / 100, GROUNDING_TOLERANCE_ABS)
        else:
            allowed = max(abs(target) * expected.tolerance_pct / 100, expected.tolerance_abs)
        if any(abs(x - target) <= allowed for x in found):
            return True
    return False


def date_mentioned(text: str, day: date) -> bool:
    """ISO, numeric (05.10.2026) or month name + day number ('5 October', 'Oct 5', '2–8 Nov')."""
    lower = text.lower()
    if day.isoformat() in lower or f"{day.day:02d}.{day.month:02d}." in lower:
        return True
    month = MONTHS[day.month - 1]
    has_month = month in lower or re.search(rf"\b{month[:3]}\b", lower) is not None
    has_day = re.search(rf"(?<![\d.]){day.day}(?:st|nd|rd|th)?(?![\d])", lower) is not None
    return has_month and has_day


def whole_month_named(text: str, days: list[date]) -> bool:
    """'October 2026' covers expected dates that are exactly a month's first and last day."""
    if len(days) != 2 or (days[0].year, days[0].month) != (days[1].year, days[1].month):
        return False
    last_day = calendar.monthrange(days[0].year, days[0].month)[1]
    if (days[0].day, days[1].day) != (1, last_day):
        return False
    month = MONTHS[days[0].month - 1]
    return re.search(rf"\b{month}\s+{days[0].year}\b", text.lower()) is not None


def contains_any(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    lower = text.lower().replace("’", "'")
    return any(p.lower() in lower for p in phrases)


# ---------- tool calls ----------


def _same(expected: Any, actual: Any) -> bool:
    """Equal values; for lists, the actual call must include every expected item."""
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return {str(e).lower() for e in expected} <= {str(a).lower() for a in actual}
    return str(expected).lower() == str(actual).lower()


# Refs are internal ids that depend on fetch order, so they are never compared.
# compare_values inputs are free-form (order, labels, unit spelling); whether the
# comparison was right is checked through the numbers in the answer instead.
# compute_when conditions carry refs and free threshold choices; checked via the numbers.
IGNORED_ARGS = {"ref", "weight_ref", "a", "b", "a_label", "b_label", "unit", "conditions"}


def call_matches(expected: ExpectedToolCall, actual: dict[str, Any]) -> bool:
    if actual["name"] != expected.name:
        return False
    return all(
        _same(v, actual["input"].get(k)) for k, v in expected.args.items() if k not in IGNORED_ARGS
    )


def tool_calls_ok(expected: list[ExpectedToolCall], actual: list[dict[str, Any]]) -> bool:
    """Every expected call is matched by some actual call (order and extras don't matter)."""
    return all(any(call_matches(e, a) for a in actual) for e in expected)


# ---------- the scorer ----------


def declined(log: RunLog) -> bool:
    """The agent refused: a model-level refusal, or no tools used plus decline wording."""
    if log.status == "refused":
        return True
    return not log.tool_calls and contains_any(log.final_answer, DECLINE_PHRASES)


def score(case: EvalCase, log: RunLog) -> CheckResults:
    answer = log.final_answer
    calls = [{"name": c.name, "input": c.input} for c in log.tool_calls]
    refused = declined(log)
    checks = CheckResults(no_error=log.status in ("ok", "refused"))

    if case.outcome == "refuse":
        checks.refusal = refused
        checks.tool_use = not calls
        return checks

    if case.outcome == "refuse_or_no_data":
        checks.refusal = refused or contains_any(answer, NO_DATA_PHRASES)
        return checks

    checks.refusal = not refused  # must not decline an answerable question
    if case.tool_calls:
        checks.tool_use = tool_calls_ok(case.tool_calls, calls)

    if case.outcome == "no_data":
        checks.no_data = contains_any(answer, NO_DATA_PHRASES)
        return checks

    # outcome == "answer"
    found = extract_numbers(answer)
    tool_numbers = [x for c in log.tool_calls if not c.is_error for x in extract_numbers(c.result)]
    if case.numbers:
        checks.numbers = all(number_present(n, found) for n in case.numbers)
        checks.grounded = all(number_present(n, tool_numbers, strict=True) for n in case.numbers)
    if case.dates or case.mentions:
        dates_ok = all(date_mentioned(answer, d) for d in case.dates) or whole_month_named(
            answer, case.dates
        )
        mentions_ok = all(contains_any(answer, group) for group in case.mentions)
        checks.dates = dates_ok and mentions_ok
    if log.unsupported_numbers is not None:
        checks.supported = not log.unsupported_numbers
    checks.source = contains_any(answer, SOURCE_PHRASES)
    return checks
