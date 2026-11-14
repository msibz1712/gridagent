"""FixtureDataSource: serves sub-ranges of recorded responses, NoDataError otherwise."""

from datetime import date

import pytest

from gridagent.data.client import NoDataError
from gridagent.data.fixture_source import FixtureDataSource

from conftest import FIXTURE_DIR

source = FixtureDataSource(FIXTURE_DIR)


def test_exact_range() -> None:
    assert len(source.public_power(date(2026, 11, 2), date(2026, 11, 8)).data) == 672


def test_sub_range_is_sliced() -> None:
    response = source.public_power(date(2026, 10, 25), date(2026, 10, 25))
    assert len(response.data) == 100  # DST day
    assert {r.timestamp.date() for r in response.data} == {date(2026, 10, 25)}


def test_uncovered_range_raises_no_data() -> None:
    with pytest.raises(NoDataError):
        source.day_ahead_price(date(2026, 11, 9), date(2026, 11, 10))
