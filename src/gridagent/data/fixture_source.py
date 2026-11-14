"""Offline data provider that answers requests from recorded fixtures.

It has the same two methods as `EnergyChartsClient`, so the agent's tools
can't tell the difference. A request is served if one fixture covers the
whole date range; the rows are then cut to exactly that range. Anything
else raises `NoDataError`, just like the real API does for missing data.
"""

import json
from datetime import date
from pathlib import Path
from typing import Protocol

from gridagent.data.client import NoDataError
from gridagent.data.models import TimeSeriesResponse


class DataProvider(Protocol):
    """What the tools need from a data source (real API or fixtures)."""

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse: ...

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse: ...


class FixtureDataSource:
    """Serves `public_power` and `price` responses from JSON files in a folder."""

    def __init__(self, directory: Path) -> None:
        self.responses: list[TimeSeriesResponse] = [
            TimeSeriesResponse.model_validate(json.loads(path.read_text()))
            for path in sorted(directory.glob("*.json"))
        ]

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._find("public_power", start, end)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        return self._find("price", start, end)

    def _find(self, endpoint: str, start: date, end: date) -> TimeSeriesResponse:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        for response in self.responses:
            if response.endpoint != endpoint or not response.data:
                continue
            first, last = response.data[0].timestamp.date(), response.data[-1].timestamp.date()
            if first <= start and end <= last:
                rows = [r for r in response.data if start <= r.timestamp.date() <= end]
                return response.model_copy(update={"data": rows})
        raise NoDataError(f"No recorded {endpoint} data covering {start} to {end}.")
