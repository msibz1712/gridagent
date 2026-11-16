"""Clock-hour helpers shared by the briefing, the anomaly engine and the price forecast.

One place for the conversion from UTC timestamps to German local hours, because daylight
saving makes it easy to get wrong: the spring day has 23 local hours (no 02:00), the
autumn day has two 02:00 hours (their values are averaged into one).
"""

from collections import defaultdict
from datetime import date

from gridagent.data.models import Series
from gridagent.tools.investigate import BERLIN, hourly


def by_local_hour(series: Series) -> dict[tuple[date, int], float]:
    """(local date, local hour) -> hourly mean; the repeated autumn hour: both hours' mean."""
    out: dict[tuple[date, int], list[float]] = defaultdict(list)
    for hour, value in hourly(series).items():
        local = hour.astimezone(BERLIN)
        out[(local.date(), local.hour)].append(value)
    return {k: sum(v) / len(v) for k, v in out.items()}


def runs(hours: list[int]) -> list[tuple[int, int]]:
    """Consecutive hours -> (first, end exclusive)."""
    out: list[tuple[int, int]] = []
    for h in sorted(hours):
        if out and out[-1][1] == h:
            out[-1] = (out[-1][0], h + 1)
        else:
            out.append((h, h + 1))
    return out


def hours_text(first: int, end: int) -> str:
    return f"{first:02d}:00–{end:02d}:00"


def best_blocks(hours: dict[int, float], size: int = 3) -> tuple[int, float, int, float]:
    """(start, mean) of the cheapest and of the dearest `size` consecutive clock hours.

    Only blocks of hours that all exist count, so a missing hour (or the spring DST gap)
    never joins two separate hours into one block."""
    starts = [h for h in sorted(hours) if all(h + i in hours for i in range(size))]
    if not starts:
        raise ValueError(f"No {size} consecutive hours with values.")
    mean = {h: sum(hours[h + i] for i in range(size)) / size for h in starts}
    cheap = min(starts, key=lambda h: mean[h])
    dear = max(starts, key=lambda h: mean[h])
    return cheap, mean[cheap], dear, mean[dear]


def clock_list(hours: dict[int, float], digits: int = 2) -> list[float | None]:
    """24 values by clock hour, None where an hour is missing (e.g. 02:00 on the spring
    DST day): charts then put every bar at its true hour."""
    return [round(hours[h], digits) if h in hours else None for h in range(24)]
