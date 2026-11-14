"""Pydantic models for Energy-Charts v2 responses and for our own time series.

The v2 API returns one "envelope" shape for every data endpoint
(see https://api.energy-charts.info, schema `TimeSeriesResponseV2`):

    {
      "endpoint": "public_power", "unit": "MW", "interval_minutes": 15,
      "timezone": "Europe/Berlin", "license": "...",
      "series": [{"id": "solar", "name": "Solar"}, ...],
      "data": [{"timestamp": "2026-11-10T00:00:00+01:00",
                "values": {"solar": 0.0, ...}}, ...]
    }

We parse that envelope (`TimeSeriesResponse`) and then pull out single
series (`Series`), which is what the stats functions work on.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class SeriesInfo(BaseModel):
    """Describes one series inside a response (e.g. "solar")."""

    id: str
    name: str
    unit: str | None = None  # only set when it differs from the envelope unit


class DataRow(BaseModel):
    """One timestamp with the value of every series. `None` = not available."""

    timestamp: datetime  # start of the interval, with UTC offset
    values: dict[str, float | None]


class TimeSeriesResponse(BaseModel):
    """The Energy-Charts v2 response envelope (only the fields we use)."""

    # Ignore fields we don't model, so new API fields don't break parsing.
    model_config = ConfigDict(extra="ignore")

    endpoint: str
    timezone: str
    unit: str | None = None
    interval_minutes: int | None = None
    license: str | None = None
    country: str | None = None
    bidding_zone: str | None = None
    series: list[SeriesInfo]
    data: list[DataRow]

    def series_ids(self) -> list[str]:
        """Ids of all series in this response."""
        return [s.id for s in self.series]

    def get_series(self, series_id: str) -> "Series":
        """Extract one series as a `Series`. Raises KeyError if it does not exist."""
        info = next((s for s in self.series if s.id == series_id), None)
        if info is None:
            raise KeyError(
                f"Unknown series '{series_id}'. Available: {', '.join(self.series_ids())}"
            )
        return Series(
            id=series_id,
            unit=info.unit or self.unit or "",
            interval_minutes=self.interval_minutes,
            points=[
                Point(timestamp=row.timestamp, value=row.values.get(series_id)) for row in self.data
            ],
        )


class Point(BaseModel):
    """A single value at a timestamp."""

    timestamp: datetime
    value: float | None


class Series(BaseModel):
    """One named time series: what `compute_stats` operates on."""

    id: str
    unit: str
    interval_minutes: int | None
    points: list[Point]

    @property
    def start(self) -> date | None:
        """Local calendar date of the first point."""
        return self.points[0].timestamp.date() if self.points else None

    @property
    def end(self) -> date | None:
        """Local calendar date of the last point."""
        return self.points[-1].timestamp.date() if self.points else None
