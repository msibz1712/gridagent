"""HTTP client for the public Energy-Charts API (Fraunhofer ISE).

Docs: https://api.energy-charts.info  (we use the v2 endpoints)

Facts from the docs and real responses that shape this client:
- Rate limit: ~2 requests/minute per endpoint (burst 4; /price burst 2).
  Exceeding it returns HTTP 429 with a `Retry-After` header (seconds).
  -> we cache aggressively and honour Retry-After.
- Dates in "YYYY-MM-DD" form are interpreted in local time (Europe/Berlin),
  and the end date includes the whole day.
- A period without data (e.g. the future) returns HTTP 404 with the
  plain-text body "no content available" (not JSON, despite the spec).
"""

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from gridagent.data.cache import ResponseCache
from gridagent.data.models import TimeSeriesResponse

BASE_URL = "https://api.energy-charts.info"
BERLIN = ZoneInfo("Europe/Berlin")

# Responses for periods that include today change as new data arrives.
TODAY_CACHE_MAX_AGE = timedelta(minutes=15)
# Never wait longer than this for a single Retry-After.
MAX_RETRY_AFTER_SECONDS = 120.0


class EnergyChartsError(Exception):
    """The API could not be reached or returned an unexpected error."""


class NoDataError(EnergyChartsError):
    """The API has no data for the requested period (HTTP 404)."""


def berlin_today() -> date:
    """Today's date in Germany, the reference for 'yesterday', 'last week', etc."""
    return datetime.now(BERLIN).date()


class EnergyChartsClient:
    """Fetches German generation and price data, with caching and retries."""

    def __init__(
        self,
        cache: ResponseCache | None = None,
        http: httpx.Client | None = None,
        base_url: str = BASE_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = berlin_today,
    ) -> None:
        self.cache = cache
        self.http = http or httpx.Client(timeout=timeout_seconds)
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep  # injectable so tests don't actually wait
        self._today = today

    # ---------- public methods ----------

    def public_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        """Public net electricity generation per source in MW (15-minute values)."""
        params = {"country": country, "start": start.isoformat(), "end": end.isoformat()}
        return self._fetch("/v2/public_power", params, start, end)

    def day_ahead_price(self, start: date, end: date, bzn: str = "DE-LU") -> TimeSeriesResponse:
        """Day-ahead spot price in EUR/MWh for a bidding zone (default Germany-Luxembourg)."""
        params = {"bzn": bzn, "start": start.isoformat(), "end": end.isoformat()}
        return self._fetch("/v2/price", params, start, end)

    def total_power(self, start: date, end: date, country: str = "de") -> TimeSeriesResponse:
        """Total net generation incl. industrial self-supply in MW (Germany only).

        Load is reported as `load_incl_self_consumption` here (not `load`).
        """
        params = {"country": country, "start": start.isoformat(), "end": end.isoformat()}
        return self._fetch("/v2/total_power", params, start, end)

    def cross_border(
        self, start: date, end: date, kind: str = "physical", country: str = "de"
    ) -> TimeSeriesResponse:
        """Cross-border flows in GW per neighbour plus 'sum'; positive = import.

        kind="physical": measured physical flows (/v2/cbpf)
        kind="trading":  scheduled commercial exchanges (/v2/cbet)
        """
        if kind not in ("physical", "trading"):
            raise ValueError("kind must be 'physical' or 'trading'")
        endpoint = "/v2/cbpf" if kind == "physical" else "/v2/cbet"
        params = {"country": country, "start": start.isoformat(), "end": end.isoformat()}
        return self._fetch(endpoint, params, start, end)

    def installed_power(self, time_step: str = "yearly", country: str = "de") -> TimeSeriesResponse:
        """Installed capacity per technology (GW; battery capacity in GWh).

        Unlike every other endpoint, timestamps label the reporting year/month and the
        values are the installed capacity at the END of that period. Yearly data also
        contains the legal expansion targets (e.g. `solar_planned_eeg_2023`).
        """
        if time_step not in ("yearly", "monthly"):
            raise ValueError("time_step must be 'yearly' or 'monthly'")
        params = {"country": country, "time_step": time_step}
        # Not a date range: treat as "touching today" so it is refreshed after 15 minutes.
        today = self._today()
        return self._fetch("/v2/installed_power", params, today, today)

    # ---------- internals ----------

    def _fetch(
        self, endpoint: str, params: dict[str, str], start: date, end: date
    ) -> TimeSeriesResponse:
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")
        body = self.get_json(endpoint, params, end)
        return TimeSeriesResponse.model_validate(body)

    def get_json(self, endpoint: str, params: dict[str, str], end: date) -> dict[str, Any]:
        """GET with cache lookup first; stores successful responses."""
        key = ResponseCache.make_key(endpoint, params)
        # Past periods are final enough to cache forever; anything touching today is not.
        max_age = None if end < self._today() else TODAY_CACHE_MAX_AGE
        if self.cache is not None:
            cached = self.cache.get(key, max_age)
            if cached is not None:
                return cached

        body = self._request_with_retries(endpoint, params)
        if self.cache is not None:
            self.cache.set(key, body, request=f"{endpoint}?{httpx.QueryParams(params)}")
        return body

    def _request_with_retries(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        """Call the API, retrying on rate limits, server errors and network problems."""
        url = self.base_url + endpoint
        last_problem = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.http.get(url, params=params)
            except httpx.TransportError as exc:  # timeouts, DNS, connection resets
                last_problem = f"network error: {exc!r}"
                wait = self.backoff_seconds * 2**attempt
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    raise NoDataError(
                        f"No data available from Energy-Charts for {endpoint} with {params}."
                    )
                if response.status_code == 429:
                    last_problem = "rate limited (HTTP 429)"
                    wait = self._retry_after(response) or self.backoff_seconds * 2**attempt
                elif response.status_code >= 500:
                    last_problem = f"server error (HTTP {response.status_code})"
                    wait = self.backoff_seconds * 2**attempt
                else:  # other 4xx: our request is wrong, retrying won't help
                    raise EnergyChartsError(
                        f"Energy-Charts rejected the request (HTTP {response.status_code}): "
                        f"{response.text[:300]}"
                    )

            if attempt < self.max_retries:
                self._sleep(wait)

        raise EnergyChartsError(
            f"Energy-Charts request failed after {self.max_retries + 1} attempts: {last_problem}"
        )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Seconds to wait from the Retry-After header, capped; None if absent/invalid."""
        try:
            seconds = float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            return None
        return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
