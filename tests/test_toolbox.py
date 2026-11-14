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


def test_definitions_are_strict_and_closed() -> None:
    for tool in ALL_TOOLS.values():
        assert tool["strict"] is True
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
    assert "nuclear" in error(
        box, "get_generation", start="2026-11-02", end="2026-11-08", sources=["nuclear"]
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
