"""Captions written by code: short, exact sentences for the public page.

Every caption is a fixed template filled with numbers from the briefing, so it can
never say more than the data. They work without any model, which makes them the
page's baseline; the AI story (writer.py) is an optional, labelled extra on top.
Associations only: templates say "on the day with", never "because".
"""

from datetime import date
from typing import Any

from gridagent.monitor.anomalies import Story


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B}"


def _num(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def headline(story: Story | None) -> str:
    """A short headline for the lead story (or a quiet day)."""
    if story is None:
        return "A quiet day on the German power market"
    f = story.facts
    templates = {
        "price_spike": lambda: f"Prices reached {_num(f['price_eur_mwh'])} €/MWh, {f['hours']}",
        "price_dip": lambda: f"Prices fell to {_num(f['price_eur_mwh'])} €/MWh, {f['hours']}",
        "negative_prices": lambda: f"Negative prices for {f['negative_hours']:g} hours",
        "lull": lambda: f"Wind and sun covered {f['wind_and_solar_share_of_demand_pct']} % "
        f"of demand, {f['hours']}",
        "record": lambda: f"{f['figure'].capitalize()} at its {f['direction']} "
        + (f"since {_day(f['previous_date'])} {f['previous_date'][:4]}" if f["previous_date"]
           else f"since at least {f['compared_back_to'][:4]}"),
        "streak": lambda: f"Day {f['days_in_a_row']} in a row among the "
        f"{f['group'].split(' 10 %')[0]} 10 % for {f['figure']}",
        "streak_end": lambda: f"A {f['run_days']}-day run of "
        f"{f['group'].split(' 10 %')[0]} {f['figure']} has ended",
        "shift": lambda: f"{f['figure'].capitalize()} "
        f"{'rose' if f['change'] > 0 else 'fell'} by {_num(abs(f['change']))} {f['unit']} "
        "in a day",
        "forecast_miss": lambda: f"{f['figure'].capitalize()} came in "
        f"{abs(f['miss_pct'] or 0):.1f} % {'above' if f['miss_gwh'] > 0 else 'below'} forecast",
    }  # fmt: skip
    return templates.get(story.kind, lambda: story.title)()


def lead_caption(briefing: dict[str, Any]) -> str:
    """One or two sentences under the headline: the day's price against its normal."""
    figures = {f["name"]: f for f in briefing["figures"]}
    price = figures.get("Average day-ahead price")
    if not price or price["value"] is None:
        return f"Key figures for {_day(briefing['day'])}."
    text = (f"On {_day(briefing['day'])} the average day-ahead price was "
            f"{_num(price['value'])} €/MWh, against {_num(price['normal'])} on a normal day "
            "of the four weeks before.")  # fmt: skip
    wind = figures.get("Wind energy")
    if wind and wind["value"] is not None and wind["normal"] is not None:
        text += (f" Wind produced {_num(wind['value'], 1)} GWh (normal "
                 f"{_num(wind['normal'], 1)}).")  # fmt: skip
    return text


def four_weeks_caption(daily: list[dict[str, Any]]) -> str:
    """The dearest and cheapest day of the period, with their wind, side by side."""
    days = [d for d in daily if "price_mean" in d and "wind_gwh" in d]
    if len(days) < 7:
        return ""
    dear = max(days, key=lambda d: d["price_mean"])
    cheap = min(days, key=lambda d: d["price_mean"])
    return (f"The dearest day was {_day(dear['date'])}: {_num(dear['price_mean'])} €/MWh "
            f"with {_num(dear['wind_gwh'], 1)} GWh of wind. The cheapest was "
            f"{_day(cheap['date'])}: {_num(cheap['price_mean'])} €/MWh with "
            f"{_num(cheap['wind_gwh'], 1)} GWh of wind.")  # fmt: skip


def plan_caption(plan: dict[str, Any]) -> str:
    cheap, dear = _num(plan["cheapest_price"]), _num(plan["dearest_price"])
    return (f"Published day-ahead prices for {_day(plan['day'])} are lowest from "
            f"{plan['cheapest_hours']} ({cheap} €/MWh on average) and highest from "
            f"{plan['dearest_hours']} ({dear} €/MWh).")  # fmt: skip


def nrw_caption(nrw: dict[str, Any]) -> str:
    years = {y["year"]: y["mw"] for y in nrw["wind_added_per_year"]}
    year = nrw["year"]
    text = f"{_num(years.get(year, 0), 0)} MW of onshore wind commissioned in {year} so far"
    if year - 1 in years:
        text += f", after {_num(years[year - 1], 0)} MW in {year - 1}"
    return text + (f". Plant register of {_day(nrw['register_date'])}; recent months are "
                   "revised upward as late registrations arrive.")  # fmt: skip
