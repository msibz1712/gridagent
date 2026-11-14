"""A tiny file-based cache for API responses.

Each response is stored as one JSON file under the cache directory. The file
name is a hash of the endpoint and query parameters, so the same request
always maps to the same file.

Freshness is decided by the caller through `max_age`:
- `None`  -> the entry never expires (used for periods fully in the past)
- a timedelta -> entries older than that are ignored (used for "today")
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ResponseCache:
    """Stores and loads JSON response bodies on disk."""

    def __init__(self, directory: Path, clock: Callable[[], datetime] = _utc_now) -> None:
        self.directory = directory
        self._clock = clock  # injectable so tests can control "now"

    @staticmethod
    def make_key(endpoint: str, params: dict[str, str]) -> str:
        """Deterministic key: same endpoint + params -> same key, whatever the dict order."""
        raw = endpoint + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str, max_age: timedelta | None) -> dict[str, Any] | None:
        """Return the cached body, or None if missing, unreadable or too old."""
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text())
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return None  # a corrupt cache file is treated as a miss
        if max_age is not None and self._clock() - fetched_at > max_age:
            return None
        return entry["body"]

    def set(self, key: str, body: dict[str, Any], request: str) -> None:
        """Save a response body together with when and what was fetched."""
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {"fetched_at": self._clock().isoformat(), "request": request, "body": body}
        self._path(key).write_text(json.dumps(entry))
