"""Tool definitions sent to the model (name, description, JSON schema).

These descriptions are effectively part of the prompt: the model decides
which tool to call, and with which arguments, from this text alone.

`strict: true` makes the API guarantee that tool inputs match the schema.
Strict schemas need `additionalProperties: false`; we keep every field
required and use `null` for "not given", which keeps the schemas simple.
"""

from typing import Any

from gridagent.data.reference import load_reference_data
from gridagent.data.smard import FILTERS as SMARD_SOURCES
from gridagent.data.smard import REGIONS
from gridagent.data.sources import available_sources
from gridagent.tools.stats import Operation

NEIGHBOURS = [
    "austria", "belgium", "czech_republic", "denmark", "france", "luxembourg",
    "netherlands", "norway", "poland", "sweden", "switzerland",
]  # fmt: skip
CAPACITY_TECHNOLOGIES = [
    "solar", "wind_onshore", "wind_offshore", "biomass", "hydro", "hydro_pumped_storage",
    "battery_storage", "fossil_brown_coal_lignite", "fossil_hard_coal", "fossil_gas",
    "fossil_oil", "nuclear",
]  # fmt: skip

WEATHER_SERIES = ["wind_speed", "solar_radiation"]
# Every name compute_stats accepts as `source`, across all kinds of refs.
ALL_SERIES_NAMES = sorted(
    set(available_sources()) | set(SMARD_SOURCES) | set(NEIGHBOURS) | {"sum"} | set(WEATHER_SERIES)
)

DATE = {
    "type": "string",
    "format": "date",
    "description": "Date as YYYY-MM-DD (German local time).",
}


def _nullable(schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}], "description": description}


GET_GENERATION: dict[str, Any] = {
    "name": "get_generation",
    "description": (
        "Fetch Germany's electricity generation by source and load from Energy-Charts for "
        "whole days from `start` to `end` inclusive (15-minute values, MW). Returns a short "
        "summary with a `ref`; the data stays on the server. Pass the `ref` to compute_stats. "
        "scope 'public' (default) = public net generation (Energy-Charts' standard); 'total' "
        "= total net generation including industrial self-supply (closer to official "
        "statistics; load is then load including self-consumption). Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": DATE,
            "end": DATE,
            "sources": _nullable(
                {"type": "array", "items": {"type": "string", "enum": available_sources()}},
                "Sources you intend to analyse, or null for all.",
            ),
            "scope": _nullable(
                {"type": "string", "enum": ["public", "total"]}, "null means 'public'."
            ),
        },
        "required": ["start", "end", "sources", "scope"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_DAY_AHEAD_PRICE: dict[str, Any] = {
    "name": "get_day_ahead_price",
    "description": (
        "Fetch the day-ahead spot market price for the Germany-Luxembourg bidding zone "
        "(EUR/MWh, 15-minute resolution) for whole days from `start` to `end` inclusive. "
        "Tomorrow's prices are published around midday. Returns a summary with a `ref` for "
        "compute_stats. Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"start": DATE, "end": DATE},
        "required": ["start", "end"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_CROSS_BORDER_FLOWS: dict[str, Any] = {
    "name": "get_cross_border_flows",
    "description": (
        "Fetch Germany's cross-border electricity flows with each neighbour plus a 'sum' "
        "series, in GW at 15-minute resolution. Positive values are imports into Germany, "
        "negative values exports; the energy of a series is therefore the NET import (GWh). "
        "kind 'physical' = measured physical flows; 'trading' = scheduled commercial "
        "exchanges. Returns a `ref` for compute_stats (source = a neighbour or 'sum'). "
        "Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": DATE,
            "end": DATE,
            "kind": {"type": "string", "enum": ["physical", "trading"]},
        },
        "required": ["start", "end", "kind"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_REGIONAL_GENERATION: dict[str, Any] = {
    "name": "get_regional_generation",
    "description": (
        "Fetch generation by source or load for one German transmission control area from "
        "SMARD (Bundesnetzagentur), converted to MW at 15-minute resolution. Regions: "
        "50Hertz (north-east), Amprion (west), TenneT (north to Bavaria), TransnetBW "
        "(Baden-Württemberg), or DE for all of Germany. Control areas are grid regions, not "
        "federal states. Returns a `ref` for compute_stats. Max 3 sources and 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "region": {"type": "string", "enum": list(REGIONS)},
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": list(SMARD_SOURCES)},
                "description": "1 to 3 series to fetch.",
            },
            "start": DATE,
            "end": DATE,
        },
        "required": ["region", "sources", "start", "end"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_INSTALLED_CAPACITY: dict[str, Any] = {
    "name": "get_installed_capacity",
    "description": (
        "Installed generation capacity in Germany from Energy-Charts, in GW (battery storage "
        "capacity in GWh). Each value is the capacity at the END of the year or month shown. "
        "'yearly' covers 2002 onwards and includes the legal 2030 targets (EEG 2023 for solar "
        "and onshore wind, WindSeeG for offshore). 'monthly' covers solar, wind, biomass and "
        "batteries for recent months. Solar is reported as AC (inverter) capacity; DC (module) "
        "capacity is also returned and is higher. Returns the figures directly (no ref)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "time_step": {"type": "string", "enum": ["yearly", "monthly"]},
            "technologies": _nullable(
                {"type": "array", "items": {"type": "string", "enum": CAPACITY_TECHNOLOGIES}},
                "Technologies to return, or null for all.",
            ),
        },
        "required": ["time_step", "technologies"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_REFERENCE_STATISTICS: dict[str, Any] = {
    "name": "get_reference_statistics",
    "description": (
        "Official published statistics with their exact definition, source and status "
        "(final, preliminary, estimate or reported): CO2 factor of the electricity mix (UBA), "
        "renewable share of gross generation (BDEW/AGEB) and of gross consumption (UBA), gross "
        "generation, and the official EEG market values (netztransparenz). Use these to put "
        "your calculated numbers next to official figures and explain differences in "
        "definition. `period` is a year ('2025') or month ('2026-10'), or null for all."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dataset": {"type": "string", "enum": load_reference_data().ids()},
            "period": _nullable({"type": "string"}, "e.g. '2025' or '2026-10'"),
        },
        "required": ["dataset", "period"],
        "additionalProperties": False,
    },
    "strict": True,
}

COMPUTE_STATS: dict[str, Any] = {
    "name": "compute_stats",
    "description": (
        "Calculate a statistic over data fetched earlier, identified by its `ref`. "
        "Always use this for numbers: never calculate yourself.\n"
        "- sum / mean / min / max / count: over the 15-minute values (min/max also return "
        "when they occurred)\n"
        "- energy: total energy (MW series -> MWh and GWh; GW series -> GWh)\n"
        "- share: percentage of `source` relative to `relative_to` (default: total "
        "generation), using energy over the period (generation refs only)\n"
        "- count_below / count_above: hours with values below / above `threshold` "
        "(e.g. negative prices: count_below, threshold 0)\n"
        "- capture_price: price ref weighted by a generation series (`weight_ref` + "
        "`weight_source`): the generation-weighted average price = market value, in EUR/MWh "
        "and ct/kWh\n"
        "- capacity_factor: generation ref + source (solar, wind, wind_onshore, wind_offshore, "
        "biomass); uses installed capacity at the start of the period\n"
        "`source`: required for generation, regional and cross-border refs (for cross-border: "
        "a neighbour or 'sum'); null for price refs. `per_day` gives one result per day "
        "(sum/mean/min/max/count/energy only)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "The ref returned by a get_* tool."},
            "operation": {"type": "string", "enum": [op.value for op in Operation]},
            "source": _nullable({"type": "string", "enum": ALL_SERIES_NAMES}, "Series to use."),
            "relative_to": _nullable(
                {"type": "string", "enum": available_sources()},
                "Only for share. Null means total generation.",
            ),
            "per_day": {"type": "boolean"},
            "threshold": _nullable({"type": "number"}, "Only for count_below / count_above."),
            "weight_ref": _nullable({"type": "string"}, "Only for capture_price: generation ref."),
            "weight_source": _nullable(
                {"type": "string", "enum": ALL_SERIES_NAMES},
                "Only for capture_price: the generation source, e.g. solar.",
            ),
        },
        "required": [
            "ref",
            "operation",
            "source",
            "relative_to",
            "per_day",
            "threshold",
            "weight_ref",
            "weight_source",
        ],
        "additionalProperties": False,
    },  # fmt: skip
    "strict": True,
}

GET_WEATHER: dict[str, Any] = {
    "name": "get_weather",
    "description": (
        "Fetch hourly weather indicators for Germany from the Deutscher Wetterdienst (DWD) "
        "for whole days from `start` to `end` inclusive (data up to yesterday). Series: "
        "'wind_speed' (m/s, mean wind measured ~10 m above ground) and 'solar_radiation' "
        "(W/m², global horizontal irradiance), each the plain mean over a fixed set of 65 DWD "
        "stations. Returns a summary with a `ref` for compute_stats (mean/min/max/"
        "count_below/count_above with `source`) and compute_when. Station wind is an "
        "indicator of how windy it was, not the wind at turbine hub height (100-150 m): say "
        "so when you use it. Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"start": DATE, "end": DATE},
        "required": ["start", "end"],
        "additionalProperties": False,
    },
    "strict": True,
}

_CONDITION = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Ref whose values decide which hours count."},
        "source": _nullable(
            {"type": "string", "enum": ALL_SERIES_NAMES},
            "Series in that ref (null for a price ref), e.g. wind_speed.",
        ),
        "below": _nullable({"type": "number"}, "Hour counts if the value is below this."),
        "above": _nullable({"type": "number"}, "Hour counts if the value is above this."),
    },
    "required": ["ref", "source", "below", "above"],
    "additionalProperties": False,
}

COMPUTE_WHEN: dict[str, Any] = {
    "name": "compute_when",
    "description": (
        "Conditions -> effect: select the hours in which ALL `conditions` hold (e.g. wind_speed "
        "below 3 m/s, solar_radiation below 50 W/m²), then compute `operation` on the target "
        "series (`ref` + `source`) in exactly those hours. Everything is compared as hourly "
        "means. operation 'hours' counts the selected hours (ref and source may be null); "
        "'mean', 'min', 'max' work on any series; 'energy' sums a MW series to MWh. Returns the "
        "value, hours_selected, hours_considered and share_of_hours_pct. Use it for questions "
        "like 'average price when there was little wind' or 'how many hours with neither wind "
        "nor sun (Dunkelflaute)'. The condition and target refs must cover the same period. "
        "State the thresholds you used: they are your choice, not an official definition."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["hours", "mean", "min", "max", "energy"]},
            "ref": _nullable({"type": "string"}, "Target ref (null for 'hours')."),
            "source": _nullable(
                {"type": "string", "enum": ALL_SERIES_NAMES},
                "Target series (null for price refs and for 'hours').",
            ),
            "conditions": {"type": "array", "items": _CONDITION, "minItems": 1, "maxItems": 3},
        },
        "required": ["operation", "ref", "source", "conditions"],
        "additionalProperties": False,
    },
    "strict": True,
}

COMPARE_VALUES: dict[str, Any] = {
    "name": "compare_values",
    "description": (
        "Compare two numbers you got from earlier tool results, e.g. your computed market "
        "value with the official one, this week's wind share with last week's, or two "
        "regions. Returns a - b (difference), (a - b) / |b| in % (percent_difference), "
        "a / b (ratio) and a + b (sum). Use it for every difference, percentage change, "
        "ratio or total of two numbers instead of calculating yourself. Both numbers must "
        "appear in an earlier tool result in this conversation (or in the user's "
        "question), in the same unit; values are rejected otherwise."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "First value (e.g. this period, ours)."},
            "b": {"type": "number", "description": "Second value (e.g. earlier, official)."},
            "a_label": {"type": "string", "description": "What a is, e.g. 'solar, Oct 2026'."},
            "b_label": {"type": "string", "description": "What b is."},
            "unit": {"type": "string", "description": "Unit of both values, e.g. 'ct/kWh'."},
        },
        "required": ["a", "b", "a_label", "b_label", "unit"],
        "additionalProperties": False,
    },
    "strict": True,
}

ALL_TOOLS: dict[str, dict[str, Any]] = {
    t["name"]: t
    for t in (
        GET_GENERATION,
        GET_DAY_AHEAD_PRICE,
        GET_CROSS_BORDER_FLOWS,
        GET_REGIONAL_GENERATION,
        GET_INSTALLED_CAPACITY,
        GET_REFERENCE_STATISTICS,
        COMPUTE_STATS,
        COMPARE_VALUES,
        GET_WEATHER,
        COMPUTE_WHEN,
    )
}

# Tools that store data under a ref, and the ref prefix they use (gen_1, price_2, ...).
REF_PREFIX = {
    "get_generation": "gen",
    "get_day_ahead_price": "price",
    "get_cross_border_flows": "flow",
    "get_regional_generation": "region",
    "get_weather": "weather",
}
