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
        source_series(week_power, "nuclear")
