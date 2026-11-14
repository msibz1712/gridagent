"""ResponseCache: hits, misses and expiry, with a controllable clock."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gridagent.data.cache import ResponseCache


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_miss_then_hit(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    assert cache.get("k", max_age=None) is None
    cache.set("k", {"a": 1}, request="/x")
    assert cache.get("k", max_age=None) == {"a": 1}


def test_entry_expires_after_max_age(tmp_path: Path) -> None:
    clock = FakeClock()
    cache = ResponseCache(tmp_path, clock=clock)
    cache.set("k", {"a": 1}, request="/x")
    clock.now += timedelta(minutes=10)
    assert cache.get("k", max_age=timedelta(minutes=15)) == {"a": 1}
    clock.now += timedelta(minutes=10)
    assert cache.get("k", max_age=timedelta(minutes=15)) is None
    assert cache.get("k", max_age=None) == {"a": 1}  # past periods never expire


def test_key_ignores_param_order() -> None:
    a = ResponseCache.make_key("/v2/price", {"start": "1", "end": "2"})
    b = ResponseCache.make_key("/v2/price", {"end": "2", "start": "1"})
    assert a == b
    assert a != ResponseCache.make_key("/v2/price", {"start": "1", "end": "3"})


def test_corrupt_file_is_a_miss(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    (tmp_path / "k.json").write_text("{not json")
    assert cache.get("k", max_age=None) is None
