"""The daily briefing: yesterday's key figures, notable events and the plan for the
next day with known prices. Everything here is plain calculation; only the short
explanations of price events (explain.py) use the model.

"Normal" always means the 28 days before the reviewed day, so every figure can be
read as "how unusual was this?".

The events ("stories") come from anomalies.py: robust surprise, the merit order,
records, streaks and day-to-day shifts, ranked by a newsworthiness score.
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import Series, TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.data.sources import source_series
from gridagent.monitor.anomalies import Story, day_stories, load_history
from gridagent.monitor.forecasts import Outlook, outlook
from gridagent.monitor.nrw import nrw_snapshot
from gridagent.monitor.week import week_outlook
from gridagent.tools.flexibility import Battery, simulate_battery
from gridagent.tools.investigate import BERLIN, hourly
from gridagent.tools.register import PlantRegister

WINDOW_DAYS = 28
MAX_STORIES = 6  # the lead and up to five shorter items
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
    hourly_prices: list[float] = []  # local hours 0..23 (for the chart)


class Briefing(BaseModel):
    day: str  # the reviewed day
    generated_at: str
    figures: list[Figure]
    events: list[Story]  # best first
    plans: list[DayPlan]
    outlooks: list[Outlook] = []  # day-ahead forecasts for today and tomorrow, when published
    not_available: list[str] = []
    sources: list[str]
    cost_usd: float = 0.0
    # For charts: one entry per day of the 4 weeks before and the day itself
    daily: list[dict[str, Any]] = []
    nrw: dict[str, Any] | None = None  # plant register snapshot (monitor/nrw.py)
    week: dict[str, Any] | None = None  # 7-day outlook from Open-Meteo (monitor/week.py)
    story: dict[str, Any] | None = None  # the news story (monitor/writer.py)


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
        hourly_prices=[round(hours[(day, h)], 2) for h in keys],
    )


# ---------- the whole briefing ----------


def build_briefing(data: DataProvider, day: date, today: date,
                   history: dict[date, dict] | None = None) -> Briefing:  # fmt: skip
    """The briefing for `day` (usually yesterday), with plans for `today` and tomorrow
    when their day-ahead prices are published. `history`: daily figures for records and
    streaks (loaded when not given; two years from the database are quick)."""
    start = day - timedelta(days=WINDOW_DAYS)
    prices = data.day_ahead_price(start, day).get_series("day_ahead_price")
    not_available: list[str] = []
    figures: list[Figure] = []
    generation_or_none = None
    try:
        generation_or_none = data.public_power(start, day)
        figures = day_figures(day, prices, generation_or_none)
    except (NoDataError, EnergyChartsError, KeyError) as exc:
        not_available.append(f"generation data ({exc})")
    if history is None:
        history = load_history(data, day)
    events = day_stories(data, day, history)[:MAX_STORIES]

    outlooks = []
    for plan_day in (today, today + timedelta(days=1)):
        try:
            expected = outlook(plan_day, data.forecast(plan_day, plan_day), generation_or_none)
        except (NoDataError, EnergyChartsError):
            expected = None
        if expected is not None:
            outlooks.append(expected)

    plans = []
    for plan_day in (today, today + timedelta(days=1)):
        try:
            future = data.day_ahead_price(plan_day, plan_day).get_series("day_ahead_price")
        except (NoDataError, EnergyChartsError):
            continue  # tomorrow's prices exist only after the auction (around midday)
        plan = day_plan(plan_day, future)
        if plan is not None:
            plans.append(plan)
    daily = [
        {"date": d.isoformat(), **{k: round(v, 2) for k, v in history[d].items()}}
        for d in sorted(history)
        if start <= d <= day
    ]
    try:
        nrw = nrw_snapshot(PlantRegister(data.plant_register()), day)
    except (NoDataError, EnergyChartsError, KeyError, ValueError):
        nrw = None
        not_available.append("plant register")
    week = week_outlook(data, today, {o.day: (o.wind_gwh, o.solar_gwh) for o in outlooks})
    if week is None:
        not_available.append("7-day weather outlook")
    return Briefing(
        day=day.isoformat(),
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        daily=daily,
        nrw=nrw,
        week=week,
        figures=figures,
        events=events,
        plans=plans,
        outlooks=outlooks,
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
