"""netztransparenz.de redispatch client (no network; SYNTHETIC data only, see fixtures/ntp)."""

from datetime import UTC, date, datetime

import httpx
import pytest

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.netztransparenz import (
    MIN_INTERVAL_SECONDS,
    Measure,
    NetztransparenzClient,
    RedispatchConfigError,
    energy_in,
    parse_measures,
    summarize,
    to_series,
)

from conftest import FIXTURE_DIR

CSV = (FIXTURE_DIR / "ntp" / "redispatch_synthetic.csv").read_text()
WEEK = (date(2026, 11, 2), date(2026, 11, 8))
HEADER = CSV.splitlines()[0]


def test_parse_reads_german_decimals_utc_and_translates_codes() -> None:
    measures = parse_measures(CSV)
    assert len(measures) == 11
    wind = next(m for m in measures if m.plant == "Wind cluster N2")
    assert wind.mean_mw == 150.5 and wind.energy_mwh == 1204
    assert (wind.direction, wind.energy_type, wind.category) == (
        "reduce",
        "renewable",
        "redispatch",
    )
    assert wind.start == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    assert {m.category for m in measures} == {"redispatch", "countertrade", "test_run"}


def test_other_time_zones_are_refused() -> None:
    row = CSV.splitlines()[1].replace(";UTC;", ";CET;", 1)
    with pytest.raises(ValueError, match="time zone"):
        parse_measures(HEADER + "\n" + row)


def test_energy_inside_a_period_is_the_overlapping_share() -> None:
    measure = Measure(start=datetime(2026, 11, 1, 21, tzinfo=UTC),
                      end=datetime(2026, 11, 2, 3, tzinfo=UTC), reason="x", direction="reduce",
                      mean_mw=100, max_mw=100, energy_mwh=600, instructing_tso="T",
                      requesting_tso="T", plant="p", energy_type="renewable")  # fmt: skip
    # Berlin midnight on 2 Nov is 23:00 UTC: 4 of the 6 hours belong to 2 Nov
    lower, upper = datetime(2026, 11, 1, 23, tzinfo=UTC), datetime(2026, 11, 2, 23, tzinfo=UTC)
    assert energy_in(measure, lower, upper) == pytest.approx(400)


def test_summary_counts_categories_and_clips_to_the_period() -> None:
    summary = summarize(parse_measures(CSV), *WEEK)
    assert summary["measures"] == {"redispatch": 9, "test_run": 1, "countertrade": 1}
    energy = summary["energy_mwh"]
    assert energy["reduce_renewable"] == 1724.0  # 400 (N1, clipped) + 1204 + 120 (N3, clipped)
    assert energy["test_run_increase"] == 300.0
    assert "increase_conventional" in energy and energy["countertrade_increase"] == 300.0
    assert summary["reductions_by_instructing_tso_mwh"] == {
        "50Hertz": 360.0, "Amprion": 920.0, "TenneT DE": 1604.0}  # fmt: skip


def test_series_keep_the_stated_energy_and_exclude_test_runs() -> None:
    measures = parse_measures(CSV)
    series = to_series(measures, *WEEK)
    summary = summarize(measures, *WEEK)["energy_mwh"]
    assert len(series.data) == 672 and series.interval_minutes == 15
    energy = {name: sum(r.values[name] for r in series.data) / 4 for name in series.series_ids()}
    assert energy["reduce_renewable"] == pytest.approx(summary["reduce_renewable"])
    assert energy["increase_total"] == pytest.approx(
        summary["redispatch_increase"] + summary["countertrade_increase"]
    )  # no test run
    # Plant A: 800 MWh over 4 h, stated mean 250 MW: spread evenly = 200 MW, not 250
    quarter = next(r for r in series.data if r.timestamp == datetime(2026, 11, 2, 11, tzinfo=UTC))
    assert quarter.values["reduce_conventional"] == pytest.approx(200.0)


# ---------- the HTTP client ----------


class FakeClock:
    def __init__(self) -> None:
        self.now, self.slept = 1000.0, []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def client(handler, clock: FakeClock | None = None, **kwargs) -> NetztransparenzClient:
    clock = clock or FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return NetztransparenzClient(
        "id", "s3cret", http=http, clock=clock, sleep=clock.sleep,
        today=lambda: date(2026, 11, 15), **kwargs,
    )  # fmt: skip


def api(tokens: list[str], data_status: list[int]):
    """Fake identity service + data API; data calls answer the given statuses in order."""
    calls = {"token": 0, "data": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/connect/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": tokens[calls["token"] - 1],
                                             "expires_in": 3600})  # fmt: skip
        calls["data"].append(request.headers["Authorization"])
        status = data_status[len(calls["data"]) - 1]
        return httpx.Response(status, text=CSV if status == 200 else "")

    return handler, calls


def test_token_is_reused_and_renewed_after_a_401() -> None:
    handler, calls = api(tokens=["t1", "t2"], data_status=[200, 401, 200])
    ntp = client(handler)
    ntp.measures(*WEEK)
    ntp.measures(date(2026, 10, 1), date(2026, 10, 2))  # 401 -> new token -> retry
    assert calls["token"] == 2
    assert calls["data"] == ["Bearer t1", "Bearer t1", "Bearer t2"]


def test_token_is_renewed_shortly_before_it_expires() -> None:
    handler, calls = api(tokens=["t1", "t2"], data_status=[200, 200])
    clock = FakeClock()
    ntp = client(handler, clock)
    ntp.measures(*WEEK)
    clock.now += 3600 - 30  # inside the 60 s safety margin
    ntp.measures(date(2026, 10, 1), date(2026, 10, 2))
    assert calls["token"] == 2


def test_requests_are_spaced_to_respect_the_rate_limit() -> None:
    handler, _ = api(tokens=["t1"], data_status=[200, 200])
    clock = FakeClock()
    ntp = client(handler, clock)
    ntp.measures(*WEEK)
    ntp.measures(date(2026, 10, 1), date(2026, 10, 2))
    assert clock.slept and all(s <= MIN_INTERVAL_SECONDS for s in clock.slept)
    assert sum(clock.slept) >= 2 * MIN_INTERVAL_SECONDS - 1e-9  # token + 2 data calls


def test_missing_credentials_and_failed_login_never_leak_secrets() -> None:
    with pytest.raises(RedispatchConfigError):
        NetztransparenzClient("", "")

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client", "echo": "s3cret"})

    with pytest.raises(EnergyChartsError) as failure:
        client(refuse).measures(*WEEK)
    assert "s3cret" not in str(failure.value) and "HTTP 400" in str(failure.value)


def test_today_and_404_mean_no_data() -> None:
    handler, _ = api(tokens=["t1"], data_status=[404])
    with pytest.raises(NoDataError):
        client(handler).measures(date(2026, 11, 15), date(2026, 11, 15))  # today
    with pytest.raises(NoDataError):
        client(handler).measures(*WEEK)


def test_no_real_redispatch_data_in_the_repository() -> None:
    """Licence: only the clearly labelled synthetic file may be committed."""
    files = {p.name for p in (FIXTURE_DIR / "ntp").iterdir()}
    assert files == {"README.md", "redispatch_synthetic.csv"}
    assert "SYNTHETIC" in (FIXTURE_DIR / "ntp" / "README.md").read_text()
