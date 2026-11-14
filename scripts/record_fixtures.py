"""Record real Energy-Charts responses as offline test fixtures.

Run once (needs internet, no API key):  uv run python scripts/record_fixtures.py

Fixtures are raw, unmodified response bodies so tests exercise the real
format. The public API allows ~2 requests/minute per endpoint, so this
script waits between calls and the client honours Retry-After on HTTP 429.
"""

import json
import time
from datetime import date
from pathlib import Path

from gridagent.data.client import EnergyChartsClient

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
PAUSE_SECONDS = 35  # stay under ~2 requests/minute per endpoint

# (file name, endpoint, params, end date)
FIXTURES = [
    (
        "public_power_de_2026-11-02_2026-11-08.json",
        "/v2/public_power",
        {"country": "de", "start": "2026-11-02", "end": "2026-11-08"},
        date(2026, 11, 8),
    ),
    (
        "public_power_de_2026-10-01_2026-10-31.json",
        "/v2/public_power",
        {"country": "de", "start": "2026-10-01", "end": "2026-10-31"},
        date(2026, 10, 31),
    ),
    (
        "price_DE-LU_2026-11-13.json",
        "/v2/price",
        {"bzn": "DE-LU", "start": "2026-11-13", "end": "2026-11-13"},
        date(2026, 11, 13),
    ),
    (
        "price_DE-LU_2026-11-02_2026-11-08.json",
        "/v2/price",
        {"bzn": "DE-LU", "start": "2026-11-02", "end": "2026-11-08"},
        date(2026, 11, 8),
    ),
    (
        "price_DE-LU_2026-10-01_2026-10-31.json",
        "/v2/price",
        {"bzn": "DE-LU", "start": "2026-10-01", "end": "2026-10-31"},
        date(2026, 10, 31),
    ),
]


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    client = EnergyChartsClient(cache=None, backoff_seconds=10)
    for i, (name, endpoint, params, end) in enumerate(FIXTURES):
        path = FIXTURE_DIR / name
        if path.exists():
            print(f"skip   {name} (exists)")
            continue
        if i > 0:
            time.sleep(PAUSE_SECONDS)
        body = client.get_json(endpoint, params, end)
        path.write_text(json.dumps(body) + "\n")
        print(f"saved  {name}  ({len(body['data'])} rows)")


if __name__ == "__main__":
    main()
