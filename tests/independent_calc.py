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
    "load_incl_self_consumption",
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


def smard_week(filter_id: int, region: str) -> list[float]:
    """Raw SMARD values (MWh per quarter-hour) for the recorded week of 2 Nov 2026."""
    path = FIXTURES / "smard" / f"{filter_id}_{region}_quarterhour_1793574000000.json"
    return [v for _, v in json.loads(path.read_text())["series"]]


def flow_energy_gwh(name: str, neighbour: str) -> float:
    """Net energy of a cross-border series (GW, 15-minute values) in GWh."""
    return sum(r["values"][neighbour] for r in rows(name)) * 0.25


def capture_price(gen: list[dict], prices: list[dict], source: str) -> float:
    weights = [value(r, source) for r in gen]
    price = [r["values"]["day_ahead_price"] for r in prices]
    return sum(w * p for w, p in zip(weights, price, strict=True)) / sum(weights)


def between(data: list[dict], start: str, end: str) -> list[dict]:
    return [r for r in data if start <= r["timestamp"][:10] <= end]


# ---- weather (0.6.0): hour-by-hour in UTC, written out again on purpose ----


def utc_hour(timestamp: str) -> str:
    """'2026-10-25T02:15:00+01:00' -> the UTC hour it falls in, as text."""
    from datetime import UTC, datetime

    return datetime.fromisoformat(timestamp).astimezone(UTC).strftime("%Y-%m-%dT%H")


def hourly(data: list[dict], fn) -> dict[str, float]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in data:
        buckets[utc_hour(r["timestamp"])].append(fn(r))
    return {h: sum(v) / len(v) for h, v in buckets.items()}


def weather_hours(name: str, series: str) -> dict[str, float]:
    return {utc_hour(r["timestamp"]): r["values"][series] for r in rows(name)}


def hours_where(weather: str, rules: list[tuple[str, float | None, float | None]]) -> set[str]:
    """Hours in which every (series, below, above) rule holds."""
    selected = None
    for series, below, above in rules:
        values = weather_hours(weather, series)
        hits = {h for h, v in values.items()
                if (below is None or v < below) and (above is None or v > above)}  # fmt: skip
        selected = hits if selected is None else selected & hits
    return selected or set()


def mean_when(target: dict[str, float], hours: set[str]) -> float:
    picked = [target[h] for h in hours if h in target]
    return sum(picked) / len(picked)


# ---- redispatch (0.8.0), on the SYNTHETIC fixture; written out again on purpose ----


def redispatch_rows() -> list[dict]:
    import csv

    text = (FIXTURES / "ntp" / "redispatch_synthetic.csv").read_text()
    return list(csv.DictReader(text.splitlines(), delimiter=";"))


def _utc_span(row: dict):
    from datetime import UTC, datetime

    def at(day: str, clock: str) -> datetime:
        return datetime.strptime(f"{day} {clock}", "%d.%m.%Y %H:%M").replace(tzinfo=UTC)

    return at(row["BEGINN_DATUM"], row["BEGINN_UHRZEIT"]), at(
        row["ENDE_DATUM"], row["ENDE_UHRZEIT"]
    )


def redispatch_mwh(rows, lower, upper, direction: str, energy_type: str | None = None,
                   tso: str | None = None) -> float:  # fmt: skip
    """Stated MWh inside [lower, upper), spread evenly; test runs excluded."""
    total = 0.0
    for r in rows:
        if "Probefahrt" in r["GRUND_DER_MASSNAHME"] or direction not in r["RICHTUNG"]:
            continue
        if energy_type and r["PRIMAERENERGIEART"] != energy_type:
            continue
        if tso and r["ANWEISENDER_UENB"] != tso:
            continue
        start, end = _utc_span(r)
        share = (
            max((min(end, upper) - max(start, lower)).total_seconds(), 0)
            / (end - start).total_seconds()
        )
        total += float(r["GESAMTE_ARBEIT_MWH"].replace(",", ".")) * share
    return total


# ---------- flexibility (0.9.0) ----------


def battery_profit(data: list[dict], power: float, capacity: float, rte: float = 0.9) -> float:
    """Best day-ahead arbitrage, day by day (local date), starting and ending empty.

    Written differently from gridagent.tools.flexibility on purpose: a forward pass
    over a dict {stored units: best cash so far}. One unit = a quarter of the energy
    the battery can move in one 15-minute interval (the grid is exact for the cases).
    """
    unit = power * 0.25 / 4
    top, eta = round(capacity / unit), rte**0.5
    total = 0.0
    days: dict[str, list[float]] = defaultdict(list)
    for row in data:
        days[row["timestamp"][:10]].append(row["values"]["day_ahead_price"])
    for prices in days.values():
        cash = {0: 0.0}
        for price in prices:
            nxt: dict[int, float] = {}
            for level, money in cash.items():
                for move in range(-4, 5):
                    new = level + move
                    if 0 <= new <= top:
                        energy = move * unit
                        bought = energy / eta if energy > 0 else energy * eta
                        value = money - bought * price
                        if value > nxt.get(new, float("-inf")):
                            nxt[new] = value
            cash = nxt
        total += cash[0]
    return total


def cheapest_load(data: list[dict], energy: float, power: float) -> tuple[float, float]:
    """(average price paid, saving vs running evenly) for 15-minute prices."""
    prices = sorted(r["values"]["day_ahead_price"] for r in data)
    per_interval = power * 0.25
    left, cost = energy, 0.0
    for price in prices:
        take = min(per_interval, left)
        cost += take * price
        left -= take
        if left <= 0:
            break
    even = energy * sum(prices) / len(prices)
    return cost / energy, even - cost
