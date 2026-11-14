"""Independent reference calculations over the raw fixture JSON.

Expected values in the eval set must NOT be computed with the agent's own
tools (that would only test the tools against themselves). This module
re-implements the few definitions it needs with plain Python over the raw
JSON, so a bug in `gridagent.tools` or `gridagent.data.sources` makes the
eval-set consistency test fail instead of silently agreeing.
"""

import json
from collections import defaultdict
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

# Energy-Charts definitions, written out again on purpose.
TOTAL_EXCLUDES = {
    "load",
    "residual_load",
    "renewable_share_of_load",
    "renewable_share_of_generation",
    "cross_border_electricity_trading",
    "hydro_pumped_storage_consumption",
    "hydro_pumped_storage",
}
RENEWABLE_WEIGHTS = {
    "hydro_run_of_river": 1,
    "hydro_water_reservoir": 1,
    "biomass": 1,
    "geothermal": 1,
    "wind_onshore": 1,
    "wind_offshore": 1,
    "solar": 1,
    "waste": 0.5,
}
GROUPS = {
    "wind": {"wind_onshore": 1, "wind_offshore": 1},
    "renewable": RENEWABLE_WEIGHTS,
}


def rows(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())["data"]


def value(row: dict, source: str) -> float:
    v = row["values"]
    if source == "total":
        return sum(x for k, x in v.items() if k not in TOTAL_EXCLUDES)
    if source in GROUPS:
        return sum(w * v[k] for k, w in GROUPS[source].items())
    return v[source]


def energy_mwh(data: list[dict], source: str) -> float:
    return sum(value(r, source) for r in data) * 0.25


def share_pct(data: list[dict], source: str, of: str = "total") -> float:
    return 100 * sum(value(r, source) for r in data) / sum(value(r, of) for r in data)


def mean(data: list[dict], source: str) -> float:
    return sum(value(r, source) for r in data) / len(data)


def peak(data: list[dict], source: str, highest: bool = True) -> tuple[float, str]:
    pick = max if highest else min
    best = pick(data, key=lambda r: value(r, source))
    return value(best, source), best["timestamp"][:16]


def by_day(data: list[dict]) -> dict[str, list[dict]]:
    days: dict[str, list[dict]] = defaultdict(list)
    for r in data:
        days[r["timestamp"][:10]].append(r)
    return dict(days)


def between(data: list[dict], start: str, end: str) -> list[dict]:
    return [r for r in data if start <= r["timestamp"][:10] <= end]
