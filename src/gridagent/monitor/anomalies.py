"""Which of yesterday's numbers are news? Four levels of evidence, one score.

1. Robust surprise. Each hour's price against the same clock hour on comparable
   days (weekdays with weekdays, weekends with weekends) in the 4 weeks before,
   measured with the median and the MAD (median absolute deviation, x 1.4826 so
   it matches a standard deviation for normal data). Unlike mean and standard
   deviation, one extreme day cannot hide the next one by widening "normal".
2. Fundamentals. A curved merit order, price = a + b*x + c*x^2 with x = residual load
   in GW, fitted on every hour of the 4 weeks: the supply curve bends upward, as the
   last plants are the most expensive. A spike the curve predicts is *explained*
   (little wind, high demand); a spike it does not predict is *unexplained*:
   something the data does not show (outages, fuel prices, neighbours), the
   genuinely surprising news.
3. History. "Highest since ..." and streaks, from up to two years of daily figures.
4. Score. surprise, size, duration and context, each 0..1, weighted and stated in
   the story, so a reader can see why it was picked.

Thresholds are named constants below; a backtest over past months (`garden
anomalies`) checks them against days that are known to have been unusual.
"""

import math
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import Series, TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.tools.investigate import BERLIN, hourly, is_weekend

WINDOW_DAYS = 28
MAD_TO_SD = 1.4826
PRICE_Z = 3.0  # robust z for a price story
PRICE_MIN_EUR = 50.0  # and at least this far from normal
UNEXPLAINED_Z = 2.5  # robust z of the gap to the merit-order curve
LULL_SHARE, LULL_HOURS = 0.10, 4
RECORD_MIN_DAYS = 60  # "highest since ..." only when it beats at least 2 months
STREAK_MIN_DAYS = 3
SHIFT_Z = 3.0  # robust z of a day-to-day change
HISTORY_DAYS = 730
HISTORY_CHUNK_DAYS = 90


class Story(BaseModel):
    """One candidate news item, with the evidence and the score behind it."""

    kind: str  # price_spike, price_dip, negative_prices, lull, record, streak
    title: str  # plain, from the numbers; the news writer rewrites it later
    score: float  # 0..1
    score_parts: dict[str, float]
    facts: dict[str, Any]  # every number a writer may use
    from_hour: int | None = None
    to_hour: int | None = None  # exclusive
    explained_by_fundamentals: bool | None = None  # price stories only
    explanation: str | None = None  # by the model (monitor/explain.py)
    explanation_steps: list[str] = []


# ---------- small statistics ----------


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def robust_scale(values: list[float]) -> float:
    """MAD x 1.4826; falls back to the standard deviation when half the values are equal."""
    m = median(values)
    mad = median([abs(v - m) for v in values]) * MAD_TO_SD
    if mad > 0:
        return mad
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1))


def quadratic_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares y = a + b*x + c*x^2 (normal equations, solved by elimination)."""
    sums = [sum(x**k for x in xs) for k in range(5)]
    rhs = [sum(y * x**k for x, y in zip(xs, ys, strict=True)) for k in range(3)]
    m = [[sums[i + j] for j in range(3)] + [rhs[i]] for i in range(3)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        if abs(m[col][col]) < 1e-12:
            raise ValueError("Residual load did not vary enough to fit a curve.")
        for r in range(3):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col], strict=True)]
    return tuple(m[i][3] / m[i][i] for i in range(3))  # type: ignore[return-value]


def _by_local_hour(series: Series) -> dict[tuple[date, int], float]:
    out: dict[tuple[date, int], list[float]] = defaultdict(list)
    for hour, value in hourly(series).items():
        local = hour.astimezone(BERLIN)
        out[(local.date(), local.hour)].append(value)
    return {k: sum(v) / len(v) for k, v in out.items()}


def _runs(hours: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for h in sorted(hours):
        if runs and runs[-1][1] == h:
            runs[-1] = (runs[-1][0], h + 1)
        else:
            runs.append((h, h + 1))
    return runs


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score(parts: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(weights[k] * parts[k] for k in weights), 3)


def _hours(first: int, end: int) -> str:
    return f"{first:02d}:00–{end:02d}:00"


# ---------- 1 + 2: price stories ----------


PRICE_WEIGHTS = {"surprise": 0.35, "unexplained": 0.25, "size": 0.25, "duration": 0.15}


def price_stories(day: date, price: Series, residual_load: Series | None) -> list[Story]:
    """Spikes and dips against the robust normal, each tested against the merit order."""
    prices = _by_local_hour(price)
    residual = _by_local_hour(residual_load) if residual_load is not None else {}
    window = [day - timedelta(days=i) for i in range(1, WINDOW_DAYS + 1)]
    comparable = [d for d in window if is_weekend(d) == is_weekend(day)]

    curve, gap_center, gap_scale = None, 0.0, 0.0
    pairs = [(residual[k] / 1000, prices[k]) for k in prices
             if k[0] in window and k in residual]  # fmt: skip
    if len(pairs) >= 200:
        curve = quadratic_fit([p[0] for p in pairs], [p[1] for p in pairs])
        gaps = [y - _curve(curve, x) for x, y in pairs]
        gap_center, gap_scale = median(gaps), robust_scale(gaps)

    flagged: dict[str, dict[int, dict[str, float]]] = {"spike": {}, "dip": {}}
    for hour in range(24):
        if (day, hour) not in prices:
            continue
        normal = [prices[(d, hour)] for d in comparable if (d, hour) in prices]
        if len(normal) < 5:
            continue
        center, scale = median(normal), robust_scale(normal)
        value = prices[(day, hour)]
        z = (value - center) / scale if scale else 0.0
        info = {"price": value, "normal": center, "z": z}
        if curve is not None and (day, hour) in residual:
            x = residual[(day, hour)] / 1000
            info |= {"residual_gw": x, "expected": _curve(curve, x),
                     "gap_z": (value - _curve(curve, x) - gap_center) / gap_scale}  # fmt: skip
        if z >= PRICE_Z and value - center >= PRICE_MIN_EUR:
            flagged["spike"][hour] = info
        elif z <= -PRICE_Z and center - value >= PRICE_MIN_EUR:
            flagged["dip"][hour] = info

    stories = []
    for kind, hours in flagged.items():
        for first, end in _runs(list(hours)):
            group = [hours[h] for h in range(first, end)]

            def mean(key: str, group: list[dict[str, float]] = group) -> float:
                return sum(g[key] for g in group) / len(group)

            price_now, normal = mean("price"), mean("normal")
            z = max(abs(g["z"]) for g in group)
            facts: dict[str, Any] = {
                "date": day.isoformat(), "hours": _hours(first, end),
                "price_eur_mwh": round(price_now, 2), "normal_eur_mwh": round(normal, 2),
                "difference_eur_mwh": round(price_now - normal, 2), "robust_z": round(z, 2),
                "comparable_days": len(comparable),
            }  # fmt: skip
            explained = None
            gap_z = 0.0
            if all("gap_z" in g for g in group):
                gap_z = mean("gap_z")
                explained = abs(gap_z) < UNEXPLAINED_Z
                facts |= {"residual_load_gw": round(mean("residual_gw"), 2),
                          "merit_order_expected_eur_mwh": round(mean("expected"), 2),
                          "gap_to_merit_order_z": round(gap_z, 2)}  # fmt: skip
            parts = {
                "surprise": _clip(z / 8),
                "unexplained": _clip(abs(gap_z) / 6),
                "size": _clip(abs(price_now - normal) / 250),
                "duration": _clip((end - first) / 8),
            }
            word = "above" if kind == "spike" else "below"
            reading = ("" if explained is None else
                       " — in line with the residual load" if explained else
                       " — more than the residual load explains")  # fmt: skip
            stories.append(Story(
                kind=f"price_{kind}", score=_score(parts, PRICE_WEIGHTS), score_parts=parts,
                facts=facts, from_hour=first, to_hour=end, explained_by_fundamentals=explained,
                title=(f"{_hours(first, end)}: {price_now:.2f} EUR/MWh, "
                       f"{abs(price_now - normal):.2f} {word} the usual {normal:.2f}{reading}"),
            ))  # fmt: skip
    return stories


def _curve(abc: tuple[float, float, float], x: float) -> float:
    a, b, c = abc
    return a + b * x + c * x * x


# ---------- other same-day stories ----------


def negative_price_story(day: date, price: Series) -> Story | None:
    points = [p for p in price.points
              if p.timestamp.astimezone(BERLIN).date() == day and p.value is not None]  # fmt: skip
    negative = [p for p in points if p.value < 0]  # type: ignore[operator]
    if not negative or not price.interval_minutes:
        return None
    hours = len(negative) * price.interval_minutes / 60
    lowest = min(p.value for p in negative)  # type: ignore[type-var]
    local = [p.timestamp.astimezone(BERLIN).hour for p in negative]
    parts = {"duration": _clip(hours / 8), "depth": _clip(-lowest / 100)}  # type: ignore[operator]
    return Story(
        kind="negative_prices", score_parts=parts,
        score=_score(parts, {"duration": 0.4, "depth": 0.3}) + 0.1,
        from_hour=min(local), to_hour=max(local) + 1,
        facts={"date": day.isoformat(), "negative_hours": hours, "lowest_eur_mwh": lowest},
        title=f"Negative prices for {hours:g} hours, down to {lowest:.2f} EUR/MWh",
    )  # fmt: skip


def lull_stories(day: date, generation: TimeSeriesResponse) -> list[Story]:
    wind = _by_local_hour(source_series(generation, "wind"))
    solar = _by_local_hour(source_series(generation, "solar"))
    load = _by_local_hour(source_series(generation, "load"))
    lull = [h for h in range(24) if all((day, h) in s for s in (wind, solar, load))
            and wind[(day, h)] + solar[(day, h)] < LULL_SHARE * load[(day, h)]]  # fmt: skip
    stories = []
    for first, end in _runs(lull):
        if end - first < LULL_HOURS:
            continue
        hs = range(first, end)
        share = (
            100
            * sum(wind[(day, h)] + solar[(day, h)] for h in hs)
            / sum(load[(day, h)] for h in hs)
        )
        parts = {"duration": _clip((end - first) / 12), "depth": _clip((10 - share) / 10)}
        stories.append(Story(
            kind="lull", score_parts=parts, score=_score(parts, {"duration": 0.4, "depth": 0.2}),
            from_hour=first, to_hour=end,
            facts={"date": day.isoformat(), "hours": _hours(first, end),
                   "wind_and_solar_share_of_demand_pct": round(share, 1)},
            title=f"{_hours(first, end)}: wind and solar covered {share:.1f} % of demand",
        ))  # fmt: skip
    return stories


# ---------- 3: history ----------

DAILY_FIGURES = {
    # key: (label, unit, higher is "high")
    "price_mean": ("average day-ahead price", "EUR/MWh"),
    "wind_gwh": ("wind energy", "GWh"),
    "solar_gwh": ("solar energy", "GWh"),
    "renewable_share_pct": ("renewable share of public net generation", "%"),
    "residual_peak_gw": ("peak residual load", "GW"),
}


def daily_figures(price: Series, generation: TimeSeriesResponse | None) -> dict[date, dict]:
    """Per local day: the figures records and streaks are about (complete days only)."""
    out: dict[date, dict] = defaultdict(dict)
    by_day: dict[date, list[float]] = defaultdict(list)
    for p in price.points:
        if p.value is not None:
            by_day[p.timestamp.astimezone(BERLIN).date()].append(p.value)
    per_day = 24 * 60 // (price.interval_minutes or 60)
    for d, values in by_day.items():
        if len(values) >= per_day * 0.9:
            out[d]["price_mean"] = sum(values) / len(values)
    if generation is not None:
        sums: dict[str, dict[date, list[float]]] = {}
        for key, source in (("wind", "wind"), ("solar", "solar"), ("ren", "renewable"),
                            ("total", "total")):  # fmt: skip
            sums[key] = defaultdict(list)
            for p in source_series(generation, source).points:
                if p.value is not None:
                    sums[key][p.timestamp.astimezone(BERLIN).date()].append(p.value)
        hours = (generation.interval_minutes or 15) / 60
        for d, wind in sums["wind"].items():
            if len(wind) * hours < 22:  # incomplete day
                continue
            out[d]["wind_gwh"] = sum(wind) * hours / 1000
            out[d]["solar_gwh"] = sum(sums["solar"].get(d, [])) * hours / 1000
            total = sum(sums["total"].get(d, []))
            if total:
                out[d]["renewable_share_pct"] = 100 * sum(sums["ren"].get(d, [])) / total
        residual: dict[date, list[float]] = defaultdict(list)
        for p in generation.get_series("residual_load").points:
            if p.value is not None:
                residual[p.timestamp.astimezone(BERLIN).date()].append(p.value)
        for d, values in residual.items():
            if d in out:
                out[d]["residual_peak_gw"] = max(values) / 1000
    return dict(out)


def load_history(data: DataProvider, day: date, days: int = HISTORY_DAYS,
                 progress: Callable[[str], None] = lambda m: None) -> dict[date, dict]:  # fmt: skip
    """Daily figures from `day - days` to `day`, fetched in chunks back in time. Where the
    data ends, the chunk is halved until even one day is missing: then history stops."""
    history: dict[date, dict] = {}
    end = day
    first_needed = day - timedelta(days=days)
    chunk = HISTORY_CHUNK_DAYS
    while end >= first_needed:
        start = max(first_needed, end - timedelta(days=chunk - 1))
        try:
            price = data.day_ahead_price(start, end).get_series("day_ahead_price")
        except (NoDataError, EnergyChartsError):
            if chunk == 1:
                break  # the available history ends here
            chunk = max(1, chunk // 2)
            continue
        try:
            generation = data.public_power(start, end)
        except (NoDataError, EnergyChartsError):
            generation = None
        history.update(daily_figures(price, generation))
        progress(f"history {start}..{end}")
        end = start - timedelta(days=1)
    return history


def record_stories(day: date, history: dict[date, dict]) -> list[Story]:
    """'Highest/lowest since ...' when the day beats at least RECORD_MIN_DAYS of history."""
    if day not in history:
        return []
    first_day = min(history)
    stories = []
    for key, (label, unit) in DAILY_FIGURES.items():
        if key not in history[day]:
            continue
        value = history[day][key]
        tests = (("highest", lambda v, x=value: v >= x), ("lowest", lambda v, x=value: v <= x))
        for word, beats in tests:
            earlier = sorted((d for d in history if d < day and key in history[d]), reverse=True)
            if not earlier:
                continue
            last_beaten = next((d for d in earlier if beats(history[d][key])), None)
            since = last_beaten or first_day
            span = (day - since).days
            if span < RECORD_MIN_DAYS:
                continue
            if last_beaten is None:
                # Only as far back as the history was loaded: "at least", never "ever".
                phrase = f"the {word} since at least {first_day:%-d %B %Y}"
            else:
                phrase = f"the {word} since {last_beaten:%-d %B %Y}"
            parts = {"span": _clip(math.log(span / RECORD_MIN_DAYS + 1) / math.log(13))}
            stories.append(Story(
                kind="record", score_parts=parts, score=round(0.25 + 0.5 * parts["span"], 3),
                facts={"date": day.isoformat(), "figure": label, "value": round(value, 2),
                       "unit": unit, "direction": word,
                       "previous_date": last_beaten.isoformat() if last_beaten else None,
                       "previous_value": round(history[last_beaten][key], 2) if last_beaten
                       else None, "days_spanned": span, "data_since": first_day.isoformat()},
                title=f"{label.capitalize()} {value:.2f} {unit}: {phrase}",
            ))  # fmt: skip
    return stories


STREAK_MILESTONES = {3, 5, 7, 10, 14, 21, 28}


def _streak_length(day: date, history: dict[date, dict], key: str, inside) -> int:
    n, d = 0, day
    while d in history and key in history[d] and inside(history[d][key]):
        n += 1
        d -= timedelta(days=1)
    return n


def streak_stories(day: date, history: dict[date, dict]) -> list[Story]:
    """Runs of days among the top or bottom 10 % of the history (thresholds from days up to
    `day` only). A run is news when it reaches a milestone (3, 5, 7, 10, 14, 21, 28 days,
    then every 7) and when a run of 5+ days ends, not on every day in between."""
    stories = []
    for key, (label, unit) in DAILY_FIGURES.items():
        values = [h[key] for d, h in history.items() if key in h and d <= day]  # no peeking
        if len(values) < 90 or key not in history.get(day, {}):
            continue
        s = sorted(values)
        low, high = s[len(s) // 10], s[-(len(s) // 10) - 1]
        tests = (("highest", lambda v, x=high: v >= x), ("lowest", lambda v, x=low: v <= x))
        for word, inside in tests:
            group = f"{word} 10 % of days since {min(history):%-d %B %Y}"
            n = _streak_length(day, history, key, inside)
            if n >= STREAK_MIN_DAYS and (n in STREAK_MILESTONES or n % 7 == 0):
                parts = {"length": _clip((n - 2) / 12)}
                stories.append(Story(
                    kind="streak", score_parts=parts, score=round(0.3 + 0.4 * parts["length"], 3),
                    facts={"date": day.isoformat(), "figure": label, "days_in_a_row": n,
                           "group": group, "unit": unit,
                           "threshold": round(high if word == "highest" else low, 2)},
                    title=f"Day {n} in a row among the {word} 10 % for {label}",
                ))  # fmt: skip
            ended = _streak_length(day - timedelta(days=1), history, key, inside)
            if n == 0 and ended >= 5:
                parts = {"length": _clip((ended - 2) / 12)}
                stories.append(Story(
                    kind="streak_end", score_parts=parts,
                    score=round(0.35 + 0.4 * parts["length"], 3),
                    facts={"date": day.isoformat(), "figure": label, "run_days": ended,
                           "group": group, "value": round(history[day][key], 2), "unit": unit},
                    title=(f"A run of {ended} days among the {word} 10 % for {label} ended: "
                           f"{history[day][key]:.2f} {unit}"),
                ))  # fmt: skip
    return stories


def shift_stories(day: date, history: dict[date, dict]) -> list[Story]:
    """Unusually large changes from the day before, against the history's day-to-day changes."""
    stories = []
    yesterday = day - timedelta(days=1)
    for key, (label, unit) in DAILY_FIGURES.items():
        if key not in history.get(day, {}) or key not in history.get(yesterday, {}):
            continue
        changes = [history[d][key] - history[d - timedelta(days=1)][key] for d in history
                   if d <= yesterday and key in history[d]
                   and key in history.get(d - timedelta(days=1), {})]  # fmt: skip
        if len(changes) < 60:
            continue
        change = history[day][key] - history[yesterday][key]
        scale = robust_scale(changes)
        z = (change - median(changes)) / scale if scale else 0.0
        if abs(z) < SHIFT_Z:
            continue
        bigger = sum(abs(c) >= abs(change) for c in changes)
        word = "rose" if change > 0 else "fell"
        parts = {"surprise": _clip((abs(z) - SHIFT_Z) / 4)}
        stories.append(Story(
            kind="shift", score_parts=parts, score=round(0.35 + 0.4 * parts["surprise"], 3),
            facts={"date": day.isoformat(), "figure": label, "unit": unit,
                   "value": round(history[day][key], 2),
                   "previous_day_value": round(history[yesterday][key], 2),
                   "change": round(change, 2), "robust_z": round(z, 2),
                   "days_with_a_larger_change": bigger, "days_compared": len(changes)},
            title=(f"{label.capitalize()} {word} by {abs(change):.2f} {unit} in one day, "
                   f"to {history[day][key]:.2f}"),
        ))  # fmt: skip
    return stories


# ---------- all candidates of a day, ranked ----------


def day_stories(data: DataProvider, day: date, history: dict[date, dict] | None = None,
                ) -> list[Story]:  # fmt: skip
    """Every candidate story for `day`, best first. `history`: from load_history (optional)."""
    start = day - timedelta(days=WINDOW_DAYS)
    price = data.day_ahead_price(start, day).get_series("day_ahead_price")
    try:
        generation = data.public_power(start, day)
    except (NoDataError, EnergyChartsError):
        generation = None
    residual = generation.get_series("residual_load") if generation is not None else None
    stories = price_stories(day, price, residual)
    negative = negative_price_story(day, price)
    if negative is not None:
        stories.append(negative)
    if generation is not None:
        stories += lull_stories(day, generation)
        try:
            from gridagent.monitor.forecasts import miss_stories  # forecasts builds on this module

            stories += miss_stories(day, generation, data.forecast(start, day))
        except (NoDataError, EnergyChartsError):
            pass  # no forecasts for this period: no forecast-miss stories
    if history:
        stories += record_stories(day, history) + streak_stories(day, history)
        stories += shift_stories(day, history)
    return sorted(stories, key=lambda s: -s.score)
