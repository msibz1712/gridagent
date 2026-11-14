"""The eval set must agree with independently computed values from the raw fixtures.

If someone edits evals.yaml, or a fixture changes, this fails, so expected
answers can't silently drift from the data.
"""

from pathlib import Path

import pytest

from gridagent.evals.dataset import load_eval_set

import independent_calc as ic

EVAL_PATH = Path(__file__).parent.parent / "agents" / "energy_qa" / "evals.yaml"
GEN_WEEK = ic.rows("public_power_de_2026-11-02_2026-11-08.json")
GEN_OCT = ic.rows("public_power_de_2026-10-01_2026-10-31.json")
PRICE_YDAY = ic.rows("price_DE-LU_2026-11-13.json")
PRICE_WEEK = ic.rows("price_DE-LU_2026-11-02_2026-11-08.json")
PRICE_OCT = ic.rows("price_DE-LU_2026-10-01_2026-10-31.json")
P = "day_ahead_price"


def daily_best(data, fn):
    return max(fn(d) for d in ic.by_day(data).values())


# case id -> independently computed value of its FIRST expected number (first unit)
INDEPENDENT = {
    "price_mean_yesterday": lambda: ic.mean(PRICE_YDAY, P),
    "price_max_yesterday": lambda: ic.peak(PRICE_YDAY, P)[0],
    "price_min_yesterday": lambda: ic.peak(PRICE_YDAY, P, highest=False)[0],
    "price_mean_last_week": lambda: ic.mean(PRICE_WEEK, P),
    "price_most_expensive_day_last_week": lambda: daily_best(PRICE_WEEK, lambda d: ic.mean(d, P)),
    "price_max_october": lambda: ic.peak(PRICE_OCT, P)[0],
    "price_negative_october": lambda: ic.peak(PRICE_OCT, P, highest=False)[0],
    "wind_share_last_week": lambda: ic.share_pct(GEN_WEEK, "wind"),
    "renewable_share_last_week": lambda: ic.share_pct(GEN_WEEK, "renewable"),
    "renewable_share_of_load_last_week": lambda: ic.share_pct(GEN_WEEK, "renewable", of="load"),
    "solar_peak_october": lambda: ic.peak(GEN_OCT, "solar")[0],
    "sunniest_day_october": lambda: daily_best(GEN_OCT, lambda d: ic.energy_mwh(d, "solar")),
    "solar_share_october": lambda: ic.share_pct(GEN_OCT, "solar"),
    "solar_max_last_week": lambda: ic.peak(GEN_WEEK, "solar")[0],
    "lignite_energy_october": lambda: ic.energy_mwh(GEN_OCT, "fossil_brown_coal_lignite") / 1000,
    "gas_energy_october": lambda: ic.energy_mwh(GEN_OCT, "fossil_gas") / 1000,
    "wind_onshore_vs_offshore_last_week": lambda: ic.energy_mwh(GEN_WEEK, "wind_onshore") / 1000,
    "load_mean_last_week": lambda: ic.mean(GEN_WEEK, "load"),
    "ambiguous_october_no_year": lambda: ic.mean(PRICE_OCT, P),
    "ambiguous_last_weekend": lambda: (
        ic.energy_mwh(ic.between(GEN_WEEK, "2026-11-07", "2026-11-08"), "wind") / 1000
    ),
}

eval_set, _ = load_eval_set(EVAL_PATH)
NUMERIC_CASES = [c for c in eval_set.cases if c.numbers]


def test_eval_set_size_and_coverage() -> None:
    assert 20 <= len(eval_set.cases) <= 30
    categories = {c.category for c in eval_set.cases}
    assert {"factual", "tool_use", "ambiguous", "missing_data", "future", "refusal"} <= categories


def test_every_numeric_case_has_an_independent_check() -> None:
    assert {c.id for c in NUMERIC_CASES} == set(INDEPENDENT)


@pytest.mark.parametrize("case", NUMERIC_CASES, ids=lambda c: c.id)
def test_expected_number_matches_independent_calculation(case) -> None:
    expected = case.numbers[0].values[0]
    # YAML values are rounded (e.g. to 0.1 GWh), so allow 0.05% or 0.01 absolute.
    assert expected == pytest.approx(INDEPENDENT[case.id](), rel=5e-4, abs=0.01)


def test_second_number_onshore_offshore() -> None:
    case = next(c for c in eval_set.cases if c.id == "wind_onshore_vs_offshore_last_week")
    assert case.numbers[1].values[0] == pytest.approx(
        ic.energy_mwh(GEN_WEEK, "wind_offshore") / 1000, abs=0.05
    )
