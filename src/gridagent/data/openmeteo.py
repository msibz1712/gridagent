"""Open-Meteo weather forecasts for Germany: wind at hub height, solar radiation, temperature.

Why Open-Meteo, and for what. For TOMORROW the grid operators' day-ahead wind and solar
forecasts (Energy-Charts) are better: measured Oct-Nov 2026, hourly correlation with
actual wind generation 0.991, against 0.965 for an Open-Meteo hub-height index. Open-Meteo
adds what Energy-Charts does not publish: forecasts up to 7 days ahead, and temperature
(heating demand). Its "previous runs" archive also gives each forecast as it was issued
1..7 days earlier, so the quality at every lead time can be measured honestly.

Three national indicators from 18 points (one per federal state + two offshore):
- wind_100m: wind speed at 100 m (hub height), weighted by installed wind capacity
  (onshore per state from the plant register; offshore split evenly North Sea / Baltic)
- solar_radiation: global horizontal irradiance, weighted by installed solar capacity
- temperature: 2 m air temperature, weighted by population (demand)
Without a plant register, the capacity weights fall back to equal weights (said in the
response's attributes).

Data: Open-Meteo.com, CC BY 4.0; the free API is for non-commercial use (10,000 calls a
day; GridAgent needs one call per period and lead time, cached).
"""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
LICENSE = "Open-Meteo.com, CC BY 4.0"
MAX_LEAD_DAYS = 7

# (name, latitude, longitude, population in millions: rounded, Destatis 31 Dec 2023)
STATES = [
    ("Baden-Württemberg", 48.66, 9.35, 11.3), ("Bayern", 48.79, 11.50, 13.4),
    ("Berlin", 52.52, 13.40, 3.8), ("Brandenburg", 52.41, 12.53, 2.6),
    ("Bremen", 53.08, 8.80, 0.7), ("Hamburg", 53.55, 10.00, 1.9),
    ("Hessen", 50.65, 9.16, 6.4), ("Mecklenburg-Vorpommern", 53.61, 12.43, 1.6),
    ("Niedersachsen", 52.64, 9.85, 8.1), ("Nordrhein-Westfalen", 51.43, 7.66, 18.1),
    ("Rheinland-Pfalz", 49.91, 7.45, 4.2), ("Saarland", 49.40, 6.95, 1.0),
    ("Sachsen", 51.10, 13.20, 4.1), ("Sachsen-Anhalt", 51.95, 11.69, 2.2),
    ("Schleswig-Holstein", 54.22, 9.70, 3.0), ("Thüringen", 50.98, 11.03, 2.1),
]  # fmt: skip
OFFSHORE = [("North Sea", 54.30, 6.30), ("Baltic Sea", 54.90, 13.60)]
VARIABLES = {"wind_100m": "wind_speed_100m", "solar_radiation": "shortwave_radiation",
             "temperature": "temperature_2m"}  # fmt: skip
UNITS = {"wind_100m": "m/s", "solar_radiation": "W/m²", "temperature": "°C"}


class OpenMeteoClient:
    """Hourly values for many points in one request, cached; retries on network errors."""

    def __init__(
        self,
        cache: ResponseCache | None = None,
        http: httpx.Client | None = None,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        sleep: Callable[[float], None] = time.sleep,
        retries: int = 3,
    ) -> None:
        self.cache, self.today, self.sleep, self.retries = cache, today, sleep, retries
        self.http = http or httpx.Client(timeout=60)

    def points(self, start: date, end: date, lead_days: int | None) -> list[dict[str, Any]]:
        """One dict per point (STATES, then OFFSHORE) with 'time' and the three variables.

        lead_days None: the latest forecast (for today and the days ahead).
        lead_days 1..7: the forecast as issued that many days before (previous runs)."""
        if lead_days is not None and not 1 <= lead_days <= MAX_LEAD_DAYS:
            raise ValueError(f"lead_days must be 1 to {MAX_LEAD_DAYS}.")
        suffix = f"_previous_day{lead_days}" if lead_days else ""
        coordinates = [(lat, lon) for _, lat, lon, _ in STATES] + [(a, b) for _, a, b in OFFSHORE]
        params = {
            "latitude": ",".join(f"{lat:.2f}" for lat, _ in coordinates),
            "longitude": ",".join(f"{lon:.2f}" for _, lon in coordinates),
            "hourly": ",".join(v + suffix for v in VARIABLES.values()),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "timezone": "UTC", "wind_speed_unit": "ms",
        }  # fmt: skip
        url = PREVIOUS_RUNS_URL if lead_days else FORECAST_URL
        key = ResponseCache.make_key(url, params)
        # Forecasts change with every run (a few hours); a past period does not.
        max_age = None if end < self.today() else timedelta(hours=3)
        if self.cache is not None:
            cached = self.cache.get(key, max_age)
            if cached is not None:
                return cached["points"]
        body = self._get(url, params)
        points = body if isinstance(body, list) else [body]
        out = [{"time": p["hourly"]["time"],
                **{name: p["hourly"][var + suffix] for name, var in VARIABLES.items()}}
               for p in points]  # fmt: skip
        if self.cache is not None:
            self.cache.set(key, {"points": out}, request=f"{url}?{httpx.QueryParams(params)}")
        return out

    def _get(self, url: str, params: dict[str, str]) -> Any:
        error = ""
        for attempt in range(self.retries):
            try:
                response = self.http.get(url, params=params)
                if response.status_code == 400:  # e.g. a date outside the archive
                    raise NoDataError(f"Open-Meteo: {response.json().get('reason', 'bad request')}")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                self.sleep(2.0 * (attempt + 1))
        raise EnergyChartsError(f"Open-Meteo unavailable after {self.retries} attempts ({error}).")


def capacity_weights(register: dict[str, Any] | None) -> tuple[list[float], list[float], str]:
    """(wind weights, solar weights, note) for STATES + OFFSHORE, from the plant register."""
    from gridagent.tools.register import PlantRegister, RegisterQueryError, query

    n = len(STATES) + len(OFFSHORE)
    if register is None:
        return [1.0] * n, [1.0] * len(STATES) + [0.0, 0.0], "equal weights (no plant register)"
    reg = PlantRegister(register)
    onshore = {g["name"]: g["installed_mw"]
               for g in query(reg, "wind_onshore", group_by="state")["groups"]}  # fmt: skip
    offshore = query(reg, "wind_offshore")["installed_mw"]
    wind = [onshore.get(name, 0.0) for name, *_ in STATES] + [offshore / 2] * 2
    try:
        solar_by = {g["name"]: g["installed_mw"]
                    for g in query(reg, "solar", group_by="state")["groups"]}  # fmt: skip
        solar = [solar_by.get(name, 0.0) for name, *_ in STATES] + [0.0, 0.0]
        note = f"capacity weights from the plant register of {reg.export_date}"
    except RegisterQueryError:  # a register without solar (light sync)
        solar = [1.0] * len(STATES) + [0.0, 0.0]
        note = f"wind weights from the plant register of {reg.export_date}; solar equal"
    return wind, solar, note


def national(points: list[dict[str, Any]], wind_w: list[float], solar_w: list[float],
             note: str, lead_days: int | None) -> TimeSeriesResponse:  # fmt: skip
    """Weighted national hourly indicators as one response (timestamps = interval start, UTC).

    Open-Meteo's radiation is the mean of the PRECEDING hour, so it moves back one hour;
    wind and temperature are values at the hour."""
    population = [p for *_, p in STATES] + [0.0, 0.0]
    weights = {"wind_100m": wind_w, "solar_radiation": solar_w, "temperature": population}
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in points[0]["time"]]
    rows = {}
    for i, t in enumerate(times):
        values: dict[str, float | None] = {}
        for name, w in weights.items():
            pairs = [(p[name][i], wi) for p, wi in zip(points, w, strict=True) if wi > 0]
            if any(v is None for v, _ in pairs) or not pairs:
                values[name] = None
            else:
                values[name] = round(sum(v * wi for v, wi in pairs) / sum(wi for _, wi in pairs), 2)
        rows[t] = values
    for t in times:  # radiation: the value at t covers t-1h..t, so it belongs to t-1h
        rows[t]["solar_radiation"] = rows.get(t + timedelta(hours=1), {}).get("solar_radiation")
    horizon = f"{lead_days} day(s) ahead" if lead_days else "latest forecast"
    return TimeSeriesResponse(
        endpoint="weather_forecast", timezone="UTC", interval_minutes=60, license=LICENSE,
        attributes={"horizon": horizon, "weights": note},
        series=[SeriesInfo(id=k, name=k.replace("_", " "), unit=UNITS[k]) for k in VARIABLES],
        data=[DataRow(timestamp=t, values=rows[t]) for t in times],
    )  # fmt: skip
