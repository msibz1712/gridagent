"""Shared test helpers: load recorded Energy-Charts responses from tests/fixtures."""

import json
from pathlib import Path

import pytest

from gridagent.data.models import TimeSeriesResponse

FIXTURE_DIR = Path(__file__).parent / "fixtures"

WEEK_POWER = "public_power_de_2026-11-02_2026-11-08.json"
OCTOBER_POWER = "public_power_de_2026-10-01_2026-10-31.json"
YESTERDAY_PRICE = "price_DE-LU_2026-11-13.json"
WEEK_PRICE = "price_DE-LU_2026-11-02_2026-11-08.json"


def load_raw(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def load_response(name: str) -> TimeSeriesResponse:
    return TimeSeriesResponse.model_validate(load_raw(name))


@pytest.fixture(scope="session")
def week_power() -> TimeSeriesResponse:
    return load_response(WEEK_POWER)


@pytest.fixture(scope="session")
def october_power() -> TimeSeriesResponse:
    return load_response(OCTOBER_POWER)


@pytest.fixture(scope="session")
def yesterday_price() -> TimeSeriesResponse:
    return load_response(YESTERDAY_PRICE)
