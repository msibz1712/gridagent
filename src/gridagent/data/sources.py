"""Mapping from user-friendly source names to Energy-Charts series.

`public_power` returns ~21 series, but not all of them are generation:
`load`, `residual_load`, the two `%` shares, cross-border trading and
pumped-storage *consumption* must never be added into "total generation".

Definitions follow Energy-Charts' own convention, verified against its
`renewable_share_of_generation` column (see tests/test_sources.py):
- total generation excludes pumped-storage output (storage moves energy,
  it does not generate it),
- 50% of waste counts as renewable (the biogenic fraction).
"""

from gridagent.data.models import Point, Series, TimeSeriesResponse

# Series in public_power that are NOT generation. Never summed into totals.
NON_GENERATION_SERIES = frozenset(
    {
        "load",
        "residual_load",
        "renewable_share_of_load",
        "renewable_share_of_generation",
        "cross_border_electricity_trading",
        "hydro_pumped_storage_consumption",
        "load_incl_self_consumption",  # total_power's load (incl. industrial self-supply)
    }
)

# Every generation series. Kept explicit so a new API series can't silently
# change our totals; a test fails if the API returns an unclassified series.
GENERATION_SERIES = frozenset(
    {
        "hydro_run_of_river",
        "hydro_water_reservoir",
        "hydro_pumped_storage",
        "biomass",
        "geothermal",
        "wind_onshore",
        "wind_offshore",
        "solar",
        "fossil_brown_coal_lignite",
        "fossil_hard_coal",
        "fossil_oil",
        "fossil_coal_derived_gas",
        "fossil_gas",
        "waste",
        "others",
        "nuclear",  # until April 2023; must count in totals for historical shares
    }
)

# Friendly name -> {series id: weight}. The weight is almost always 1.0.
SOURCE_GROUPS: dict[str, dict[str, float]] = {
    "solar": {"solar": 1.0},
    "wind": {"wind_onshore": 1.0, "wind_offshore": 1.0},
    "wind_onshore": {"wind_onshore": 1.0},
    "wind_offshore": {"wind_offshore": 1.0},
    "biomass": {"biomass": 1.0},
    "hydro": {"hydro_run_of_river": 1.0, "hydro_water_reservoir": 1.0},
    "pumped_storage": {"hydro_pumped_storage": 1.0},
    "geothermal": {"geothermal": 1.0},
    "lignite": {"fossil_brown_coal_lignite": 1.0},
    "hard_coal": {"fossil_hard_coal": 1.0},
    "gas": {"fossil_gas": 1.0},
    "oil": {"fossil_oil": 1.0},
    "fossil": {
        "fossil_brown_coal_lignite": 1.0,
        "fossil_hard_coal": 1.0,
        "fossil_oil": 1.0,
        "fossil_coal_derived_gas": 1.0,
        "fossil_gas": 1.0,
    },
    "renewable": {
        "hydro_run_of_river": 1.0,
        "hydro_water_reservoir": 1.0,
        "biomass": 1.0,
        "geothermal": 1.0,
        "wind_onshore": 1.0,
        "wind_offshore": 1.0,
        "solar": 1.0,
        "waste": 0.5,
    },
    "waste": {"waste": 1.0},
    "nuclear": {"nuclear": 1.0},
    "others": {"others": 1.0},
    "load": {"load": 1.0},
    "total": {s: 1.0 for s in GENERATION_SERIES if s != "hydro_pumped_storage"},
}


def available_sources() -> list[str]:
    """Names accepted by `source_series`."""
    return sorted(SOURCE_GROUPS)


def source_series(response: TimeSeriesResponse, source: str) -> Series:
    """Build the series for a friendly source name by summing its components.

    A point is `None` (missing) if any component is missing at that timestamp,
    so we never silently report a partial total. A component series that does
    not exist in the period at all (e.g. nuclear after April 2023, coal-derived
    gas before it was reported separately) contributes nothing.
    """
    if source not in SOURCE_GROUPS:
        raise KeyError(f"Unknown source '{source}'. Choose from: {', '.join(available_sources())}")
    weights = SOURCE_GROUPS[source]
    if source == "load" and "load" not in response.series_ids():
        # total_power reports demand including industrial self-consumption.
        weights = {"load_incl_self_consumption": 1.0}
    available = set(response.series_ids())
    weights = {sid: w for sid, w in weights.items() if sid in available}
    if not weights:
        raise KeyError(f"'{source}' is not available for this period.")
    components = {sid: response.get_series(sid) for sid in weights}

    points: list[Point] = []
    for i, row in enumerate(response.data):
        values = [components[sid].points[i].value for sid in weights]
        known = [v for v in values if v is not None]
        total = (
            sum(weights[sid] * v for sid, v in zip(weights, known, strict=True))
            if len(known) == len(values)  # one missing component: the total is unknown
            else None
        )
        points.append(Point(timestamp=row.timestamp, value=total))

    return Series(
        id=source,
        unit=response.unit or "MW",
        interval_minutes=response.interval_minutes,
        points=points,
    )
