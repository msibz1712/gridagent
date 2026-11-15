"""Neighbouring bidding zones: prices, spreads and the zone guard."""

import json
from datetime import date

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

WEEK = {"start": "2026-11-02", "end": "2026-11-08"}
STATS = {"relative_to": None, "per_day": False, "threshold": None,
         "weight_ref": None, "weight_source": None}  # fmt: skip


def toolbox(data=None) -> ToolBox:
    return ToolBox(data or FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))


def mean_of(box: ToolBox, ref: str, source: str | None) -> float:
    args = {"ref": ref, "operation": "mean", "source": source, **STATS}
    return json.loads(box.execute("compute_stats", args).content)["value"]


def test_spread_is_germany_minus_neighbour() -> None:
    box = toolbox()
    ref = json.loads(box.execute("get_price_spread", {"zone": "PL", **WEEK}).content)["ref"]
    first = box.store[ref].data[0].values
    assert first["spread"] == round(first["germany"] - first["neighbour"], 2)
    assert (
        mean_of(box, ref, "spread") < 0
    )  # Poland was dearer than Germany that week (271.83 vs 256.59)


def test_neighbour_price_is_a_normal_price_ref() -> None:
    box = toolbox()
    summary = json.loads(box.execute("get_neighbour_price", {"zone": "AT", **WEEK}).content)
    assert summary["ref"].startswith("price_") and summary["zone"] == "AT (Austria)"
    assert mean_of(box, summary["ref"], None) == 286.54


def test_only_openly_licensed_neighbour_zones() -> None:
    result = toolbox().execute("get_neighbour_price", {"zone": "ES", **WEEK})
    assert result.is_error  # Spain: not a neighbour and not in the CC BY list


def test_a_zone_is_never_answered_with_another_zones_prices() -> None:
    """A source that ignores the zone (the bug fixed in the database layer) is caught."""

    class IgnoresZone(FixtureDataSource):
        def day_ahead_price(self, start, end, bzn="DE-LU"):
            return super().day_ahead_price(start, end)  # always Germany

    result = toolbox(IgnoresZone(FIXTURE_DIR)).execute(
        "get_neighbour_price", {"zone": "FR", **WEEK}
    )
    assert result.is_error and "Expected FR prices, got DE-LU" in result.content


def test_no_recorded_zone_means_no_data() -> None:
    result = toolbox().execute("get_neighbour_price", {"zone": "CH", **WEEK})
    assert result.is_error and "No data" in result.content


def test_spread_conditions_and_counts_are_spelled_out() -> None:
    """A live run flipped the sign (spread < 0 read as 'France cheaper'): the tools now say it."""
    box = toolbox()
    spread = json.loads(box.execute("get_price_spread", {"zone": "FR", **WEEK}).content)
    assert (spread["hours_germany_dearer"], spread["hours_germany_cheaper"],
            spread["hours_equal_price"]) == (39.5, 87.0, 41.5)  # fmt: skip
    flows = json.loads(box.execute("get_cross_border_flows", {**WEEK, "kind": "trading"}).content)
    cheaper = {"ref": spread["ref"], "source": "spread", "below": 0, "above": None}
    args = {"operation": "hours", "ref": flows["ref"], "source": "france", "conditions": [cheaper]}
    result = json.loads(box.execute("compute_when", args).content)
    assert result["conditions"] == ["spread < 0 EUR / MWh (Germany cheaper than FR)"]


def test_numbers_from_the_systems_own_instructions_count_as_backed() -> None:
    from test_loop import AGENTS_DIR

    from gridagent.agent.loop import Agent
    from gridagent.garden.registry import resolve
    from gridagent.llm.fake import FakeLLM, text

    agent = resolve(AGENTS_DIR, "energy_qa@0.8.0")
    answer = "Station wind is not hub-height wind (100–150 m); I use 3 m/s as 'little wind'."
    runner = Agent(agent, FakeLLM([[text(answer)]]), FixtureDataSource(FIXTURE_DIR),
                   lambda: date(2026, 11, 14))  # fmt: skip
    assert runner.run("How windy was it?").unsupported_numbers == []
