"""The eval set must agree with independently computed values from the raw fixtures.

If someone edits evals.yaml, or a fixture changes, this fails, so expected
answers can't silently drift from the data.
"""

from pathlib import Path

import pytest

from gridagent.data.reference import lookup
from gridagent.evals.dataset import load_eval_set

import independent_calc as ic

EVAL_PATH = Path(__file__).parent.parent / "agents" / "energy_qa" / "evals.yaml"
GEN_WEEK = ic.rows("public_power_de_2026-11-02_2026-11-08.json")
GEN_OCT = ic.rows("public_power_de_2026-10-01_2026-10-31.json")
PRICE_YDAY = ic.rows("price_DE-LU_2026-11-13.json")
PRICE_WEEK = ic.rows("price_DE-LU_2026-11-02_2026-11-08.json")
PRICE_OCT = ic.rows("price_DE-LU_2026-10-01_2026-10-31.json")
P = "day_ahead_price"
CBPF = "cbpf_de_2026-11-02_2026-11-08.json"


def _installed(year: str, column: str) -> float:
    rows = ic.rows("installed_power_de_yearly.json")
    return next(r for r in rows if r["timestamp"].startswith(year))["values"][column]


def _reference(dataset: str, period: str) -> float:
    return lookup(dataset, period)["values"][0]["value"]


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
    # ---- Stage 1 (0.3.0) ----
    "net_imports_physical_last_week": lambda: ic.flow_energy_gwh(CBPF, "sum"),
    "physical_vs_commercial_imports": lambda: ic.flow_energy_gwh(CBPF, "sum"),
    "poland_exchange_last_week": lambda: -ic.flow_energy_gwh(CBPF, "poland"),
    "regional_solar_tennet": lambda: sum(ic.smard_week(4068, "TenneT")) / 1000,
    "regional_wind_largest_area": lambda: max(
        sum(ic.smard_week(4067, r)) / 1000 for r in ("50Hertz", "Amprion", "TenneT", "TransnetBW")
    ),
    "capacity_factor_wind_onshore_october": lambda: (
        100 * ic.energy_mwh(GEN_OCT, "wind_onshore") / (71.336 * 1000 * len(GEN_OCT) * 0.25)
    ),
    "installed_solar_vs_2030_target": lambda: _installed("2026", "solar_ac"),
    "negative_price_hours_october": lambda: (
        sum(r["values"]["day_ahead_price"] < 0 for r in PRICE_OCT) * 0.25
    ),
    "hours_above_300_october": lambda: (
        sum(r["values"]["day_ahead_price"] > 300 for r in PRICE_OCT) * 0.25
    ),
    "solar_market_value_october": lambda: ic.capture_price(GEN_OCT, PRICE_OCT, "solar"),
    "renewable_share_total_scope": lambda: ic.share_pct(
        ic.rows("total_power_de_2026-11-02_2026-11-08.json"), "renewable"
    ),
    # ---- Conversations (0.4.0): the follow-up's expected number ----
    "followup_solar_after_wind": lambda: ic.share_pct(GEN_WEEK, "solar"),
    "followup_highest_price_same_day": lambda: ic.peak(PRICE_YDAY, P)[0],
    "followup_compare_control_area": lambda: sum(ic.smard_week(4068, "50Hertz")) / 1000,
    "followup_market_value_wind": lambda: ic.capture_price(GEN_OCT, PRICE_OCT, "wind_onshore"),
    # Published figures: checked against the curated reference file instead.
    "co2_factor_2025": lambda: _reference("co2_intensity_electricity_mix", "2025"),
    "official_renewable_share_2025": lambda: _reference("renewable_share_gross_generation", "2025"),
    "ambiguous_last_weekend": lambda: (
        ic.energy_mwh(ic.between(GEN_WEEK, "2026-11-07", "2026-11-08"), "wind") / 1000
    ),
}

eval_set, _ = load_eval_set(EVAL_PATH)
NUMERIC_CASES = [c for c in eval_set.cases if c.numbers]


def test_eval_set_size_and_coverage() -> None:
    # The spec asked for 20-30 core questions; Stage 1 adds 13 for the new data sources.
    core = [c for c in eval_set.cases if c.min_version is None]
    assert any(c.previous for c in eval_set.cases)  # conversations are covered
    assert 20 <= len(core) <= 30
    assert len(eval_set.cases) <= 50
    categories = {c.category for c in eval_set.cases}
    assert {"factual", "tool_use", "ambiguous", "missing_data", "future", "refusal"} <= categories


def test_every_numeric_case_has_an_independent_check() -> None:
    assert {c.id for c in NUMERIC_CASES} == set(INDEPENDENT)


@pytest.mark.parametrize("case", NUMERIC_CASES, ids=lambda c: c.id)
def test_expected_number_matches_independent_calculation(case) -> None:
    expected = case.numbers[0].values[0]
    # YAML values are rounded (e.g. to 0.1 GWh), so allow 0.05% or 0.01 absolute.
    assert expected == pytest.approx(INDEPENDENT[case.id](), rel=5e-4, abs=0.01)


# Cases with a second expected number: check that one independently too.
SECOND_NUMBERS = {
    "wind_onshore_vs_offshore_last_week": lambda: ic.energy_mwh(GEN_WEEK, "wind_offshore") / 1000,
    "physical_vs_commercial_imports": lambda: ic.flow_energy_gwh(
        "cbet_de_2026-11-02_2026-11-08.json", "sum"
    ),
    "installed_solar_vs_2030_target": lambda: _installed("2030", "solar_planned_eeg_2023"),
    "solar_market_value_october": lambda: next(
        v["value"]
        for v in lookup("market_value_monthly", "2026-10")["values"]
        if v["key"] == "solar"
    ),
}


@pytest.mark.parametrize("case_id", sorted(SECOND_NUMBERS))
def test_second_expected_numbers(case_id: str) -> None:
    case = next(c for c in eval_set.cases if c.id == case_id)
    assert case.numbers[1].values[0] == pytest.approx(SECOND_NUMBERS[case_id](), rel=5e-4, abs=0.05)
