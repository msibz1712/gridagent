"""Source grouping: our totals and shares must match Energy-Charts' definitions."""

from datetime import datetime

import pytest

from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse
from gridagent.data.sources import (
    GENERATION_SERIES,
    NON_GENERATION_SERIES,
    source_series,
)


def test_every_api_series_is_classified(week_power: TimeSeriesResponse) -> None:
    """Fails if Energy-Charts adds a series we haven't decided how to treat."""
    unclassified = set(week_power.series_ids()) - GENERATION_SERIES - NON_GENERATION_SERIES
    assert unclassified == set()


def test_renewable_share_matches_energy_charts(week_power: TimeSeriesResponse) -> None:
    """Our definition reproduces the API's own `renewable_share_of_generation` (±0.15 pp)."""
    ours = source_series(week_power, "renewable").points
    total = source_series(week_power, "total").points
    theirs = week_power.get_series("renewable_share_of_generation").points
    for r, t, api in zip(ours, total, theirs, strict=True):
        assert 100 * r.value / t.value == pytest.approx(api.value, abs=0.15)


def test_wind_is_onshore_plus_offshore(week_power: TimeSeriesResponse) -> None:
    wind = source_series(week_power, "wind").points[0].value
    row = week_power.data[0].values
    assert wind == pytest.approx(row["wind_onshore"] + row["wind_offshore"])


def test_total_excludes_non_generation_and_pumped_storage(week_power: TimeSeriesResponse) -> None:
    row = week_power.data[0].values
    expected = (
        sum(v for k, v in row.items() if k in GENERATION_SERIES) - row["hydro_pumped_storage"]
    )
    assert source_series(week_power, "total").points[0].value == pytest.approx(expected)


def test_missing_component_makes_point_missing() -> None:
    response = TimeSeriesResponse(
        endpoint="public_power",
        timezone="Europe/Berlin",
        unit="MW",
        interval_minutes=15,
        series=[
            SeriesInfo(id="wind_onshore", name="On"),
            SeriesInfo(id="wind_offshore", name="Off"),
        ],
        data=[
            DataRow(
                timestamp=datetime.fromisoformat("2026-11-01T00:00:00+01:00"),
                values={"wind_onshore": 100.0, "wind_offshore": None},
            )
        ],
    )
    assert source_series(response, "wind").points[0].value is None


def test_unknown_source_lists_choices(week_power: TimeSeriesResponse) -> None:
    with pytest.raises(KeyError, match="Choose from"):
        source_series(week_power, "fusion")


# ---------- historical data: nuclear (until April 2023), changing series ----------

from conftest import load_response  # noqa: E402


def test_2022_renewable_share_matches_energy_charts_with_nuclear_in_total() -> None:
    """Before April 2023 total generation must include nuclear (verified: 0.17 pp vs 3.6 pp)."""
    day = load_response("public_power_de_2022-06-01_2022-06-01.json")
    ours = source_series(day, "renewable").points
    total = source_series(day, "total").points
    theirs = day.get_series("renewable_share_of_generation").points
    for r, t, api in zip(ours, total, theirs, strict=True):
        assert 100 * r.value / t.value == pytest.approx(api.value, abs=0.2)


def test_series_missing_in_a_period_count_as_zero() -> None:
    day = load_response("public_power_de_2022-06-01_2022-06-01.json")
    assert "fossil_coal_derived_gas" not in day.series_ids()
    fossil = source_series(day, "fossil")  # works without coal-derived gas
    assert fossil.points[0].value > 0
    assert source_series(day, "nuclear").points[48].value == pytest.approx(3624.5)


def test_source_absent_in_whole_period_is_a_clear_error(week_power: TimeSeriesResponse) -> None:
    with pytest.raises(KeyError, match="not available for this period"):
        source_series(week_power, "nuclear")  # no nuclear in 2026


def test_2018_total_power_is_fully_classified() -> None:
    day = load_response("total_power_de_2018-06-01_2018-06-01.json")
    unclassified = set(day.series_ids()) - GENERATION_SERIES - NON_GENERATION_SERIES
    assert unclassified == set()
    assert source_series(day, "load").points[0].value > 0
