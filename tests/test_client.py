"""EnergyChartsClient against a fake HTTP server (httpx.MockTransport): no network."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from gridagent.data.cache import ResponseCache
from gridagent.data.client import EnergyChartsClient, EnergyChartsError, NoDataError

from conftest import WEEK_POWER, YESTERDAY_PRICE, load_raw

TODAY = date(2026, 11, 14)


class FakeServer:
    """Returns queued responses in order and records every request."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(
    server: FakeServer, cache: ResponseCache | None = None, sleeps: list[float] | None = None
) -> EnergyChartsClient:
    sleeps = sleeps if sleeps is not None else []
    return EnergyChartsClient(
        cache=cache,
        http=httpx.Client(transport=httpx.MockTransport(server)),
        max_retries=2,
        backoff_seconds=1.0,
        sleep=sleeps.append,  # record waits instead of sleeping
        today=lambda: TODAY,
    )


def ok(fixture: str) -> httpx.Response:
    return httpx.Response(200, json=load_raw(fixture))


def test_parses_response_and_sends_correct_params() -> None:
    server = FakeServer(ok(YESTERDAY_PRICE))
    result = make_client(server).day_ahead_price(date(2026, 11, 13), date(2026, 11, 13))
    assert result.endpoint == "price"
    request = server.requests[0]
    assert request.url.path == "/v2/price"
    assert dict(request.url.params) == {"bzn": "DE-LU", "start": "2026-11-13", "end": "2026-11-13"}


def test_second_call_is_served_from_cache(tmp_path: Path) -> None:
    server = FakeServer(ok(WEEK_POWER))  # only ONE response queued
    client = make_client(server, cache=ResponseCache(tmp_path))
    first = client.public_power(date(2026, 11, 2), date(2026, 11, 8))
    second = client.public_power(date(2026, 11, 2), date(2026, 11, 8))
    assert len(server.requests) == 1
    assert first == second


def test_period_including_today_uses_short_ttl(tmp_path: Path) -> None:
    now = [datetime(2026, 11, 14, 9, 0, tzinfo=UTC)]
    cache = ResponseCache(tmp_path, clock=lambda: now[0])
    server = FakeServer(ok(YESTERDAY_PRICE), ok(YESTERDAY_PRICE))
    client = make_client(server, cache=cache)

    client.day_ahead_price(TODAY, TODAY)  # fetched and cached at 09:00
    now[0] += timedelta(minutes=5)
    client.day_ahead_price(TODAY, TODAY)  # 09:05 -> still fresh, from cache
    assert len(server.requests) == 1
    now[0] += timedelta(hours=1)
    client.day_ahead_price(TODAY, TODAY)  # 10:05 -> stale, fetched again
    assert len(server.requests) == 2


def test_404_raises_no_data_error_without_retry() -> None:
    server = FakeServer(httpx.Response(404, text="no content available"))
    with pytest.raises(NoDataError):
        make_client(server).public_power(date(2026, 11, 20), date(2026, 11, 21))
    assert len(server.requests) == 1


def test_429_waits_for_retry_after_then_succeeds() -> None:
    sleeps: list[float] = []
    server = FakeServer(httpx.Response(429, headers={"Retry-After": "7"}), ok(YESTERDAY_PRICE))
    make_client(server, sleeps=sleeps).day_ahead_price(date(2026, 11, 13), date(2026, 11, 13))
    assert sleeps == [7.0]


def test_server_errors_retry_with_exponential_backoff_then_fail() -> None:
    sleeps: list[float] = []
    server = FakeServer(*[httpx.Response(503) for _ in range(3)])
    with pytest.raises(EnergyChartsError, match="after 3 attempts"):
        make_client(server, sleeps=sleeps).public_power(date(2026, 11, 2), date(2026, 11, 8))
    assert sleeps == [1.0, 2.0]


def test_network_error_is_retried() -> None:
    server = FakeServer(httpx.ConnectTimeout("slow"), ok(YESTERDAY_PRICE))
    result = make_client(server).day_ahead_price(date(2026, 11, 13), date(2026, 11, 13))
    assert result.endpoint == "price"


def test_bad_request_is_not_retried() -> None:
    server = FakeServer(httpx.Response(422, json={"detail": "bad country"}))
    with pytest.raises(EnergyChartsError, match="422"):
        make_client(server).public_power(date(2026, 11, 2), date(2026, 11, 8), country="zz")
    assert len(server.requests) == 1


def test_start_after_end_is_rejected_before_any_request() -> None:
    server = FakeServer()
    with pytest.raises(ValueError):
        make_client(server).public_power(date(2026, 11, 8), date(2026, 11, 2))
    assert server.requests == []


@pytest.mark.live
def test_live_api_returns_yesterdays_prices() -> None:
    """Real network call. Run with: uv run pytest -m live"""
    client = EnergyChartsClient()
    result = client.day_ahead_price(date(2026, 11, 13), date(2026, 11, 13))
    assert len(result.data) == 96
