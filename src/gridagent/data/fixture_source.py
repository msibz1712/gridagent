"""Offline data provider that answers requests from recorded fixtures.

It has the same methods as `LiveDataSource`, so the agent's tools can't tell the
difference. A time-series request is served if one fixture covers the whole date
range (rows are cut to exactly that range); anything else raises `NoDataError`,
just like the real APIs do for missing data. SMARD files are read from
`<fixtures>/smard/` under their original file names.
"""

import json
from datetime import date
from pathlib import Path

from gridagent.data.client import NoDataError
from gridagent.data.models import TimeSeriesResponse
from gridagent.data.provider import DataProvider
from gridagent.data.smard import SmardClient

__all__ = ["DataProvider", "FixtureDataSource"]


class FixtureDataSource:
    """Serves recorded Energy-Charts and SMARD responses from a folder."""

    def __init__(self, directory: Path) -> None:
        self.responses: list[TimeSeriesResponse] = [
            TimeSeriesResponse.model_validate(json.loads(path.read_text()))
            for path in sorted(directory.glob("*.json"))
        ]
        self.smard = SmardClient(get_json=self._smard_file(directory / "smard"))

    # ---------- Energy-Charts ----------

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._find("public_power", start, end)

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        return self._find("total_power", start, end)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        return self._find("price", start, end, bidding_zone=bzn)

    def cross_border(self, start: date, end: date, kind: str = "physical") -> TimeSeriesResponse:
        if kind not in ("physical", "trading"):
            raise ValueError("kind must be 'physical' or 'trading'")
        return self._find("cbpf" if kind == "physical" else "cbet", start, end)

    def installed_power(self, time_step: str = "yearly") -> TimeSeriesResponse:
        for response in self.responses:
            if (
                response.endpoint == "installed_power"
                and response.attributes.get("time_step") == time_step
            ):
                return response
        raise NoDataError(f"No recorded installed_power data for time_step={time_step}.")

    def weather(self, start: date, end: date) -> TimeSeriesResponse:
        return self._find("weather", start, end)

    # ---------- SMARD ----------

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        return self.smard.regional(region, sources, start, end)

    @staticmethod
    def _smard_file(folder: Path):
        def get_json(path: str, end: date) -> dict:
            file = folder / path.rsplit("/", 1)[-1]
            if not file.is_file():
                raise NoDataError(f"No recorded SMARD file {file.name}.")
            return json.loads(file.read_text())

        return get_json

    # ---------- helpers ----------

    def _find(
        self, endpoint: str, start: date, end: date, bidding_zone: str | None = None
    ) -> TimeSeriesResponse:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        for response in self.responses:
            if response.endpoint != endpoint or not response.data:
                continue
            if bidding_zone is not None and response.bidding_zone != bidding_zone:
                continue
            first, last = response.data[0].timestamp.date(), response.data[-1].timestamp.date()
            if first <= start and end <= last:
                rows = [r for r in response.data if start <= r.timestamp.date() <= end]
                return response.model_copy(update={"data": rows})
        zone = f" for {bidding_zone}" if bidding_zone else ""
        raise NoDataError(f"No recorded {endpoint} data{zone} covering {start} to {end}.")
