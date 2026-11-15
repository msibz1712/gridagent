"""The eval set must agree with independently computed values from the raw fixtures.

If someone edits evals.yaml, or a fixture changes, this fails, so expected
answers can't silently drift from the data.
"""

from datetime import UTC, datetime, timedelta
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
W_WEEK = "weather_de_2026-11-02_2026-11-08.json"
W_OCT = "weather_de_2026-10-01_2026-10-31.json"
CALM, WINDY = [("wind_speed", 3, None)], [("wind_speed", None, 5)]
DARK_CALM = [("wind_speed", 3, None), ("solar_radiation", 50, None)]


PRICE_FR_WEEK = ic.rows("price_FR_2026-11-02_2026-11-08.json")


def _france_cheaper_quarter_hours() -> float:
    pairs = zip(PRICE_WEEK, PRICE_FR_WEEK, strict=True)
    return sum(de["values"][P] > fr["values"][P] for de, fr in pairs) * 0.25


def _commercial_imports_when_germany_dearer() -> float:
    de, fr = (
        ic.hourly(PRICE_WEEK, lambda r: r["values"][P]),
        ic.hourly(PRICE_FR_WEEK, lambda r: r["values"][P]),
    )
    flows = ic.hourly(ic.rows("cbet_de_2026-11-02_2026-11-08.json"),
                      lambda r: r["values"]["france"])  # fmt: skip
    return sum(flows[h] for h in de if de[h] - fr[h] > 0)  # GW x 1 h = GWh


_RD_LOWER = datetime(2026, 11, 1, 23, tzinfo=UTC)  # 2 Nov 00:00 in Berlin
_RD_UPPER = datetime(2026, 11, 8, 23, tzinfo=UTC)


def _curtailment_in_windy_hours() -> float:
    rows = ic.redispatch_rows()
    total = 0.0
    for hour in ic.hours_where(W_WEEK, [("wind_speed", None, 4)]):
        start = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        total += ic.redispatch_mwh(
            rows, start, start + timedelta(hours=1), "reduzieren", "Erneuerbar"
        )
    return total


def _hourly_price_october() -> dict[str, float]:
    return ic.hourly(PRICE_OCT, lambda r: r["values"][P])


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
    # ---- Comparisons (0.5.0) ----
    "solar_market_value_difference_official": lambda: (
        ic.capture_price(GEN_OCT, PRICE_OCT, "solar") / 10
    ),
    "price_week_vs_october": lambda: ic.mean(PRICE_WEEK, P),
    # compare_values works on the figures as reported (2 decimals), like a reader would
    "followup_wind_solar_ratio": lambda: (
        round(ic.share_pct(GEN_WEEK, "wind"), 2) / round(ic.share_pct(GEN_WEEK, "solar"), 2)
    ),
    # ---- Weather (0.6.0), hour by hour in UTC ----
    "wind_speed_last_week": lambda: sum(ic.weather_hours(W_WEEK, "wind_speed").values()) / 168,
    "price_calm_vs_windy_october": lambda: ic.mean_when(
        _hourly_price_october(), ic.hours_where(W_OCT, CALM)
    ),
    "dunkelflaute_hours_october": lambda: len(ic.hours_where(W_OCT, DARK_CALM)),
    "followup_gas_in_dunkelflaute": lambda: ic.mean_when(
        ic.hourly(GEN_OCT, lambda r: r["values"]["fossil_gas"]), ic.hours_where(W_OCT, DARK_CALM)
    ),
    # ---- Neighbouring markets (0.7.0) ----
    "price_germany_vs_france_last_week": lambda: ic.mean(PRICE_FR_WEEK, P),
    "hours_france_cheaper_last_week": _france_cheaper_quarter_hours,
    "followup_commercial_imports_from_france": _commercial_imports_when_germany_dearer,
    # ---- Congestion management (0.8.0), SYNTHETIC redispatch fixture ----
    "renewable_curtailment_last_week": lambda: ic.redispatch_mwh(
        ic.redispatch_rows(), _RD_LOWER, _RD_UPPER, "reduzieren", "Erneuerbar"
    ),
    "redispatch_by_operator_last_week": lambda: ic.redispatch_mwh(
        ic.redispatch_rows(), _RD_LOWER, _RD_UPPER, "reduzieren", tso="TenneT DE"
    ),
    "curtailment_in_windy_hours": _curtailment_in_windy_hours,
    # ---- Flexibility (0.9.0): forward pass over the raw prices ----
    "battery_last_week": lambda: ic.battery_profit(PRICE_WEEK, power=10, capacity=20),
    "battery_october_default_size": lambda: ic.battery_profit(PRICE_OCT, power=1, capacity=2),
    "flexible_load_yesterday": lambda: ic.cheapest_load(PRICE_YDAY, energy=20, power=5)[0],
    # ---- Price events (0.11.0): hour means, comparable days and the fit, from raw rows ----
    "investigate_october_peak": lambda: ic.event_evidence("2026-10-29", 17, 18)["event_price"],
    "investigate_saturday_midday": lambda: ic.event_evidence("2026-10-31", 12, 15)["event_price"],
    # ---- Plant register (0.10.0): raw rows of the MaStR excerpt ----
    "register_wind_nrw_since_2023": lambda: ic.register_mw(
        "wind_onshore", "Nordrhein-Westfalen", from_year=2023
    )[1],
    "register_lignite_top_district_nrw": lambda: ic.register_top_district(
        "lignite", "Nordrhein-Westfalen"
    )[1],
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
    assert len(eval_set.cases) <= 72  # kept small: every live run costs money
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
    "solar_market_value_october": lambda: _official_solar_october(),
    "solar_market_value_difference_official": lambda: _official_solar_october(),
    "price_week_vs_october": lambda: ic.mean(PRICE_OCT, P),
    "price_calm_vs_windy_october": lambda: ic.mean_when(
        _hourly_price_october(), ic.hours_where(W_OCT, WINDY)
    ),
    "price_germany_vs_france_last_week": lambda: ic.mean(PRICE_WEEK, P),
    "flexible_load_yesterday": lambda: ic.cheapest_load(PRICE_YDAY, energy=20, power=5)[1],
    "register_wind_nrw_since_2023": lambda: ic.register_mw("wind_onshore", "Nordrhein-Westfalen")[
        0
    ],
    "investigate_october_peak": lambda: ic.event_evidence("2026-10-29", 17, 18)["normal_price"],
    "investigate_saturday_midday": lambda: ic.event_evidence("2026-10-31", 12, 15)["normal_price"],
}


# Price events: the third expected number is the merit-order estimate.
EVENT_ESTIMATES = {
    "investigate_october_peak": ("2026-10-29", 17, 18),
    "investigate_saturday_midday": ("2026-10-31", 12, 15),
}


@pytest.mark.parametrize("case_id", sorted(EVENT_ESTIMATES))
def test_merit_order_estimates(case_id: str) -> None:
    case = next(c for c in eval_set.cases if c.id == case_id)
    expected = ic.event_evidence(*EVENT_ESTIMATES[case_id])["expected_price"]
    assert case.numbers[2].values[0] == pytest.approx(expected, abs=0.01)


def _official_solar_october() -> float:
    values = lookup("market_value_monthly", "2026-10")["values"]
    return next(v["value"] for v in values if v["key"] == "solar")


# Comparisons: the third expected number is the comparison itself, from reported figures.
def _reported(x: float, decimals: int = 2) -> float:
    return round(x, decimals)


THIRD_NUMBERS = {
    "solar_market_value_difference_official": lambda: (
        _reported(ic.capture_price(GEN_OCT, PRICE_OCT, "solar") / 10, 3) - _official_solar_october()
    ),
    "price_week_vs_october": lambda: (
        100
        * (_reported(ic.mean(PRICE_WEEK, P)) - _reported(ic.mean(PRICE_OCT, P)))
        / _reported(ic.mean(PRICE_OCT, P))
    ),
}


@pytest.mark.parametrize("case_id", sorted(THIRD_NUMBERS))
def test_comparison_numbers(case_id: str) -> None:
    case = next(c for c in eval_set.cases if c.id == case_id)
    assert case.numbers[2].values[0] == pytest.approx(THIRD_NUMBERS[case_id](), abs=0.005)


@pytest.mark.parametrize("case_id", sorted(SECOND_NUMBERS))
def test_second_expected_numbers(case_id: str) -> None:
    case = next(c for c in eval_set.cases if c.id == case_id)
    assert case.numbers[1].values[0] == pytest.approx(SECOND_NUMBERS[case_id](), rel=5e-4, abs=0.05)
