"""Tool definitions sent to the model (name, description, JSON schema).

These descriptions are effectively part of the prompt: the model decides
which tool to call, and with which arguments, from this text alone.

`strict: true` makes the API guarantee that tool inputs match the schema.
Strict schemas need `additionalProperties: false`; we keep every field
required and use `null` for "not given", which keeps the schemas simple.
"""

from typing import Any

from gridagent.data.sources import available_sources
from gridagent.tools.stats import Operation

DATE = {
    "type": "string",
    "format": "date",
    "description": "Date as YYYY-MM-DD (German local time).",
}

GET_GENERATION: dict[str, Any] = {
    "name": "get_generation",
    "description": (
        "Fetch Germany's public net electricity generation by source (and load) from "
        "Energy-Charts for whole days from `start` to `end` inclusive, in 15-minute resolution "
        "(MW). Returns a short summary with a `ref` id; the data itself stays on the server. "
        "Pass the `ref` to compute_stats to calculate anything. Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start": DATE,
            "end": DATE,
            "sources": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string", "enum": available_sources()}},
                    {"type": "null"},
                ],
                "description": "Sources you intend to analyse, or null for all.",
            },
        },
        "required": ["start", "end", "sources"],
        "additionalProperties": False,
    },
    "strict": True,
}

GET_DAY_AHEAD_PRICE: dict[str, Any] = {
    "name": "get_day_ahead_price",
    "description": (
        "Fetch the day-ahead spot market price for the Germany-Luxembourg bidding zone "
        "(EUR/MWh, 15-minute resolution) for whole days from `start` to `end` inclusive. "
        "Tomorrow's prices are published around midday. Returns a summary with a `ref` id "
        "for compute_stats. Max 93 days per call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"start": DATE, "end": DATE},
        "required": ["start", "end"],
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
        "- energy: total energy in MWh and GWh (generation data only)\n"
        "- share: percentage of `source` relative to `relative_to` (default: total "
        "generation), using energy over the period\n"
        "`source` is required for generation refs and must be null for price refs. "
        "Set `per_day` to true for one result per calendar day (not for share)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "The ref returned by a get_* tool."},
            "operation": {"type": "string", "enum": [op.value for op in Operation]},
            "source": {
                "anyOf": [{"type": "string", "enum": available_sources()}, {"type": "null"}],
            },
            "relative_to": {
                "anyOf": [{"type": "string", "enum": available_sources()}, {"type": "null"}],
                "description": "Only for share. Null means total generation.",
            },
            "per_day": {"type": "boolean"},
        },
        "required": ["ref", "operation", "source", "relative_to", "per_day"],
        "additionalProperties": False,
    },
    "strict": True,
}

ALL_TOOLS: dict[str, dict[str, Any]] = {
    t["name"]: t for t in (GET_GENERATION, GET_DAY_AHEAD_PRICE, COMPUTE_STATS)
}
