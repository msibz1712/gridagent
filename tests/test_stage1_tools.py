"""Stage 1 tools and calculations on real fixtures: cross-border flows, regional
(SMARD) data, installed capacity, capacity factors, capture prices, threshold
counts and official reference statistics."""

import json
from datetime import date, datetime, timedelta

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Point, Series
from gridagent.data.reference import load_reference_data, lookup
from gridagent.tools.definitions import ALL_TOOLS
from gridagent.tools.stats import Operation, capacity_factor, compute_stats, daily
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

TODAY = date(2026, 11, 14)
WEEK = {"start": "2026-11-02", "end": "2026-11-08"}
OCTOBER = {"start": "2026-10-01", "end": "2026-10-31"}
T0 = datetime.fromisoformat("2026-11-10T00:00:00+01:00")


@pytest.fixture
def box() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: TODAY)


def ok(box: ToolBox, name: str, **tool_input) -> dict:
    result = box.execute(name, tool_input)
    assert not result.is_error, result.content
    return json.loads(result.content)


def err(box: ToolBox, name: str, **tool_input) -> str:
    result = box.execute(name, tool_input)
    assert result.is_error
    return result.content


def stats(box: ToolBox, **args) -> dict:
    defaults = {
        "source": None,
        "relative_to": None,
        "per_day": False,
        "threshold": None,
        "weight_ref": None,
        "weight_source": None,
    }
    return ok(box, "compute_stats", **(defaults | args))


def series(values, unit="MW", sid="x", interval=15) -> Series:
    pts = [Point(timestamp=T0 + timedelta(minutes=15 * i), value=v) for i, v in enumerate(values)]
    return Series(id=sid, unit=unit, interval_minutes=interval, points=pts)


# ---------- pure calculations ----------


def test_energy_of_gw_series_is_gwh() -> None:
    result = compute_stats(series([4, 4, 4, 4], unit="GW"), Operation.ENERGY)
    assert result.value == 4.0 and result.unit == "GWh"


def test_count_below_and_above_threshold_in_hours() -> None:
    prices = series([-5, -0.01, 0, 10, 400, 500], unit="EUR / MWh")
    below = compute_stats(prices, Operation.COUNT_BELOW, threshold=0)
    above = compute_stats(prices, Operation.COUNT_ABOVE, threshold=300)
    assert below.value == 0.5 and below.unit == "h"  # 2 quarter-hours, 0 is not < 0
    assert above.value == 0.5 and above.detail["intervals"] == 2


def test_count_needs_threshold() -> None:
    with pytest.raises(ValueError, match="threshold"):
        compute_stats(series([1]), Operation.COUNT_BELOW)


def test_capture_price_is_generation_weighted() -> None:
    price = series([100, 0, 200], unit="EUR / MWh", sid="price")
    solar = series([0, 10, 30], sid="solar")  # night hour has no weight
    result = compute_stats(price, Operation.CAPTURE_PRICE, weights=solar)
    assert result.value == pytest.approx((0 * 10 + 200 * 30) / 40)


def test_capacity_factor() -> None:
    gen = series([50, 50, 50, 50])  # 1 hour at 50 MW = 50 MWh
    result = capacity_factor(gen, capacity_mw=100, capacity_note="test")
    assert result.value == pytest.approx(50.0) and result.unit == "%"
    assert result.detail["hours"] == 1.0


def test_per_day_rejected_for_new_operations() -> None:
    with pytest.raises(ValueError, match="per_day"):
        daily(series([1, 2]), Operation.COUNT_BELOW)


# ---------- tools on real fixtures ----------


def test_tool_schemas_are_strict() -> None:
    for tool in ALL_TOOLS.values():
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False  # strictness: see test_toolbox.py
        assert set(schema["required"]) == set(schema["properties"])


def test_net_imports_physical_vs_trading(box: ToolBox) -> None:
    physical = ok(box, "get_cross_border_flows", kind="physical", **WEEK)
    trading = ok(box, "get_cross_border_flows", kind="trading", **WEEK)
    assert physical["ref"] == "flow_1" and "positive = import" in physical["sign_convention"]
    p = stats(box, ref=physical["ref"], operation="energy", source="sum")
    t = stats(box, ref=trading["ref"], operation="energy", source="sum")
    assert p["value"] == pytest.approx(95.69, abs=0.01) and p["unit"] == "GWh"
    assert t["value"] == pytest.approx(33.99, abs=0.01)
    poland = stats(box, ref=physical["ref"], operation="energy", source="poland")
    assert poland["value"] < 0  # net export to Poland


def test_regional_generation(box: ToolBox) -> None:
    summary = ok(box, "get_regional_generation", region="TenneT", sources=["solar", "load"], **WEEK)
    assert summary["ref"] == "region_1" and "SMARD" in summary["data_source"]
    solar = stats(box, ref=summary["ref"], operation="energy", source="solar")
    assert solar["value_gwh"] == pytest.approx(253.38, abs=0.01)
    assert "not in this ref" in err(
        box,
        "compute_stats",
        ref="region_1",
        operation="mean",
        source="gas",
        relative_to=None,
        per_day=False,
        threshold=None,
        weight_ref=None,
        weight_source=None,
    )


def test_regional_rejects_too_many_sources(box: ToolBox) -> None:
    sources = ["solar", "load", "wind_onshore", "gas"]
    assert "Invalid input" in err(
        box, "get_regional_generation", region="DE", sources=sources, **WEEK
    )


def test_total_scope_gives_lower_renewable_share(box: ToolBox) -> None:
    public = ok(box, "get_generation", sources=None, scope=None, **WEEK)
    total = ok(box, "get_generation", sources=None, scope="total", **WEEK)
    assert total["scope"] == "total" and "industrial" in total["data_source"]
    share_public = stats(box, ref=public["ref"], operation="share", source="renewable")
    share_total = stats(box, ref=total["ref"], operation="share", source="renewable")
    assert share_public["value"] == pytest.approx(42.27, abs=0.01)
    assert share_total["value"] == pytest.approx(37.51, abs=0.01)


def test_installed_capacity_with_targets(box: ToolBox) -> None:
    table = ok(box, "get_installed_capacity", time_step="yearly", technologies=["solar"])
    by_year = {row["period_end_of"]: row for row in table["values"]}
    assert by_year["2026"]["solar_ac"] == 120.981 and by_year["2026"]["solar_dc"] == 133.003
    assert by_year["2030"]["solar_planned_eeg_2023"] == 215.0
    assert table["units"]["solar_ac"] == "GW"


def test_capacity_factor_october_wind(box: ToolBox) -> None:
    ref = ok(box, "get_generation", sources=["wind_onshore"], scope=None, **OCTOBER)["ref"]
    result = stats(box, ref=ref, operation="capacity_factor", source="wind_onshore")
    assert result["value"] == pytest.approx(21.71, abs=0.01)
    assert "end of 2026-09" in result["detail"]["capacity_used"]
    assert result["detail"]["hours"] == 745.0  # October has a 25-hour day


def test_capacity_factor_solar_uses_ac_capacity(box: ToolBox) -> None:
    ref = ok(box, "get_generation", sources=["solar"], scope=None, **OCTOBER)["ref"]
    result = stats(box, ref=ref, operation="capacity_factor", source="solar")
    assert "AC" in result["detail"]["capacity_used"]
    assert result["value"] == pytest.approx(5.75, abs=0.01)


def test_capture_price_october_matches_official_market_value(box: ToolBox) -> None:
    price = ok(box, "get_day_ahead_price", **OCTOBER)["ref"]
    gen = ok(box, "get_generation", sources=["solar"], scope=None, **OCTOBER)["ref"]
    result = stats(box, ref=price, operation="capture_price", weight_ref=gen, weight_source="solar")
    assert result["value"] == pytest.approx(134.49, abs=0.01)
    official = lookup("market_value_monthly", "2026-10")["values"]
    solar_official = next(v["value"] for v in official if v["key"] == "solar")
    assert result["value_ct_per_kwh"] == pytest.approx(solar_official, rel=0.01)  # within 1%


def test_negative_price_hours_october(box: ToolBox) -> None:
    price = ok(box, "get_day_ahead_price", **OCTOBER)["ref"]
    result = stats(box, ref=price, operation="count_below", threshold=0)
    assert result["value"] == 1.75 and result["unit"] == "h"


def test_capture_price_errors(box: ToolBox) -> None:
    gen = ok(box, "get_generation", sources=None, scope=None, **WEEK)["ref"]
    message = err(
        box,
        "compute_stats",
        ref=gen,
        operation="capture_price",
        source=None,
        relative_to=None,
        per_day=False,
        threshold=None,
        weight_ref=gen,
        weight_source="solar",
    )
    assert "price ref" in message


def test_reference_statistics(box: ToolBox) -> None:
    co2 = ok(
        box, "get_reference_statistics", dataset="co2_intensity_electricity_mix", period="2025"
    )
    assert co2["values"] == [{"period": "2025", "value": 344.0, "status": "estimate"}]
    assert "consumed in Germany" in co2["definition"]
    assert co2["source"]["url"].startswith("https://www.umweltbundesamt.de")
    assert "Available periods" in err(
        box, "get_reference_statistics", dataset="co2_intensity_electricity_mix", period="1999"
    )


def test_reference_file_is_valid_and_fully_cited() -> None:
    data = load_reference_data()
    assert len(data.ids()) == len(set(data.ids()))
    for dataset in data.datasets:
        assert dataset.source.url.startswith("https://")
        assert len(dataset.definition) > 40  # every figure explains what it measures
