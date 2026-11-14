"""New Energy-Charts endpoints (cross-border, total power, installed power) on real fixtures."""

from datetime import date

import httpx
import pytest

from gridagent.data.client import EnergyChartsClient
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.sources import GENERATION_SERIES, NON_GENERATION_SERIES, source_series

from conftest import FIXTURE_DIR, load_raw

WEEK = (date(2026, 11, 2), date(2026, 11, 8))
source = FixtureDataSource(FIXTURE_DIR)


def client_returning(body: dict, seen: list) -> EnergyChartsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    return EnergyChartsClient(http=httpx.Client(transport=httpx.MockTransport(handler)),
                              today=lambda: date(2026, 11, 14))  # fmt: skip


@pytest.mark.parametrize(("kind", "path"), [("physical", "/v2/cbpf"), ("trading", "/v2/cbet")])
def test_cross_border_endpoints(kind: str, path: str) -> None:
    seen: list = []
    body = load_raw(f"{path.rsplit('/', 1)[-1]}_de_2026-11-02_2026-11-08.json")
    result = client_returning(body, seen).cross_border(*WEEK, kind=kind)
    assert seen[0].url.path == path
    assert result.unit == "GW"
    assert result.attributes["sign_convention"] == "positive = import, negative = export"


def test_cross_border_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        client_returning({}, []).cross_border(*WEEK, kind="smuggling")


def test_installed_power_params() -> None:
    seen: list = []
    client_returning(load_raw("installed_power_de_yearly.json"), seen).installed_power("yearly")
    assert dict(seen[0].url.params) == {"country": "de", "time_step": "yearly"}


def test_cross_border_sum_is_sum_of_neighbours() -> None:
    flows = source.cross_border(*WEEK, kind="physical")
    for row in flows.data[:50]:
        neighbours = sum(v for k, v in row.values.items() if k != "sum")
        assert neighbours == pytest.approx(row.values["sum"], abs=0.01)


def test_installed_power_contains_2030_targets() -> None:
    yearly = source.installed_power("yearly")
    target = yearly.data[-1]
    assert target.timestamp.year == 2030
    assert target.values["solar_planned_eeg_2023"] == 215.0
    assert target.values["wind_onshore_planned_eeg_2023"] == 115.0
    assert yearly.attributes["timestamp_reference"] == "period_end"


def test_total_power_series_are_classified_and_load_maps() -> None:
    total = source.total_power(*WEEK)
    unclassified = set(total.series_ids()) - GENERATION_SERIES - NON_GENERATION_SERIES
    assert unclassified == set()
    load = source_series(total, "load")  # -> load_incl_self_consumption
    assert load.points[0].value == total.data[0].values["load_incl_self_consumption"]
