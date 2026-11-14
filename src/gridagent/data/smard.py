"""Client for SMARD (Bundesnetzagentur): electricity data per German control area.

SMARD publishes static JSON files, one per (filter, region, resolution, week):
    https://www.smard.de/app/chart_data/{filter}/{region}/{filter}_{region}_quarterhour_{ts}.json
where `ts` is the start of the week (Monday 00:00 German time) in milliseconds and
the file contains `"series": [[timestamp_ms, value], ...]`.

Facts verified against real responses (see tests/test_smard.py):
- Values are ENERGY per interval (MWh per quarter-hour), not power. We convert to
  MW (x4) so every existing calculation works unchanged; Germany x4 matches the
  Energy-Charts MW values exactly.
- The four control areas (50Hertz, Amprion, TenneT, TransnetBW) add up to DE.
- No API key; data from 2015 on; licence CC BY 4.0 (Bundesnetzagentur | SMARD.de).
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gridagent.data.client import NoDataError
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse

SMARD_BASE_URL = "https://www.smard.de/app/chart_data"
BERLIN = ZoneInfo("Europe/Berlin")
LICENSE = "CC BY 4.0, Bundesnetzagentur | SMARD.de"

REGIONS = {
    "DE": "Germany",
    "50Hertz": "50Hertz control area (north-east)",
    "Amprion": "Amprion control area (west)",
    "TenneT": "TenneT control area (north to Bavaria)",
    "TransnetBW": "TransnetBW control area (Baden-Württemberg)",
}

# SMARD filter ids (from the bundesAPI/smard-api documentation, checked by requests).
FILTERS = {
    "solar": 4068,
    "wind_onshore": 4067,
    "wind_offshore": 1225,
    "biomass": 4066,
    "hydro": 1226,
    "lignite": 1223,
    "hard_coal": 4069,
    "gas": 4071,
    "pumped_storage": 4070,
    "other_renewable": 1228,
    "other_conventional": 1227,
    "load": 410,
    "residual_load": 4359,
}

# get_json(path, period_end) -> parsed JSON; raises NoDataError for missing files.
JsonFetcher = Callable[[str, date], dict[str, Any]]


def week_start_ms(day: date) -> int:
    """Start of the SMARD week containing `day`: Monday 00:00 German time, in ms."""
    monday = day - timedelta(days=day.weekday())
    return int(datetime(monday.year, monday.month, monday.day, tzinfo=BERLIN).timestamp() * 1000)


def chunk_path(filter_id: int, region: str, week_ms: int) -> str:
    return f"/{filter_id}/{region}/{filter_id}_{region}_quarterhour_{week_ms}.json"


class SmardClient:
    """Fetches quarter-hourly SMARD data and returns it as our TimeSeriesResponse (MW)."""

    def __init__(self, get_json: JsonFetcher) -> None:
        self.get_json = get_json

    def regional(
        self, region: str, sources: list[str], start: date, end: date
    ) -> TimeSeriesResponse:
        if region not in REGIONS:
            raise ValueError(f"Unknown region '{region}'. Choose from: {', '.join(REGIONS)}.")
        unknown = [s for s in sources if s not in FILTERS]
        if unknown:
            raise ValueError(
                f"Unknown SMARD source(s) {unknown}. Choose from: {', '.join(FILTERS)}."
            )
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        weeks = sorted({week_start_ms(start + timedelta(days=i))
                        for i in range((end - start).days + 1)})  # fmt: skip
        by_source = {source: self._series(source, region, weeks, end) for source in sources}

        timestamps = sorted({ts for values in by_source.values() for ts in values})
        rows = []
        for ts in timestamps:
            moment = datetime.fromtimestamp(ts / 1000, tz=BERLIN)
            if start <= moment.date() <= end:
                rows.append(
                    DataRow(timestamp=moment, values={s: by_source[s].get(ts) for s in sources})
                )
        if not any(v is not None for row in rows for v in row.values.values()):
            raise NoDataError(f"SMARD has no data for {region} from {start} to {end}.")
        return TimeSeriesResponse(
            endpoint="smard",
            timezone="Europe/Berlin",
            unit="MW",
            interval_minutes=15,
            license=LICENSE,
            country="de",
            attributes={"region": region, "source": "SMARD.de (Bundesnetzagentur)"},
            series=[SeriesInfo(id=s, name=s.replace("_", " ")) for s in sources],
            data=rows,
        )

    def _series(
        self, source: str, region: str, weeks: list[int], end: date
    ) -> dict[int, float | None]:
        """timestamp_ms -> average power in MW (SMARD gives MWh per quarter-hour)."""
        values: dict[int, float | None] = {}
        for week in weeks:
            body = self.get_json(chunk_path(FILTERS[source], region, week), end)
            for ts, mwh in body["series"]:
                values[ts] = None if mwh is None else mwh * 4  # MWh per 15 min -> MW
        return values
