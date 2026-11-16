"""What is flexibility worth? A battery on the day-ahead market, and shiftable load.

Battery (`simulate_battery`)
----------------------------
Each day is optimised on its own with dynamic programming, starting and ending
empty. That is realistic for the day-ahead market: the auction publishes all
of tomorrow's prices at midday, so a battery trading only day-ahead knows the
whole day before it commits. The result is still an upper bound: no intraday or
balancing markets, no grid fees, no bidding risk.

The state is the energy stored, on a grid of `step` MWh. In each interval the
battery may move up or down by whole steps, at most `power * interval` (the
power limit applies to the energy entering or leaving the cells). Losses are
split evenly: round-trip efficiency 0.9 means sqrt(0.9) when charging and
again when discharging. When capacity and one interval's energy are multiples
of the step (e.g. 10 MW / 20 MWh at 15 minutes) the grid contains the true
optimum, so the result is exact; otherwise capacity is rounded down slightly
and `exact` is False.

Shiftable load (`plan_flexible_load`)
-------------------------------------
Pick the cheapest intervals until the energy is used, at most `max_power` per
interval, and compare with running evenly over the whole period.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from gridagent.data.models import Series

GRID_STEPS_PER_INTERVAL = 4  # power in 25 % steps
MAX_LEVELS = 160  # keeps a 93-day run fast; longer batteries get a coarser grid
MAX_DURATION_HOURS = 24  # capacity / power; a day-ahead battery cycles within a day


@dataclass(frozen=True)
class Battery:
    power_mw: float
    capacity_mwh: float
    round_trip_efficiency: float = 0.9
    wear_eur_per_mwh: float = 0.0  # per MWh discharged to the grid (degradation)

    def check(self) -> None:
        if self.power_mw <= 0 or self.capacity_mwh <= 0:
            raise ValueError("power_mw and capacity_mwh must be positive.")
        if not 0.5 <= self.round_trip_efficiency <= 1:
            raise ValueError("round_trip_efficiency must be between 0.5 and 1 (e.g. 0.9).")
        if self.wear_eur_per_mwh < 0:
            raise ValueError("wear_eur_per_mwh cannot be negative.")
        if self.capacity_mwh / self.power_mw > MAX_DURATION_HOURS:
            raise ValueError(
                f"capacity / power is over {MAX_DURATION_HOURS} h: not a day-ahead battery."
            )


@dataclass(frozen=True)
class Grid:
    """The discretisation: energy levels 0..levels * step, moves of up to max_move steps."""

    step: float
    levels: int
    max_move: int
    exact: bool


def make_grid(battery: Battery, interval_h: float) -> Grid:
    per_interval = battery.power_mw * interval_h  # MWh in or out per interval at full power
    step = min(per_interval, battery.capacity_mwh) / GRID_STEPS_PER_INTERVAL
    step = max(step, battery.capacity_mwh / MAX_LEVELS)
    levels = math.floor(battery.capacity_mwh / step + 1e-9)
    max_move = max(1, math.floor(per_interval / step + 1e-9))
    exact = all(_is_multiple(x, step) for x in (battery.capacity_mwh, per_interval))
    return Grid(step=step, levels=levels, max_move=max_move, exact=exact)


def _is_multiple(x: float, step: float) -> bool:
    return abs(x / step - round(x / step)) < 1e-6


@dataclass
class DayResult:
    day: date
    stored: list[float]  # MWh in the battery at the END of each interval
    grid_mwh: list[float]  # energy from (+) or to (-) the grid in each interval
    profit_eur: float


def optimise_day(day: date, prices: Sequence[float | None], battery: Battery, grid: Grid,
                 ) -> DayResult:  # fmt: skip
    """Best schedule for one day, empty at start and end. Missing price = no trading."""
    eta = math.sqrt(battery.round_trip_efficiency)
    n = len(prices)
    minus_inf = float("-inf")
    # best[t][s]: highest profit from interval t to the end of the day, starting at level s
    best = [[minus_inf] * (grid.levels + 1) for _ in range(n + 1)]
    best[n][0] = 0.0  # must end empty
    choice = [[0] * (grid.levels + 1) for _ in range(n)]
    for t in range(n - 1, -1, -1):
        price = prices[t]
        moves = range(-grid.max_move, grid.max_move + 1) if price is not None else (0,)
        nxt, row, picks = best[t + 1], best[t], choice[t]
        for s in range(grid.levels + 1):
            top, pick = minus_inf, 0
            for move in moves:
                target = s + move
                if target < 0 or target > grid.levels or nxt[target] == minus_inf:
                    continue
                value = nxt[target] + _cash(move * grid.step, price, eta, battery)
                if value > top + 1e-9:
                    top, pick = value, move
            row[s], picks[s] = top, pick
    level, stored, grid_mwh = 0, [], []
    for t in range(n):
        move = choice[t][level]
        level += move
        stored.append(level * grid.step)
        grid_mwh.append(_grid_energy(move * grid.step, eta))
    return DayResult(day=day, stored=stored, grid_mwh=grid_mwh, profit_eur=best[0][0])


def _grid_energy(delta_stored: float, eta: float) -> float:
    """Charging 1 MWh into the cells takes 1/eta from the grid; discharging gives eta."""
    return delta_stored / eta if delta_stored > 0 else delta_stored * eta


def _cash(delta_stored: float, price: float | None, eta: float, battery: Battery) -> float:
    if delta_stored == 0 or price is None:
        return 0.0
    energy = _grid_energy(delta_stored, eta)  # + bought, - sold
    wear = battery.wear_eur_per_mwh * -energy if energy < 0 else 0.0
    return -energy * price - wear


@dataclass
class BatteryRun:
    battery: Battery
    grid: Grid
    interval_h: float
    days: list[DayResult]
    timestamps: list[datetime]
    prices: list[float | None]

    @property
    def stored(self) -> list[float]:
        return [x for d in self.days for x in d.stored]

    @property
    def grid_mwh(self) -> list[float]:
        return [x for d in self.days for x in d.grid_mwh]

    def summary(self) -> dict[str, object]:
        energy = self.grid_mwh
        charged = sum(e for e in energy if e > 0)
        discharged = -sum(e for e in energy if e < 0)
        bought = sum(e * p for e, p in zip(energy, self.prices, strict=True) if e > 0 and p)
        sold = -sum(e * p for e, p in zip(energy, self.prices, strict=True) if e < 0 and p)
        profit = sum(d.profit_eur for d in self.days)
        ranked = sorted(self.days, key=lambda d: d.profit_eur)
        stored_in = sum(e for e in self._stored_moves() if e > 0)
        return {
            "profit_eur": round(profit, 2),
            "profit_eur_per_mw": round(profit / self.battery.power_mw, 2),
            "profit_eur_per_day": round(profit / len(self.days), 2),
            "revenue_eur": round(sold, 2),
            "charging_cost_eur": round(bought, 2),
            "wear_cost_eur": round(discharged * self.battery.wear_eur_per_mwh, 2),
            "energy_charged_mwh": round(charged, 2),
            "energy_discharged_mwh": round(discharged, 2),
            "losses_mwh": round(charged - discharged, 2),
            "full_cycles": round(stored_in / self.battery.capacity_mwh, 2),
            "full_cycles_per_day": round(stored_in / self.battery.capacity_mwh / len(self.days), 2),
            "average_buy_price_eur_per_mwh": round(bought / charged, 2) if charged else None,
            "average_sell_price_eur_per_mwh": round(sold / discharged, 2) if discharged else None,
            # sell minus buy price: answers quoted it from mental arithmetic (live run, 0.12.0)
            "average_spread_eur_per_mwh": round(sold / discharged - bought / charged, 2)
            if charged and discharged else None,
            "best_day": {"date": ranked[-1].day.isoformat(),
                         "profit_eur": round(ranked[-1].profit_eur, 2)},
            "worst_day": {"date": ranked[0].day.isoformat(),
                          "profit_eur": round(ranked[0].profit_eur, 2)},
            "days": len(self.days),
        }  # fmt: skip

    def _stored_moves(self) -> list[float]:
        moves = []
        for day in self.days:
            previous = 0.0
            for level in day.stored:
                moves.append(level - previous)
                previous = level
        return moves


def simulate_battery(prices: Series, battery: Battery) -> BatteryRun:
    """Optimise every local calendar day of a price series."""
    battery.check()
    if not prices.interval_minutes:
        raise ValueError("The price data has no fixed interval.")
    interval_h = prices.interval_minutes / 60
    grid = make_grid(battery, interval_h)
    by_day: dict[date, list[float | None]] = {}
    for point in prices.points:
        by_day.setdefault(point.timestamp.date(), []).append(point.value)
    if not by_day:
        raise ValueError("No prices in this ref.")
    days = [optimise_day(day, values, battery, grid) for day, values in by_day.items()]
    return BatteryRun(battery=battery, grid=grid, interval_h=interval_h, days=days,
                      timestamps=[p.timestamp for p in prices.points],
                      prices=[p.value for p in prices.points])  # fmt: skip


# ---------- shiftable load ----------


def plan_load(prices: Series, energy_mwh: float, max_power_mw: float) -> dict[str, object]:
    """Cheapest intervals for `energy_mwh`, at most `max_power_mw`; vs. running evenly."""
    if energy_mwh <= 0 or max_power_mw <= 0:
        raise ValueError("energy_mwh and max_power_mw must be positive.")
    if not prices.interval_minutes:
        raise ValueError("The price data has no fixed interval.")
    interval_h = prices.interval_minutes / 60
    known: list[tuple[int, float]] = [
        (i, p.value) for i, p in enumerate(prices.points) if p.value is not None
    ]
    possible = len(known) * max_power_mw * interval_h
    if energy_mwh > possible + 1e-9:
        raise ValueError(
            f"{energy_mwh} MWh does not fit: at {max_power_mw} MW the period allows at most "
            f"{round(possible, 2)} MWh."
        )
    use: dict[int, float] = {}
    left = energy_mwh
    for i, _ in sorted(known, key=lambda item: (item[1], item[0])):
        if left <= 1e-9:
            break
        use[i] = min(max_power_mw * interval_h, left)
        left -= use[i]
    price_at = dict(known)
    cost = sum(mwh * price_at[i] for i, mwh in use.items())
    even_price = sum(v for _, v in known) / len(known)  # same energy in every interval
    even_cost = energy_mwh * even_price
    return {
        "energy_mwh": energy_mwh,
        "max_power_mw": max_power_mw,
        "hours_running": _r(sum(use.values()) / max_power_mw),
        "cost_eur": _r(cost),
        "average_price_eur_per_mwh": _r(cost / energy_mwh),
        "average_price_ct_per_kwh": _r(cost / energy_mwh / 10, 3),
        "even_running_cost_eur": _r(even_cost),
        "even_running_average_price_eur_per_mwh": _r(even_price),
        "saving_eur": _r(even_cost - cost),
        # a percentage of a cost <= 0 (negative prices) means nothing
        "saving_pct": _r(100 * (even_cost - cost) / even_cost) if even_cost > 0 else None,
        "blocks": _blocks(prices, use, interval_h),
    }


MAX_BLOCKS = 24


def _r(value: float, digits: int = 2) -> float:
    return round(value, digits) + 0.0  # + 0.0 turns -0.0 into 0.0


def _blocks(prices: Series, use: dict[int, float], interval_h: float) -> list[dict[str, object]]:
    """Consecutive intervals at the same power -> one block, in local time."""
    blocks: list[dict[str, object]] = []
    previous: int | None = None
    for i in sorted(use):
        mw = round(use[i] / interval_h, 3)
        start = prices.points[i].timestamp
        if previous == i - 1 and blocks and blocks[-1]["power_mw"] == mw:
            blocks[-1]["intervals"] += 1  # type: ignore[operator]
        else:
            blocks.append({"from": start.isoformat(), "power_mw": mw, "intervals": 1})
        previous = i
    for block in blocks:
        hours = block.pop("intervals") * interval_h  # type: ignore[operator]
        start = datetime.fromisoformat(block["from"])  # type: ignore[arg-type]
        block["until"] = (start + timedelta(hours=hours)).isoformat()
        block["hours"] = hours
    if len(blocks) > MAX_BLOCKS:
        return blocks[:MAX_BLOCKS] + [{"note": f"{len(blocks) - MAX_BLOCKS} more blocks"}]
    return blocks
