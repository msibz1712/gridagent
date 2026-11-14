"""SMARD client: week chunking, MWh -> MW conversion and consistency checks on real data."""

from datetime import date

import pytest

from gridagent.data.client import NoDataError
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.smard import REGIONS, SmardClient, chunk_path, week_start_ms

from conftest import FIXTURE_DIR, load_response

WEEK = (date(2026, 11, 2), date(2026, 11, 8))
source = FixtureDataSource(FIXTURE_DIR)
TSOS = [r for r in REGIONS if r != "DE"]


def test_week_start_is_monday_midnight_berlin() -> None:
    assert week_start_ms(date(2026, 11, 2)) == 1793574000000  # verified against SMARD's index
    assert week_start_ms(date(2026, 11, 8)) == 1793574000000  # Sunday -> same week
    assert week_start_ms(date(2026, 11, 9)) == 1793574000000 + 7 * 86_400_000


def test_chunk_path() -> None:
    assert chunk_path(4068, "DE", 1) == "/4068/DE/4068_DE_quarterhour_1.json"


def test_values_converted_to_mw_match_energy_charts_exactly() -> None:
    """SMARD gives MWh per quarter-hour; x4 must equal Energy-Charts' MW values."""
    smard = source.regional("DE", ["solar", "load"], *WEEK)
    ec = load_response("public_power_de_2026-11-02_2026-11-08.json")
    assert smard.unit == "MW" and smard.interval_minutes == 15
    assert len(smard.data) == len(ec.data) == 672
    for s_row, e_row in zip(smard.data, ec.data, strict=True):
        assert s_row.timestamp == e_row.timestamp
        assert s_row.values["solar"] == pytest.approx(e_row.values["solar"], abs=0.1)
        assert s_row.values["load"] == pytest.approx(e_row.values["load"], abs=0.1)


@pytest.mark.parametrize("series", ["solar", "wind_onshore", "load"])
def test_control_areas_add_up_to_germany(series: str) -> None:
    germany = source.regional("DE", [series], *WEEK)
    areas = [source.regional(r, [series], *WEEK) for r in TSOS]
    for i, row in enumerate(germany.data):
        total = sum(a.data[i].values[series] for a in areas)
        assert total == pytest.approx(row.values[series], abs=0.5)


def test_rows_are_cut_to_the_requested_days() -> None:
    one_day = source.regional("TenneT", ["solar"], date(2026, 11, 5), date(2026, 11, 5))
    assert len(one_day.data) == 96
    assert {r.timestamp.date() for r in one_day.data} == {date(2026, 11, 5)}
    assert one_day.attributes["region"] == "TenneT"
    assert "SMARD" in one_day.license


def test_missing_week_is_no_data() -> None:
    with pytest.raises(NoDataError):
        source.regional("DE", ["solar"], date(2026, 11, 20), date(2026, 11, 21))


def test_all_null_values_are_no_data() -> None:
    client = SmardClient(get_json=lambda path, end: {"series": [[1793574000000, None]]})
    with pytest.raises(NoDataError):
        client.regional("DE", ["solar"], date(2026, 11, 2), date(2026, 11, 2))


@pytest.mark.parametrize(
    ("region", "sources", "message"),
    [("Bavaria", ["solar"], "Unknown region"), ("DE", ["nuclear_fusion"], "Unknown SMARD source")],
)
def test_validation(region: str, sources: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        source.regional(region, sources, *WEEK)


def test_requests_only_the_weeks_needed() -> None:
    requested = []

    def fake(path: str, end: date) -> dict:
        requested.append(path)
        friday_6_nov = week_start_ms(date(2026, 11, 2)) + 4 * 86_400_000
        return {"series": [[friday_6_nov, 1.0]]}

    SmardClient(get_json=fake).regional("DE", ["solar"], date(2026, 11, 6), date(2026, 11, 10))
    assert len(requested) == 2  # the week of 2 Nov and the week of 9 Nov
