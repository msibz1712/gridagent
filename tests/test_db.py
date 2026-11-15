"""Database layer against a real PostgreSQL (local Docker in development, a service
container in CI). Skipped unless TEST_DATABASE_URL is set; never touches Supabase.

    docker run -d --name gridagent-testdb -e POSTGRES_PASSWORD=test \\
      -e POSTGRES_DB=gridagent_test -p 127.0.0.1:5433:5432 postgres:17
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/gridagent_test uv run pytest
"""

import json
import os
from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from gridagent.data.fixture_source import FixtureDataSource  # noqa: E402
from gridagent.db.source import DatabaseDataSource, HybridDataSource, NotInDatabase  # noqa: E402
from gridagent.db.store import connect, covered_ranges, init_schema, read_response, status  # noqa: E402
from gridagent.db.sync import run_sync, year_chunks  # noqa: E402
from gridagent.db.tables import TABLES  # noqa: E402
from gridagent.tools.toolbox import ToolBox  # noqa: E402

from conftest import FIXTURE_DIR, load_response  # noqa: E402

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

WEEK = (date(2026, 11, 2), date(2026, 11, 8))
OCTOBER = (date(2026, 10, 1), date(2026, 10, 31))
REGIONAL_SERIES = ("solar", "wind_onshore", "load")  # the SMARD series we have fixtures for
fixtures = FixtureDataSource(FIXTURE_DIR)


@pytest.fixture(scope="module")
def synced_url() -> str:
    """A fresh schema with the recorded week, October and one 2022 day synced into it."""
    with connect(URL) as conn:
        conn.execute("drop schema if exists energy cascade")
        conn.commit()
        init_schema(conn)
        init_schema(conn)  # idempotent: running it twice must not fail
        run_sync(conn, fixtures, *WEEK, regional_series=REGIONAL_SERIES)
        run_sync(conn, fixtures, *OCTOBER, tables=["public_power", "day_ahead_price"])
        run_sync(conn, fixtures, date(2022, 6, 1), date(2022, 6, 1), tables=["public_power"])
    return URL


def values_equal(a, b) -> bool:
    return a is None and b is None or (a is not None and b is not None and abs(a - b) < 1e-6)


@pytest.mark.parametrize(
    ("table", "fixture"),
    [
        ("public_power", "public_power_de_2026-11-02_2026-11-08.json"),
        ("total_power", "total_power_de_2026-11-02_2026-11-08.json"),
        ("day_ahead_price", "price_DE-LU_2026-11-02_2026-11-08.json"),
        ("cross_border_physical", "cbpf_de_2026-11-02_2026-11-08.json"),
        ("cross_border_trading", "cbet_de_2026-11-02_2026-11-08.json"),
    ],
)
def test_round_trip_is_exact(synced_url: str, table: str, fixture: str) -> None:
    """What the API delivered comes back out of the database value for value."""
    original = load_response(fixture)
    with connect(synced_url) as conn:
        stored = read_response(conn, TABLES[table], *WEEK)
    assert stored.endpoint == original.endpoint and stored.unit == original.unit
    assert set(stored.series_ids()) == set(original.series_ids())
    assert len(stored.data) == len(original.data) == 672
    for s_row, o_row in zip(stored.data, original.data, strict=True):
        assert s_row.timestamp == o_row.timestamp
        assert all(values_equal(s_row.values[k], o_row.values[k]) for k in o_row.values)


def test_regional_round_trip(synced_url: str) -> None:
    original = fixtures.regional("TenneT", list(REGIONAL_SERIES), *WEEK)
    with connect(synced_url) as conn:
        stored = read_response(conn, TABLES["smard_regional"], *WEEK, region="TenneT")
    assert stored.attributes["region"] == "TenneT" and len(stored.data) == 672
    for s_row, o_row in zip(stored.data, original.data, strict=True):
        assert all(values_equal(s_row.values[k], o_row.values[k]) for k in REGIONAL_SERIES)


def test_dst_day_keeps_100_quarter_hours(synced_url: str) -> None:
    with connect(synced_url) as conn:
        day = read_response(conn, TABLES["public_power"], date(2026, 10, 25), date(2026, 10, 25))
    assert len(day.data) == 100


def test_series_absent_in_a_period_are_not_reported(synced_url: str) -> None:
    with connect(synced_url) as conn:
        day_2022 = read_response(conn, TABLES["public_power"], date(2022, 6, 1), date(2022, 6, 1))
    assert "nuclear" in day_2022.series_ids()
    assert "fossil_coal_derived_gas" not in day_2022.series_ids()


def test_sync_twice_creates_no_duplicates(synced_url: str) -> None:
    with connect(synced_url) as conn:
        before = conn.execute("select count(*) from energy.public_power").fetchone()[0]
        run_sync(conn, fixtures, *WEEK, tables=["public_power"])
        after = conn.execute("select count(*) from energy.public_power").fetchone()[0]
    assert before == after


def test_coverage_merges_touching_ranges(synced_url: str) -> None:
    with connect(synced_url) as conn:
        ranges = covered_ranges(conn, TABLES["public_power"])
    assert (date(2026, 10, 1), date(2026, 10, 31)) in ranges
    assert (date(2022, 6, 1), date(2022, 6, 1)) in ranges


def test_database_source_serves_only_covered_periods(synced_url: str) -> None:
    db = DatabaseDataSource(synced_url)
    assert db.public_power(*WEEK).attributes["served_from"] == "database"
    with pytest.raises(NotInDatabase):
        db.public_power(date(2026, 11, 9), date(2026, 11, 10))  # never synced
    with pytest.raises(NotInDatabase):
        db.cross_border(*OCTOBER)  # flows were only synced for the week


def test_hybrid_falls_back_to_live(synced_url: str) -> None:
    hybrid = HybridDataSource(DatabaseDataSource(synced_url), live=fixtures)
    hybrid.day_ahead_price(*WEEK)
    assert hybrid.last_served_from == "database"
    hybrid.day_ahead_price(date(2026, 11, 13), date(2026, 11, 13))  # not in the database
    assert hybrid.last_served_from == "live"
    unreachable = HybridDataSource(
        DatabaseDataSource("postgresql://nobody@127.0.0.1:1/x"), fixtures
    )
    unreachable.day_ahead_price(*WEEK)
    assert unreachable.last_served_from == "live"  # database down -> the agent keeps working


def test_agent_tools_give_identical_results_from_the_database(synced_url: str) -> None:
    """The headline promise: same question, same numbers, whichever source serves it."""
    args = {"start": "2026-11-02", "end": "2026-11-08", "sources": None, "scope": None}
    stats = {"operation": "share", "source": "wind", "relative_to": None, "per_day": False,
             "threshold": None, "weight_ref": None, "weight_source": None}  # fmt: skip
    results = []
    for provider in (fixtures, HybridDataSource(DatabaseDataSource(synced_url), fixtures)):
        box = ToolBox(provider, today=lambda: date(2026, 11, 14))
        ref = json.loads(box.execute("get_generation", args).content)["ref"]
        results.append(json.loads(box.execute("compute_stats", {"ref": ref, **stats}).content))
    assert results[0]["value"] == results[1]["value"] == 23.3


def test_status_reports_rows_and_coverage(synced_url: str) -> None:
    with connect(synced_url) as conn:
        report = {row["table"]: row for row in status(conn)}
    assert report["public_power"]["rows"] == 672 + 2980 + 96
    assert report["smard_regional"]["rows"] == 5 * 672
    assert set(report["smard_regional"]["coverage"]) == {"DE", "50Hertz", "Amprion", "TenneT",
                                                         "TransnetBW"}  # fmt: skip


def test_year_chunks() -> None:
    assert year_chunks(date(2024, 11, 1), date(2026, 2, 3)) == [
        (date(2024, 11, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 2, 3)),
    ]


def test_build_data_puts_the_database_in_front_of_live(tmp_path) -> None:
    from gridagent import services

    data = services.build_data(False, FIXTURE_DIR, tmp_path, database_url=URL)
    assert isinstance(data, HybridDataSource)


def test_reader_url_prefers_the_read_only_role(monkeypatch) -> None:
    from gridagent import services

    monkeypatch.setenv("DATABASE_URL", "postgresql://owner@h/db")
    monkeypatch.delenv("DATABASE_URL_READER", raising=False)
    assert services.reader_database_url() == "postgresql://owner@h/db"
    monkeypatch.setenv("DATABASE_URL_READER", "postgresql://reader@h/db")
    assert services.reader_database_url() == "postgresql://reader@h/db"


def test_cli_db_commands(synced_url: str, monkeypatch) -> None:
    from typer.testing import CliRunner

    from gridagent.cli import app

    monkeypatch.setenv("DATABASE_URL", synced_url)
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0 and "public_power" in result.output
    bad = runner.invoke(app, ["db", "sync", "--since", "2026-01-01", "--table", "nope"])
    assert bad.exit_code == 1 and "Unknown table" in bad.output


def test_reader_role_can_read_but_not_write(synced_url: str, tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from gridagent.cli import app

    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=keep-me\nDATABASE_URL_READER=old\n")
    monkeypatch.setenv("DATABASE_URL", synced_url)
    result = CliRunner().invoke(app, ["db", "create-reader", "--env-file", str(env_file)])
    assert result.exit_code == 0
    lines = env_file.read_text().splitlines()
    assert lines[0] == "ANTHROPIC_API_KEY=keep-me" and len(lines) == 2
    url = lines[1].removeprefix("DATABASE_URL_READER=")
    assert url.split("@")[0].split(":")[-1] not in result.output  # password never printed

    assert DatabaseDataSource(url).public_power(*WEEK).data  # reading works
    with connect(url) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("delete from energy.public_power")


def test_reader_url_keeps_supabase_project_ref() -> None:
    from gridagent.db.store import reader_url

    owner = "postgresql://postgres.abcref:secret@pooler.example.com:5432/postgres"
    assert reader_url(owner, "p/w") == (
        "postgresql://gridagent_reader.abcref:p%2Fw@pooler.example.com:5432/postgres"
    )


def _price_response(timestamps):
    from gridagent.data.models import DataRow, SeriesInfo, TimeSeriesResponse

    return TimeSeriesResponse(
        endpoint="price", timezone="Europe/Berlin", unit="EUR / MWh",
        series=[SeriesInfo(id="day_ahead_price", name="price")],
        data=[DataRow(timestamp=t, values={"day_ahead_price": -1.0}) for t in timestamps],
    )  # fmt: skip


def test_hourly_prices_keep_their_interval(synced_url: str) -> None:
    """Prices were hourly until Sep 2025: counting negative-price hours must not be 4x off."""
    from datetime import UTC, datetime, timedelta

    from gridagent.db.store import BERLIN, record_sync, upsert_response
    from gridagent.tools.stats import Operation, compute_stats

    # 26 Mar 2023 is the spring DST day: 23 hours, still a fixed 60-minute step.
    # Step in UTC (absolute time), then show in German time.
    midnight = datetime(2023, 3, 26, tzinfo=BERLIN).astimezone(UTC)
    hourly = [(midnight + timedelta(hours=h)).astimezone(BERLIN) for h in range(23)]
    with connect(synced_url) as conn:
        upsert_response(conn, TABLES["day_ahead_price"], _price_response(hourly))
        record_sync(conn, TABLES["day_ahead_price"], date(2023, 3, 26), date(2023, 3, 26), 23)
        conn.commit()
    response = DatabaseDataSource(synced_url).day_ahead_price(date(2023, 3, 26), date(2023, 3, 26))
    assert response.interval_minutes == 60
    series = response.get_series("day_ahead_price")
    assert compute_stats(series, Operation.COUNT_BELOW, threshold=0).value == 23  # hours


def test_interval_is_none_when_the_step_changes() -> None:
    from datetime import datetime, timedelta

    from gridagent.db.store import BERLIN, interval_minutes

    t0 = datetime(2025, 9, 30, 22, tzinfo=BERLIN)
    mixed = [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=1, minutes=15)]
    assert interval_minutes(mixed) is None
    assert interval_minutes(mixed[1:]) == 15


def test_synced_period_without_rows_falls_back_to_live(synced_url: str) -> None:
    from gridagent.db.store import record_sync

    with connect(synced_url) as conn:  # e.g. DE-LU prices before the zone existed
        record_sync(conn, TABLES["day_ahead_price"], date(2018, 1, 1), date(2018, 9, 30), 0)
        conn.commit()
    with pytest.raises(NotInDatabase):
        DatabaseDataSource(synced_url).day_ahead_price(date(2018, 2, 1), date(2018, 2, 7))


def test_dst_day_still_reports_15_minutes(synced_url: str) -> None:
    with connect(synced_url) as conn:
        day = read_response(conn, TABLES["public_power"], date(2026, 10, 25), date(2026, 10, 25))
    assert day.interval_minutes == 15


def test_regional_sync_skips_series_a_region_does_not_have() -> None:
    from gridagent.data.client import NoDataError
    from gridagent.db.sync import regional_available

    class NoOffshore:
        def regional(self, region, sources, start, end):
            if sources == ["wind_offshore"]:
                raise NoDataError("no such file")
            return fixtures.regional("TenneT", sources, start, end)

    merged = regional_available(NoOffshore(), "Amprion", ("solar", "wind_offshore", "load"), *WEEK)
    assert merged.series_ids() == ["solar", "load"] and len(merged.data) == 672
    assert all(set(row.values) == {"solar", "load"} for row in merged.data)


def test_regional_merge_keeps_the_repeated_hour() -> None:
    from gridagent.db.sync import regional_available

    day = (date(2026, 10, 25), date(2026, 10, 25))  # clocks go back: 25 hours
    october = fixtures.public_power(*day)

    class OneSeriesPerCall:  # serves each series separately, like SMARD's files
        def regional(self, region, sources, start, end):
            keep = set(sources)
            return october.model_copy(update={
                "series": [s for s in october.series if s.id in keep],
                "data": [r.model_copy(update={"values": {k: v for k, v in r.values.items()
                                                         if k in keep}}) for r in october.data],
            })  # fmt: skip

    merged = regional_available(OneSeriesPerCall(), "TenneT", ("solar", "load"), *day)
    assert len(merged.data) == 100  # 25 hours x 4: the repeated hour is kept
    assert all(set(row.values) == {"solar", "load"} for row in merged.data)


def test_large_and_negative_values_with_3_decimals_are_exact(synced_url: str) -> None:
    """A 4-byte float would turn 65432.123 into 65432.125; fixed-point keeps it."""
    from datetime import datetime, timedelta

    from gridagent.db.store import BERLIN, upsert_response

    values = [65432.123, -499.999, 0.001, -0.5, 79999.999, None]
    start = datetime(2030, 1, 1, tzinfo=BERLIN)
    response = _price_response([start + timedelta(minutes=15 * i) for i in range(6)])
    for row, value in zip(response.data, values, strict=True):
        row.values["day_ahead_price"] = value
    with connect(synced_url) as conn:
        upsert_response(conn, TABLES["day_ahead_price"], response)
        stored = read_response(conn, TABLES["day_ahead_price"], date(2030, 1, 1), date(2030, 1, 1))
        conn.rollback()  # leave nothing behind
    assert [row.values["day_ahead_price"] for row in stored.data] == values


def test_disk_cache_serves_repeats_and_refreshes_after_a_resync(synced_url: str, tmp_path) -> None:
    db = DatabaseDataSource(synced_url, cache_dir=tmp_path)
    first = db.public_power(*WEEK)
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert db.public_power(*WEEK) == first  # second read: from disk, identical

    with connect(synced_url) as conn:  # re-syncing the week must make the copy stale
        run_sync(conn, fixtures, *WEEK, tables=["public_power"])
    db.public_power(*WEEK)
    assert len(list(tmp_path.glob("*.json"))) == 2

    with pytest.raises(NotInDatabase):  # coverage is still checked before the cache
        db.public_power(date(2026, 11, 9), date(2026, 11, 10))


def test_other_bidding_zones_never_get_germanys_prices(synced_url: str) -> None:
    """Only DE-LU is stored: a French request must go to the live source, with the zone."""
    asked = []

    class Live:
        def day_ahead_price(self, start, end, bzn="DE-LU"):
            asked.append(bzn)
            return fixtures.day_ahead_price(start, end)

    with pytest.raises(NotInDatabase):
        DatabaseDataSource(synced_url).day_ahead_price(*WEEK, bzn="FR")
    hybrid = HybridDataSource(DatabaseDataSource(synced_url), live=Live())
    hybrid.day_ahead_price(*WEEK, bzn="FR")
    assert asked == ["FR"] and hybrid.last_served_from == "live"
