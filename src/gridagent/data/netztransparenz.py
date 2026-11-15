"""Redispatch measures from the German TSOs' netztransparenz.de Web API.

Checked against the official WebAPI documentation (v1.14) and real responses:
- Login: OAuth 2.0 client credentials (NTP_CLIENT_ID / NTP_CLIENT_SECRET from the
  account's OAuth Manager). An access token lasts 1 hour; the client renews it a minute
  early, and once more if the API answers 401.
- Endpoint: GET https://ds.netztransparenz.de/api/v1/data/redispatch/{from}/{to} (UTC).
  The answer is CSV (';', German decimal commas), one row per measure: start, end,
  reason, direction, mean and maximum MW, total MWh, TSOs, plant, primary energy type.
- Limit: at most 2 requests per second per IP (repeated breaches block the IP for 2 h),
  so requests are spaced at least MIN_INTERVAL_SECONDS apart.

Two properties of the data shape how we use it:
- The stated total energy (GESAMTE_ARBEIT_MWH) is often LESS than mean MW x duration:
  a measure need not be active for its whole window. Totals therefore always use the
  stated energy. For hourly analyses each measure's energy is spread evenly over its
  window (the real profile is not published): an approximation, stated in answers.
- "Probefahrt" rows are test runs of plants, not congestion management: kept separate.

Licence: netztransparenz.de content is copyright-protected; reproduction needs the TSOs'
prior written consent unless the law permits it. GridAgent therefore uses this data for
the account holder's own analysis and keeps it out of the public repository (tests use
synthetic data in the documented format).
"""

import csv
import io
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse

TOKEN_URL = "https://identity.netztransparenz.de/users/connect/token"
DATA_URL = "https://ds.netztransparenz.de/api/v1/data/redispatch"
BERLIN = ZoneInfo("Europe/Berlin")
MIN_INTERVAL_SECONDS = 0.6  # documented limit: 2 requests per second per IP
TOKEN_MARGIN = timedelta(seconds=60)
QUERY_MARGIN = timedelta(days=2)  # measures that started before the period can reach into it
SOURCE = "netztransparenz.de (the four German TSOs)"
LICENSE = (
    "© German TSOs, netztransparenz.de; reproduction requires their prior written consent: "
    "used for the account holder's own analysis"
)
DIRECTIONS = {"Wirkleistungseinspeisung reduzieren": "reduce",
              "Wirkleistungseinspeisung erhöhen": "increase"}  # fmt: skip
ENERGY_TYPES = {"Konventionell": "conventional", "Erneuerbar": "renewable",
                "Sonstiges": "other"}  # fmt: skip


class RedispatchConfigError(NoDataError):
    """No credentials configured: redispatch data is unavailable (not an outage)."""


@dataclass(frozen=True)
class Measure:
    start: datetime  # UTC
    end: datetime  # UTC
    reason: str  # as published, e.g. "Strombedingter Redispatch"
    direction: str  # "reduce" or "increase"
    mean_mw: float | None
    max_mw: float | None
    energy_mwh: float
    instructing_tso: str
    requesting_tso: str
    plant: str
    energy_type: str  # "conventional", "renewable" or "other"

    @property
    def category(self) -> str:
        """test_run, countertrade or redispatch (everything else is congestion management)."""
        reason = self.reason.lower()
        if "probefahrt" in reason:
            return "test_run"
        if "countertrade" in reason:
            return "countertrade"
        return "redispatch"


# ---------- parsing ----------


def _number(text: str) -> float | None:
    text = text.strip()
    return float(text.replace(",", ".")) if text else None


def _utc(day: str, clock: str) -> datetime:
    return datetime.strptime(f"{day} {clock}", "%d.%m.%Y %H:%M").replace(tzinfo=UTC)


def parse_measures(text: str) -> list[Measure]:
    """The API's CSV -> measures. Rows in a time zone other than UTC are refused."""
    measures = []
    for row in csv.DictReader(io.StringIO(text.strip()), delimiter=";"):
        if row["ZEITZONE_VON"] != "UTC" or row["ZEITZONE_BIS"] != "UTC":
            raise ValueError(f"unexpected time zone in redispatch data: {row['ZEITZONE_VON']}")
        energy = _number(row["GESAMTE_ARBEIT_MWH"])
        if energy is None:
            continue
        measures.append(Measure(
            start=_utc(row["BEGINN_DATUM"], row["BEGINN_UHRZEIT"]),
            end=_utc(row["ENDE_DATUM"], row["ENDE_UHRZEIT"]),
            reason=row["GRUND_DER_MASSNAHME"].strip(),
            direction=DIRECTIONS.get(row["RICHTUNG"].strip(), row["RICHTUNG"].strip()),
            mean_mw=_number(row["MITTLERE_LEISTUNG_MW"]),
            max_mw=_number(row["MAXIMALE_LEISTUNG_MW"]),
            energy_mwh=energy,
            instructing_tso=row["ANWEISENDER_UENB"].strip(),
            requesting_tso=row["ANFORDERNDER_UENB"].strip(),
            plant=row["BETROFFENE_ANLAGE"].strip(),
            energy_type=ENERGY_TYPES.get(row["PRIMAERENERGIEART"].strip(), "other"),
        ))  # fmt: skip
    return measures


# ---------- turning measures into answers ----------


def day_bounds_utc(start: date, end: date) -> tuple[datetime, datetime]:
    """German local days start..end as a UTC interval [lower, upper)."""
    lower = datetime.combine(start, datetime.min.time(), tzinfo=BERLIN).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=BERLIN)
    return lower, upper.astimezone(UTC)


def energy_in(measure: Measure, lower: datetime, upper: datetime) -> float:
    """The part of a measure's energy that falls into [lower, upper), spread evenly."""
    duration = (measure.end - measure.start).total_seconds()
    if duration <= 0:
        return measure.energy_mwh if lower <= measure.start < upper else 0.0
    overlap = (min(measure.end, upper) - max(measure.start, lower)).total_seconds()
    return measure.energy_mwh * max(overlap, 0.0) / duration


SERIES = {
    "reduce_total": ("reduce", None), "increase_total": ("increase", None),
    "reduce_renewable": ("reduce", "renewable"), "reduce_conventional": ("reduce", "conventional"),
    "increase_conventional": ("increase", "conventional"), "increase_other": ("increase", "other"),
}  # fmt: skip
SERIES_NAMES = list(SERIES)


def summarize(measures: list[Measure], start: date, end: date) -> dict:
    """Totals for the period (MWh, energy inside the period only), computed in code."""
    lower, upper = day_bounds_utc(start, end)
    relevant = [m for m in measures if m.start < upper and m.end > lower]
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    by_tso: dict[str, float] = defaultdict(float)
    for m in relevant:
        part = energy_in(m, lower, upper)
        counts[m.category] += 1
        key = f"{m.category}_{m.direction}"
        totals[key] += part
        if m.category != "test_run":
            totals[f"{m.direction}_{m.energy_type}"] += part
            if m.direction == "reduce":
                by_tso[m.instructing_tso] += part
    return {
        "measures": dict(counts),
        "energy_mwh": {k: round(v, 1) for k, v in sorted(totals.items())},
        "reductions_by_instructing_tso_mwh": {k: round(v, 1) for k, v in sorted(by_tso.items())},
        "note": "Totals use each measure's stated energy within the period. 'redispatch_*' and "
        "'countertrade_*' are congestion management; 'test_run_*' are plant test runs.",
    }


def to_series(measures: list[Measure], start: date, end: date) -> TimeSeriesResponse:
    """15-minute average power (MW) of congestion-management measures, test runs excluded.

    Each measure's stated energy is spread evenly over its window, so the energy of each
    series equals the stated totals; the shape within a measure is an approximation.
    """
    lower, upper = day_bounds_utc(start, end)
    step = timedelta(minutes=15)
    slots = []
    cursor = lower
    while cursor < upper:
        slots.append(cursor)
        cursor += step
    index = {slot: i for i, slot in enumerate(slots)}
    values = {name: [0.0] * len(slots) for name in SERIES}
    for m in measures:
        if m.category == "test_run" or m.end <= lower or m.start >= upper:
            continue
        first = max(m.start, lower)
        slot = first - timedelta(minutes=first.minute % 15, seconds=first.second)
        while slot < min(m.end, upper):
            mw = energy_in(m, slot, slot + step) * 4  # MWh in 15 min -> average MW
            for name, (direction, kind) in SERIES.items():
                if m.direction == direction and kind in (None, m.energy_type):
                    values[name][index[slot]] += mw
            slot += step
    data = [
        DataRow(timestamp=slot.astimezone(BERLIN),
                values={name: round(values[name][i], 3) for name in SERIES})
        for i, slot in enumerate(slots)
    ]  # fmt: skip
    return TimeSeriesResponse(
        endpoint="redispatch",
        timezone="Europe/Berlin",
        unit="MW",
        interval_minutes=15,
        license=LICENSE,
        country="de",
        attributes={
            "source": SOURCE,
            "method": "each measure's stated energy spread evenly over its window; "
            "test runs excluded",
        },
        series=[SeriesInfo(id=name, name=name.replace("_", " ")) for name in SERIES],
        data=data,
    )


# ---------- download ----------


class NetztransparenzClient:
    """Token handling, polite request spacing, retries and a disk cache."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        cache: ResponseCache | None = None,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] | None = None,
        max_retries: int = 3,
    ) -> None:
        if not client_id or not client_secret:
            raise RedispatchConfigError(
                "Redispatch data needs netztransparenz.de API credentials "
                "(NTP_CLIENT_ID and NTP_CLIENT_SECRET)."
            )
        self._credentials = {"grant_type": "client_credentials",
                             "client_id": client_id, "client_secret": client_secret}  # fmt: skip
        self.cache = cache
        self.http = http or httpx.Client(timeout=60.0)
        self._clock, self._sleep = clock, sleep
        self._today = today or (lambda: datetime.now(BERLIN).date())
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expires = 0.0
        self._last_request = -1e9

    def _wait_turn(self) -> None:
        wait = MIN_INTERVAL_SECONDS - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _access_token(self, force: bool = False) -> str:
        if force or self._token is None or self._clock() >= self._token_expires:
            self._wait_turn()
            response = self.http.post(TOKEN_URL, data=self._credentials)
            if response.status_code != 200:
                raise EnergyChartsError(
                    f"netztransparenz.de login failed (HTTP {response.status_code}); "
                    "check NTP_CLIENT_ID and NTP_CLIENT_SECRET"
                )  # the response body is never shown: it could echo credentials
            body = response.json()
            self._token = body["access_token"]
            lifetime = float(body.get("expires_in", 3600))
            self._token_expires = self._clock() + lifetime - TOKEN_MARGIN.total_seconds()
        return self._token

    def _get(self, url: str) -> str:
        problem, renewed = "", False
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            try:
                response = self.http.get(url, headers=headers)
            except httpx.TransportError as exc:
                problem = f"network error: {exc!r}"
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code == 401 and not renewed:  # token expired early
                    self._access_token(force=True)
                    renewed = True
                    continue
                if response.status_code in (401, 403):
                    raise EnergyChartsError(
                        f"netztransparenz.de refused access (HTTP {response.status_code})"
                    )
                if response.status_code == 404:
                    raise NoDataError("netztransparenz.de has no redispatch data for this period")
                problem = f"HTTP {response.status_code}"
                if response.status_code < 500 and response.status_code != 429:
                    break
            if attempt < self.max_retries:
                self._sleep(2.0 * 2**attempt)
        raise EnergyChartsError(f"netztransparenz.de request failed: {problem}")

    def measures(self, start: date, end: date) -> list[Measure]:
        """All measures overlapping the German local days start..end."""
        if end >= self._today():
            raise NoDataError("Redispatch measures are available up to yesterday.")
        lower, upper = day_bounds_utc(start, end)
        query_from = lower - QUERY_MARGIN
        url = f"{DATA_URL}/{query_from:%Y-%m-%dT%H:%M:%S}/{upper:%Y-%m-%dT%H:%M:%S}"
        key = ResponseCache.make_key("ntp/redispatch", {"from": str(query_from), "to": str(upper)})
        # Recent measures can still be corrected: cache them for a day, older ones for good.
        max_age = None if end < self._today() - timedelta(days=30) else timedelta(days=1)
        text = None
        if self.cache is not None:
            cached = self.cache.get(key, max_age)
            text = cached["csv"] if cached else None
        if text is None:
            text = self._get(url)
            if self.cache is not None:
                self.cache.set(key, {"csv": text}, request=url)
        return [m for m in parse_measures(text) if m.start < upper and m.end > lower]
