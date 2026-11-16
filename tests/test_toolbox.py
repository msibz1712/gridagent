"""ToolBox: tool execution on fixture data, including every error path."""

import json
from datetime import date

import pytest

from gridagent.data.client import EnergyChartsError
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.tools.definitions import ALL_TOOLS
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

TODAY = date(2026, 11, 14)


@pytest.fixture
def box() -> ToolBox:
    return ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: TODAY)


def call(box: ToolBox, name: str, **tool_input) -> dict:
    result = box.execute(name, tool_input)
    assert not result.is_error, result.content
    return json.loads(result.content)


def error(box: ToolBox, name: str, **tool_input) -> str:
    result = box.execute(name, tool_input)
    assert result.is_error
    return result.content


def stats(box: ToolBox, ref: str, operation: str, source=None, relative_to=None, per_day=False):
    return call(
        box,
        "compute_stats",
        ref=ref,
        operation=operation,
        source=source,
        relative_to=relative_to,
        per_day=per_day,
    )


# Validated by the ToolBox only (see STRICT_TOOLS_VERIFIED_LIVE below for why).
NOT_STRICT = {
    "simulate_battery",
    "plan_flexible_load",
    "get_plant_register",
    "investigate_price_event",
    "get_power_forecast",
    "get_weather_outlook",
    "get_notable_events",
}


def test_definitions_are_strict_and_closed() -> None:
    for name, tool in ALL_TOOLS.items():
        assert tool["strict"] is (name not in NOT_STRICT), name
        schema = tool["input_schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])


def test_get_generation_returns_ref_not_data(box: ToolBox) -> None:
    summary = call(box, "get_generation", start="2026-11-02", end="2026-11-08", sources=["wind"])
    assert summary["ref"] == "gen_1"
    assert summary["n_intervals"] == 672
    assert "Energy-Charts" in summary["data_source"]
    assert len(json.dumps(summary)) < 1000  # small: the 672 x 21 values stay server-side


def test_wind_share_last_week(box: ToolBox) -> None:
    ref = call(box, "get_generation", start="2026-11-02", end="2026-11-08", sources=None)["ref"]
    result = stats(box, ref, "share", source="wind")
    assert result["value"] == pytest.approx(23.3, abs=0.05)
    assert result["unit"] == "%"
    assert result["period"] == "2026-11-02 to 2026-11-08"


def test_energy_reports_gwh(box: ToolBox) -> None:
    ref = call(box, "get_generation", start="2026-11-02", end="2026-11-08", sources=None)["ref"]
    result = stats(box, ref, "energy", source="wind")
    assert result["unit"] == "MWh"
    assert result["value_gwh"] == pytest.approx(result["value"] / 1000, abs=0.01)


def test_solar_max_october_has_timestamp(box: ToolBox) -> None:
    ref = call(box, "get_generation", start="2026-10-01", end="2026-10-31", sources=["solar"])[
        "ref"
    ]
    result = stats(box, ref, "max", source="solar")
    assert result["at"].startswith("2026-10-05T12:45")


def test_price_mean_yesterday(box: ToolBox) -> None:
    ref = call(box, "get_day_ahead_price", start="2026-11-13", end="2026-11-13")["ref"]
    assert ref == "price_1"
    result = stats(box, ref, "mean")
    assert result["value"] == pytest.approx(161.73, abs=0.01)
    assert result["unit"] == "EUR / MWh"


def test_per_day_results(box: ToolBox) -> None:
    ref = call(box, "get_day_ahead_price", start="2026-11-02", end="2026-11-08")["ref"]
    days = stats(box, ref, "mean", per_day=True)["per_day"]
    assert [d["period"] for d in days][0] == "2026-11-02 to 2026-11-02"
    assert len(days) == 7


def test_future_dates_are_reported_as_no_data(box: ToolBox) -> None:
    message = error(box, "get_generation", start="2026-12-01", end="2026-12-07", sources=None)
    assert "No data available" in message and "future" in message


def test_tomorrows_prices_are_allowed_but_day_after_is_not(box: ToolBox) -> None:
    # Tomorrow passes the date check (then the fixture source has no data for it).
    assert "future" not in error(box, "get_day_ahead_price", start="2026-11-15", end="2026-11-15")
    assert "future" in error(box, "get_day_ahead_price", start="2026-11-16", end="2026-11-16")


def test_period_too_long(box: ToolBox) -> None:
    assert "too long" in error(
        box, "get_generation", start="2026-01-01", end="2026-10-31", sources=None
    )


def test_start_after_end(box: ToolBox) -> None:
    assert "after" in error(box, "get_day_ahead_price", start="2026-11-08", end="2026-11-02")


def test_bad_date_format(box: ToolBox) -> None:
    assert "Invalid input" in error(box, "get_day_ahead_price", start="13.11.2026", end="x")


def test_unknown_ref_lists_known_refs(box: ToolBox) -> None:
    assert "Unknown ref" in error(
        box,
        "compute_stats",
        ref="gen_9",
        operation="mean",
        source="solar",
        relative_to=None,
        per_day=False,
    )


def test_unknown_source(box: ToolBox) -> None:
    assert "fusion" in error(
        box, "get_generation", start="2026-11-02", end="2026-11-08", sources=["fusion"]
    )


def test_generation_ref_requires_source(box: ToolBox) -> None:
    ref = call(box, "get_generation", start="2026-11-02", end="2026-11-08", sources=None)["ref"]
    assert "source is required" in error(
        box,
        "compute_stats",
        ref=ref,
        operation="mean",
        source=None,
        relative_to=None,
        per_day=False,
    )


def test_share_of_price_is_rejected(box: ToolBox) -> None:
    ref = call(box, "get_day_ahead_price", start="2026-11-13", end="2026-11-13")["ref"]
    assert "generation" in error(
        box,
        "compute_stats",
        ref=ref,
        operation="share",
        source=None,
        relative_to=None,
        per_day=False,
    )


def test_unknown_tool(box: ToolBox) -> None:
    assert "Unknown tool" in error(box, "delete_everything")


def test_data_source_outage_is_a_tool_error() -> None:
    class Broken:
        def public_power(self, start, end, country="de"):
            raise EnergyChartsError("HTTP 503")

        def day_ahead_price(self, start, end, bzn="DE-LU"):
            raise EnergyChartsError("HTTP 503")

    box = ToolBox(Broken(), today=lambda: TODAY)
    assert "unavailable" in error(box, "get_day_ahead_price", start="2026-11-13", end="2026-11-13")


# Keywords the API rejects in strict tool schemas (found the hard way: a live run of 0.6.0
# failed with "For 'array' type, property 'maxItems' is not supported"). Limits like these
# are enforced by the ToolBox's pydantic models instead.
UNSUPPORTED_IN_STRICT = {"maxItems", "minimum", "maximum", "minLength", "maxLength"}


def _keywords(schema) -> set[str]:
    if isinstance(schema, dict):
        found = set(schema)
        for value in schema.values():
            found |= _keywords(value)
        return found
    if isinstance(schema, list):
        return set().union(*(_keywords(v) for v in schema)) if schema else set()
    return set()


def test_strict_schemas_use_only_supported_keywords() -> None:
    from gridagent.tools.definitions import ALL_TOOLS

    for name, tool in ALL_TOOLS.items():
        bad = _keywords(tool["input_schema"]) & UNSUPPORTED_IN_STRICT
        assert not bad, f"{name} uses {bad}"


# The API also limits strict schemas to 16 parameters that can be null (type arrays or
# anyOf), counted over ALL tools of a request (found the hard way: a live smoke test of
# 0.10.0 failed with "too many parameters with union types (22 ...)").
MAX_UNION_PARAMETERS = 16


def _unions(schema) -> int:
    if isinstance(schema, dict):
        own = "anyOf" in schema or isinstance(schema.get("type"), list)
        return int(own) + sum(_unions(v) for v in schema.values())
    if isinstance(schema, list):
        return sum(_unions(v) for v in schema)
    return 0


def test_every_agent_version_stays_under_the_union_parameter_limit() -> None:
    from test_loop import AGENTS_DIR

    from gridagent.garden.registry import discover
    from gridagent.tools.definitions import ALL_TOOLS

    versions = [v.agent for entry in discover(AGENTS_DIR) for v in entry.versions if v.agent]
    assert versions
    for agent in versions:
        tools = [t for t in agent.config.tools if t in ALL_TOOLS]
        count = sum(_unions(ALL_TOOLS[t]["input_schema"]) for t in tools)
        assert count <= MAX_UNION_PARAMETERS, f"{agent.config.version}: {count}"


# Beyond the explicit limits, the API caps the size of the grammar compiled from all strict
# schemas of a request ("The compiled grammar is too large", live smoke test of 0.9.0).
# It can't be computed offline, so: no version may use more strict tools than the set
# last verified live. Grow this set only after a live smoke test passes.
STRICT_TOOLS_VERIFIED_LIVE = {
    "get_generation", "get_day_ahead_price", "get_cross_border_flows",
    "get_regional_generation", "get_installed_capacity", "get_reference_statistics",
    "compute_stats", "compare_values", "get_weather", "compute_when", "get_neighbour_price",
    "get_price_spread", "get_redispatch",
}  # fmt: skip


def test_no_version_uses_unverified_strict_tools() -> None:
    from test_loop import AGENTS_DIR

    from gridagent.garden.registry import discover
    from gridagent.tools.definitions import ALL_TOOLS

    for entry in discover(AGENTS_DIR):
        for version in entry.versions:
            if version.agent is None:
                continue
            strict = {t for t in version.agent.config.tools if ALL_TOOLS[t].get("strict")}
            assert strict <= STRICT_TOOLS_VERIFIED_LIVE, f"{version.version}: {strict}"


@pytest.mark.parametrize("name", sorted(NOT_STRICT))
def test_non_strict_tools_reject_unknown_fields(box: ToolBox, name: str) -> None:
    """Without grammar enforcement, the ToolBox's own validation is the guard."""
    result = box.execute(name, {"ref": "price_1", "surprise": 1})
    assert result.is_error and "Invalid input" in result.content


def test_an_agent_without_tools_gets_none_not_all() -> None:
    """Tool-less agents (the news writer) are allowed; [] must never mean 'every tool'."""
    from gridagent.data.fixture_source import FixtureDataSource

    from conftest import FIXTURE_DIR

    assert ToolBox(FixtureDataSource(FIXTURE_DIR), today=date.today, enabled=[]).definitions() == []
