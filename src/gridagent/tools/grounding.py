"""Is every number in an answer backed by data? (one rule, used at run time and in evals)

The agent must take every number from a tool result and calculate only with tools.
This module checks that after the fact: each number in the answer has to match a
number the tools returned in this conversation, or one the user wrote.

A number counts as backed if it matches a tool number
- after ordinary rounding at the precision it is written with ("23%" for 23.31,
  "1,200" for 1,234 since trailing zeros mean "rounded to hundreds"), or
- after a factor of 1000 (MW <-> GW, MWh <-> GWh <-> TWh), the one unit
  conversion allowed without a tool, or
- with the opposite sign ("0.06 lower" for a difference of -0.06).
Dates, times, years and names that contain digits (50Hertz) are ignored.

Known limits (it is a heuristic, deliberately simple):
- Small whole numbers like "3" are matched easily; the check is strongest for the
  decimals that matter (prices, shares, differences).
- Arithmetic in words ("about a third", "twice as much") is not detected.
"""

import re

NUMBER = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?")
MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
NOT_QUANTITIES = [
    r"\d{4}-\d{2}-\d{2}(?:T[\d:.+-]+)?",  # 2026-11-13, ISO timestamps
    r"\b\d{1,2}:\d{2}\b",  # 23:45
    r"\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?",  # 05.10.2026
    rf"\b{_DAY}(?:\s*(?:–|-|to|and)\s*{_DAY})?\s+(?:{MONTHS})\b",  # 2–8 November
    rf"\b(?:{MONTHS})\.?\s+{_DAY}(?:\s*(?:–|-)\s*{_DAY})?\b",  # Nov 2–8
    r"\b(?:19|20)\d{2}\b",  # years
    r"\b50Hertz\b",
    r"\bCO2\b",
    r"\b\d+-(?:minute|hour|day)\b",  # 15-minute values
    r"\bQ[1-4]\b",
]
UNIT_FACTORS = (1.0, 1e-3, 1e-6, 1e3, 1e6)


def extract_numbers(text: str) -> list[float]:
    """All numbers in a text, e.g. '45,722 MW' -> 45722.0 and '-0.01' -> -0.01.

    A hyphen counts as a minus sign only when not preceded by a word character,
    so dates don't produce negative numbers.
    """
    return [value for value, _ in _numbers_with_text(text)]


def _numbers_with_text(text: str) -> list[tuple[float, str]]:
    found = []
    for match in NUMBER.findall(text):
        cleaned = match.replace(",", "").replace("−", "-")
        try:
            found.append((float(cleaned), cleaned))
        except ValueError:
            continue
    return found


def without_dates(text: str) -> str:
    """The text with dates, times, years and digit-containing names blanked out."""
    for pattern in NOT_QUANTITIES:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    return text


def rounding_tolerance(written: str) -> float:
    """Half a unit of the last written digit: '23.3' -> 0.05, '23' -> 0.5, '1200' -> 50."""
    digits = written.lstrip("-")
    if "." in digits:
        return 0.5 * 10 ** -len(digits.split(".")[1])
    trailing_zeros = len(digits) - len(digits.rstrip("0"))
    if trailing_zeros and digits.strip("0"):
        return 0.5 * 10**trailing_zeros
    return 0.5


def is_backed(value: float, written: str, pool: list[float]) -> bool:
    tolerance = rounding_tolerance(written) + 1e-9
    return any(
        abs(abs(value) - abs(source * factor)) <= tolerance
        for source in pool
        for factor in UNIT_FACTORS
    )


def unsupported_numbers(answer: str, tool_results: list[str], questions: list[str]) -> list[str]:
    """Numbers in `answer` (as written) that no tool result or question backs."""
    pool = [x for text in tool_results for x in extract_numbers(text)]
    pool += [x for text in questions for x in extract_numbers(without_dates(text))]
    return [
        written
        for value, written in _numbers_with_text(without_dates(answer))
        if not is_backed(value, written, pool)
    ]
