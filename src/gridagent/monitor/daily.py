"""The daily briefing: yesterday's key figures, notable events and the plan for the
next day with known prices. Everything here is plain calculation; only the short
explanations of price events (explain.py) use the model.

"Normal" always means the 28 days before the reviewed day, so every figure can be
read as "how unusual was this?".

Event rules (fixed, stated in every event, so a reader can judge them):
- price spike / price dip: hourly price at least 2.5 standard deviations above
  (below) the same hour on comparable days (weekdays with weekdays, weekends with
  weekends) and at least 50 EUR/MWh away from it; consecutive hours form one event.
- negative prices: any quarter-hour (or hour, before Oct 2025) below 0 EUR/MWh.
- wind-and-sun lull: wind + solar below 10 % of demand for 4 or more hours in a row.
- 4-week high: renewable share or solar / wind energy the highest of the 28 days.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import Series, TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.tools.flexibility import Battery, simulate_battery
from gridagent.tools.investigate import BERLIN, hourly, is_weekend

WINDOW_DAYS = 28
SPIKE_Z = 2.5
SPIKE_MIN_EUR = 50.0
LULL_SHARE = 0.10
LULL_HOURS = 4
EXPLAIN_EVENTS = 2  # price events per day that get a model-written explanation


class Figure(BaseModel):
    """One key figure of the day, with its 4-week normal."""

    name: str
    value: float | None
    unit: str
    normal: float | None = None  # mean of the daily values of the 28 days before
    at: str | None = None  # local time, for a min or max

    @property
    def difference(self) -> float | None:
        if self.value is None or self.normal is None:
            return None
        return round(self.value - self.normal, 2)

    @property
    def change_pct(self) -> float | None:
        """Only for quantities that are always positive: a percentage change of a value
        that can change sign (imports) or start near zero (negative hours) means nothing."""
        if self.unit in ("h", "%") or "imports" in self.name or "Lowest" in self.name:
            return None
        if self.value is None or self.normal is None or self.normal <= 0 or self.value < 0:
            return None
        return round(100 * (self.value - self.normal) / self.normal, 1)


class Event(BaseModel):
    kind: str  # price_spike, price_dip, negative_prices, lull, four_week_high
    title: str  # one plain sentence, from the numbers
    rule: str
    severity: float  # for ordering: z-score or a scale of the rule
    from_hour: int | None = None
    to_hour: int | None = None  # exclusive
    values: dict[str, float] = {}
    explanation: str | None = None  # by the model (explain.py), with its method steps
    explanation_steps: list[str] = []


class DayPlan(BaseModel):
    """What the known day-ahead prices of one day mean for flexible users."""

    day: str
    mean_price: float
    cheapest_hours: str
    cheapest_price: float
    dearest_hours: str
    dearest_price: float
    negative_hours: float
    battery_profit_eur_per_mw: float  # 1 MW / 2 MWh, 90 % round trip


class Briefing(BaseModel):
    day: str  # the reviewed day
    generated_at: str
    figures: list[Figure]
    events: list[Event]
    plans: list[DayPlan]
    not_available: list[str] = []
    sources: list[str]
    cost_usd: float = 0.0


# ---------- helpers ----------


def _day_points(series: Series, day: date) -> Series:
    return series.model_copy(
        update={
            "points": [p for p in series.points if p.timestamp.astimezone(BERLIN).date() == day]
        }
    )


def _values(series: Series) -> list[float]:
    return [p.value for p in series.points if p.value is not None]


def _energy_gwh(series: Series) -> float | None:
    values = _values(series)
    if not values or not series.interval_minutes:
        return None
    return sum(values) * series.interval_minutes / 60 / 1000


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _hours_text(from_hour: int, to_hour: int) -> str:
    return f"{from_hour:02d}:00–{to_hour:02d}:00"


def _runs(hours: list[int]) -> list[tuple[int, int]]:
    """Consecutive local hours -> (first, end exclusive)."""
    runs: list[tuple[int, int]] = []
    for h in sorted(hours):
        if runs and runs[-1][1] == h:
            runs[-1] = (runs[-1][0], h + 1)
        else:
            runs.append((h, h + 1))
    return runs


def _local_hourly(series: Series) -> dict[tuple[date, int], float]:
    """(local date, local hour) -> hourly mean (the repeated DST hour: both hours' mean)."""
    by_key: dict[tuple[date, int], list[float]] = defaultdict(list)
    for hour, value in hourly(series).items():
        local = hour.astimezone(BERLIN)
        by_key[(local.date(), local.hour)].append(value)
    return {k: sum(v) / len(v) for k, v in by_key.items()}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: list[float]) -> float:
    m = _mean(values) or 0
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5 if len(values) > 1 else 0


# ---------- key figures ----------


def day_figures(day: date, prices: Series, generation: TimeSeriesResponse) -> list[Figure]:
    """Key figures of `day`, each with the mean of its daily value over the 28 days before."""
    window = _days(day - timedelta(days=WINDOW_DAYS), day - timedelta(days=1))

    def per_day(fn) -> tuple[float | None, float | None]:
        values = [fn(d) for d in window]
        values = [v for v in values if v is not None]
        return fn(day), (sum(values) / len(values) if values else None)

    def price_stat(d: date, kind: str) -> float | None:
        values = _values(_day_points(prices, d))
        if not values:
            return None
        return {"mean": sum(values) / len(values), "max": max(values), "min": min(values)}[kind]

    def negative_hours(d: date) -> float | None:
        values = _values(_day_points(prices, d))
        if not values or not prices.interval_minutes:
            return None
        return sum(v < 0 for v in values) * prices.interval_minutes / 60

    def gen_energy(source: str):
        series = source_series(generation, source)
        return lambda d: _energy_gwh(_day_points(series, d))

    def share(source: str, of: str):
        part, whole = gen_energy(source), gen_energy(of)
        return lambda d: (100 * part(d) / whole(d)) if part(d) is not None and whole(d) else None

    def residual_peak(d: date) -> float | None:
        values = _values(_day_points(generation.get_series("residual_load"), d))
        return max(values) / 1000 if values else None

    def imports(d: date) -> float | None:
        return _energy_gwh(
            _day_points(generation.get_series("cross_border_electricity_trading"), d)
        )

    figures = []
    for name, unit, fn in [
        ("Average day-ahead price", "EUR / MWh", lambda d: price_stat(d, "mean")),
        ("Highest price", "EUR / MWh", lambda d: price_stat(d, "max")),
        ("Lowest price", "EUR / MWh", lambda d: price_stat(d, "min")),
        ("Hours with negative prices", "h", negative_hours),
        ("Renewable share of public net generation", "%", share("renewable", "total")),
        ("Wind energy", "GWh", gen_energy("wind")),
        ("Solar energy", "GWh", gen_energy("solar")),
        ("Demand (load)", "GWh", gen_energy("load")),
        ("Peak residual load (demand minus wind and solar)", "GW", residual_peak),
        ("Net commercial imports (+ import, - export)", "GWh", imports),
    ]:
        value, normal = per_day(fn)
        figure = Figure(name=name, value=_round(value), unit=unit, normal=_round(normal))
        if name in ("Highest price", "Lowest price") and value is not None:
            points = [p for p in _day_points(prices, day).points if p.value == value]
            figure.at = points[0].timestamp.astimezone(BERLIN).strftime("%H:%M") if points else None
        figures.append(figure)
    return figures


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


# ---------- events ----------


def price_events(day: date, prices: Series) -> list[Event]:
    """Spikes and dips against the same hour on comparable days, and negative prices."""
    hourly_prices = _local_hourly(prices)
    window = [d for d in _days(day - timedelta(days=WINDOW_DAYS), day - timedelta(days=1))
              if is_weekend(d) == is_weekend(day)]  # fmt: skip
    flagged: dict[str, dict[int, tuple[float, float, float]]] = {"spike": {}, "dip": {}}
    for hour in range(24):
        if (day, hour) not in hourly_prices:
            continue
        normal = [hourly_prices[(d, hour)] for d in window if (d, hour) in hourly_prices]
        if len(normal) < 5:
            continue
        mean, spread = _mean(normal) or 0, _std(normal)
        price = hourly_prices[(day, hour)]
        if not spread:
            continue
        z = (price - mean) / spread
        if z >= SPIKE_Z and price - mean >= SPIKE_MIN_EUR:
            flagged["spike"][hour] = (price, mean, z)
        elif z <= -SPIKE_Z and mean - price >= SPIKE_MIN_EUR:
            flagged["dip"][hour] = (price, mean, z)

    events = []
    rule = (f"hourly price ≥ {SPIKE_Z} standard deviations and ≥ {SPIKE_MIN_EUR:.0f} EUR/MWh "
            "away from the same hour on comparable days of the 4 weeks before")  # fmt: skip
    for kind, hours in flagged.items():
        for first, end in _runs(list(hours)):
            group = [hours[h] for h in range(first, end)]
            price = sum(g[0] for g in group) / len(group)
            normal = sum(g[1] for g in group) / len(group)
            z = max(abs(g[2]) for g in group)
            word = "above" if kind == "spike" else "below"
            events.append(Event(
                kind=f"price_{kind}", rule=rule, severity=round(z, 2),
                from_hour=first, to_hour=end,
                title=(f"{_hours_text(first, end)}: the price averaged {price:.2f} EUR/MWh, "
                       f"{abs(price - normal):.2f} {word} the usual {normal:.2f} for these hours"),
                values={"price": round(price, 2), "normal": round(normal, 2),
                        "z_score": round(z, 2)},
            ))  # fmt: skip

    day_prices = _day_points(prices, day)
    negative = [p for p in day_prices.points if p.value is not None and p.value < 0]
    if negative and prices.interval_minutes:
        hours = len(negative) * prices.interval_minutes / 60
        lowest = min(p.value for p in negative)  # type: ignore[type-var]
        local_hours = [p.timestamp.astimezone(BERLIN).hour for p in negative]
        first, end = min(local_hours), max(local_hours) + 1
        events.append(Event(
            kind="negative_prices", rule="any day-ahead price below 0 EUR/MWh",
            severity=round(1 + hours, 2), from_hour=first, to_hour=end,
            title=(f"Negative prices for {hours:g} hours between {_hours_text(first, end)}, "
                   f"down to {lowest:.2f} EUR/MWh"),
            values={"hours": hours, "lowest": round(lowest, 2)},
        ))  # fmt: skip
    return events


def supply_events(day: date, generation: TimeSeriesResponse) -> list[Event]:
    """Wind-and-sun lulls and 4-week highs of renewable output."""
    events = []
    wind = _local_hourly(source_series(generation, "wind"))
    solar = _local_hourly(source_series(generation, "solar"))
    load = _local_hourly(source_series(generation, "load"))
    lull = [h for h in range(24) if (day, h) in load and (day, h) in wind and (day, h) in solar
            and wind[(day, h)] + solar[(day, h)] < LULL_SHARE * load[(day, h)]]  # fmt: skip
    for first, end in _runs(lull):
        if end - first < LULL_HOURS:
            continue
        hours = range(first, end)
        share = (
            100
            * sum(wind[(day, h)] + solar[(day, h)] for h in hours)
            / sum(load[(day, h)] for h in hours)
        )
        events.append(Event(
            kind="lull", severity=round(1 + (end - first) / 2, 2), from_hour=first, to_hour=end,
            rule=f"wind + solar below {LULL_SHARE:.0%} of demand for {LULL_HOURS}+ hours in a row",
            title=(f"{_hours_text(first, end)}: wind and solar covered only {share:.1f} % "
                   "of demand"),
            values={"hours": end - first, "wind_solar_share_pct": round(share, 1)},
        ))  # fmt: skip

    window = _days(day - timedelta(days=WINDOW_DAYS), day)
    for source, label in (("solar", "Solar energy"), ("wind", "Wind energy")):
        series = source_series(generation, source)
        daily = {d: _energy_gwh(_day_points(series, d)) for d in window}
        daily = {d: v for d, v in daily.items() if v is not None}
        if day in daily and len(daily) > 7 and daily[day] == max(daily.values()):
            events.append(Event(
                kind="four_week_high", severity=1.0,
                rule="the highest daily value of the last 4 weeks",
                title=f"{label} {daily[day]:.2f} GWh: the highest of the last 4 weeks",
                values={"gwh": round(daily[day], 2)},
            ))  # fmt: skip
    return events


# ---------- plan for a day with known prices ----------


def day_plan(day: date, prices: Series, block_hours: int = 3) -> DayPlan | None:
    """Cheapest and dearest `block_hours` in a row, and a 1 MW / 2 MWh battery's profit."""
    hours = _local_hourly(_day_points(prices, day))
    keys = sorted(h for (d, h) in hours if d == day)
    if len(keys) < 20:
        return None
    blocks = [
        (h, _mean([hours[(day, x)] for x in range(h, h + block_hours)]))
        for h in keys
        if all((day, x) in hours for x in range(h, h + block_hours))
    ]
    cheap = min(blocks, key=lambda b: b[1])
    dear = max(blocks, key=lambda b: b[1])
    series = _day_points(prices, day)
    battery = simulate_battery(series, Battery(power_mw=1, capacity_mwh=2)).summary()
    values = _values(series)
    return DayPlan(
        day=day.isoformat(),
        mean_price=round(sum(values) / len(values), 2),
        cheapest_hours=_hours_text(cheap[0], cheap[0] + block_hours),
        cheapest_price=round(cheap[1] or 0, 2),
        dearest_hours=_hours_text(dear[0], dear[0] + block_hours),
        dearest_price=round(dear[1] or 0, 2),
        negative_hours=sum(v < 0 for v in values) * (prices.interval_minutes or 60) / 60,
        battery_profit_eur_per_mw=battery["profit_eur_per_mw"],
    )


# ---------- the whole briefing ----------


def build_briefing(data: DataProvider, day: date, today: date) -> Briefing:
    """The briefing for `day` (usually yesterday), with plans for `today` and tomorrow
    when their day-ahead prices are published."""
    start = day - timedelta(days=WINDOW_DAYS)
    prices = data.day_ahead_price(start, day).get_series("day_ahead_price")
    not_available: list[str] = []
    figures: list[Figure] = []
    events = price_events(day, prices)
    try:
        generation = data.public_power(start, day)
        figures = day_figures(day, prices, generation)
        events += supply_events(day, generation)
    except (NoDataError, EnergyChartsError, KeyError) as exc:
        not_available.append(f"generation data ({exc})")
    events.sort(key=lambda e: -e.severity)

    plans = []
    for plan_day in (today, today + timedelta(days=1)):
        try:
            future = data.day_ahead_price(plan_day, plan_day).get_series("day_ahead_price")
        except (NoDataError, EnergyChartsError):
            continue  # tomorrow's prices exist only after the auction (around midday)
        plan = day_plan(plan_day, future)
        if plan is not None:
            plans.append(plan)
    return Briefing(
        day=day.isoformat(),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        figures=figures,
        events=events,
        plans=plans,
        not_available=not_available,
        sources=["Energy-Charts.info (Fraunhofer ISE)",
                 "Bundesnetzagentur | SMARD.de via Energy-Charts.info (prices)"],
    )  # fmt: skip


def as_dict(briefing: Briefing) -> dict[str, Any]:
    """JSON for storage and the website, with each figure's change against its normal."""
    data = briefing.model_dump()
    for figure, raw in zip(briefing.figures, data["figures"], strict=True):
        raw["difference"] = figure.difference
        raw["change_pct"] = figure.change_pct
    return data
