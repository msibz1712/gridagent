"""M1 demo: answer the three example questions with the data tools only (no LLM).

Run:  uv run python scripts/demo_data_tools.py           # offline, from fixtures
      uv run python scripts/demo_data_tools.py --live    # real API (cached in .cache/)
"""

import json
import sys
from datetime import date
from pathlib import Path

from rich.console import Console

from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient
from gridagent.data.models import TimeSeriesResponse
from gridagent.data.sources import source_series
from gridagent.tools.stats import Operation, compute_stats, daily

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
console = Console()


def load(name: str, fetch) -> TimeSeriesResponse:
    if "--live" in sys.argv:
        return fetch()
    return TimeSeriesResponse.model_validate(json.loads((FIXTURES / name).read_text()))


def main() -> None:
    client = EnergyChartsClient(cache=ResponseCache(ROOT / ".cache" / "energy_charts"))

    # 1. Wind share last week (Mon 2 Nov - Sun 8 Nov 2026)
    week = load(
        "public_power_de_2026-11-02_2026-11-08.json",
        lambda: client.public_power(date(2026, 11, 2), date(2026, 11, 8)),
    )
    share = compute_stats(
        source_series(week, "wind"), Operation.SHARE, total=source_series(week, "total")
    )
    wind_twh = compute_stats(source_series(week, "wind"), Operation.ENERGY).value / 1e6
    console.print(
        f"[bold]Wind share of public net generation, {share.start} to {share.end}:[/] "
        f"{share.value:.1f}% ({wind_twh:.2f} TWh, {share.n_points} quarter-hours, "
        f"{share.n_missing} missing)"
    )

    # 2. When was solar highest in October 2026?
    october = load(
        "public_power_de_2026-10-01_2026-10-31.json",
        lambda: client.public_power(date(2026, 10, 1), date(2026, 10, 31)),
    )
    solar = source_series(october, "solar")
    peak = compute_stats(solar, Operation.MAX)
    best_day = max(daily(solar, Operation.ENERGY), key=lambda d: d.value)
    console.print(
        f"[bold]Solar peak in October 2026:[/] {peak.value:,.0f} MW "
        f"at {peak.timestamp:%Y-%m-%d %H:%M} (local); "
        f"sunniest day {best_day.start} with {best_day.value:,.0f} MWh"
    )

    # 3. Average day-ahead price yesterday (13 Nov 2026)
    price = load(
        "price_DE-LU_2026-11-13.json",
        lambda: client.day_ahead_price(date(2026, 11, 13), date(2026, 11, 13)),
    )
    mean = compute_stats(price.get_series("day_ahead_price"), Operation.MEAN)
    console.print(
        f"[bold]Average day-ahead price DE-LU on {mean.start}:[/] {mean.value:.2f} {mean.unit} "
        f"({mean.n_points} quarter-hours)"
    )
    console.print(f"[dim]Source: Energy-Charts.info (Fraunhofer ISE), {week.license}[/]")


if __name__ == "__main__":
    main()
