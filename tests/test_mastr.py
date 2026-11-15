"""Marktstammdatenregister: range-request ZIP reading, parsing, aggregation and queries.

The unit tests build a tiny export in memory (UTF-16 XML, like the real one) and serve
it through a fake server that honours HTTP range requests. The last tests use the
committed real excerpt (tests/fixtures/mastr, 1 Oct 2026, dl-de/by-2-0).
"""

import io
import json
import zipfile
from datetime import date

import httpx
import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.mastr import (
    WITHOUT_MUNICIPALITY,
    MastrError,
    RemoteFile,
    export_date,
    load_register,
    open_remote_zip,
    sync_register,
)
from gridagent.tools.register import PlantRegister, RegisterQueryError, panel, query
from gridagent.tools.toolbox import ToolBox

from conftest import FIXTURE_DIR

URL = "https://download.marktstammdatenregister.de/Gesamtdatenexport_20261115_26.2.zip"
CATALOGUE = {
    "84": "Deutschland", "1": "Österreich", "1409": "Nordrhein-Westfalen", "1403": "Bayern",
    "35": "In Betrieb", "37": "Vorübergehend stillgelegt", "38": "Endgültig stillgelegt",
    "31": "In Planung", "888": "Windenergie an Land", "889": "Windenergie auf See",
    "2408": "Braunkohle", "2410": "Erdgas",
}  # fmt: skip


def xml(root: str, tag: str, items: list[dict[str, str]]) -> bytes:
    body = "".join(
        f"<{tag}>" + "".join(f"<{k}>{v}</{k}>" for k, v in item.items()) + f"</{tag}>"
        for item in items
    )
    return f"<?xml version='1.0' encoding='UTF-16'?><{root}>{body}</{root}>".encode("utf-16")


def wind(**fields: str) -> dict[str, str]:
    unit = {"Land": "84", "Bundesland": "1409", "Landkreis": "Düren", "Gemeinde": "Düren",
            "Gemeindeschluessel": "05358008", "EinheitBetriebsstatus": "35",
            "Inbetriebnahmedatum": "2024-05-01", "Nettonennleistung": "3000.000",
            "WindAnLandOderAufSee": "888"}  # fmt: skip
    return unit | fields


UNITS = [
    wind(),  # 3 MW, 2024
    wind(Inbetriebnahmedatum="2025-03-01", Nettonennleistung="5000", Buergerenergie="1"),
    wind(
        Gemeinde="Kreuzau",
        Gemeindeschluessel="05358028",
        Inbetriebnahmedatum="2010-01-01",
        EinheitBetriebsstatus="38",
        DatumEndgueltigeStilllegung="2025-06-30",
        Nettonennleistung="1500",
    ),  # fmt: skip
    wind(Inbetriebnahmedatum="2019-01-01", EinheitBetriebsstatus="37"),  # temporarily closed
    wind(EinheitBetriebsstatus="31", Inbetriebnahmedatum="", Nettonennleistung="6000"),  # planned
    wind(Land="1"),  # outside Germany
    wind(
        Bundesland="1403",
        Landkreis="Würzburg",
        Gemeinde="Würzburg",
        Gemeindeschluessel="09663000",
        Nettonennleistung="4000",
    ),  # fmt: skip
]
COMBUSTION = [  # a large plant: district published, municipality not (critical infrastructure)
    {"Land": "84", "Bundesland": "1409", "Landkreis": "Rhein-Erft-Kreis",
     "EinheitBetriebsstatus": "35", "Inbetriebnahmedatum": "2002-09-01",
     "Nettonennleistung": "944000", "Energietraeger": "2408"},
]  # fmt: skip


def export_zip() -> bytes:
    catalogue = [{"Id": k, "Wert": v, "KatalogKategorieId": "0"} for k, v in CATALOGUE.items()]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Katalogwerte.xml", xml("Katalogwerte", "Katalogwert", catalogue))
        archive.writestr("EinheitenWind.xml", xml("EinheitenWind", "EinheitWind", UNITS))
        archive.writestr(
            "EinheitenVerbrennung.xml",
            xml("EinheitenVerbrennung", "EinheitVerbrennung", COMBUSTION),
        )
        archive.writestr("Marktakteure_1.xml", b"x" * 10_000)  # never downloaded
    return buffer.getvalue()


def range_server(blob: bytes, fail_first: int = 0):
    """Serves byte ranges of `blob` (206), dropping the first `fail_first` connections."""
    seen = {"requests": 0, "bytes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["requests"] += 1
        if seen["requests"] <= fail_first:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        chunk = blob[start : end + 1]
        seen["bytes"] += len(chunk)
        headers = {"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(blob)}"}
        return httpx.Response(206, content=chunk, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


@pytest.fixture
def register(tmp_path) -> PlantRegister:
    http, _ = range_server(export_zip())
    data = sync_register(tmp_path / "r.json.gz", url=URL, http=http, skip_heavy=True)
    return PlantRegister(data)


# ---------- reading the remote ZIP ----------


def test_only_the_needed_files_are_downloaded() -> None:
    blob = export_zip()
    http, seen = range_server(blob)
    archive = open_remote_zip(URL, http)
    assert "Marktakteure_1.xml" in archive.namelist()
    with archive.open("EinheitenWind.xml") as fh:
        assert fh.read(2) == b"\xff\xfe"  # UTF-16 byte order mark
    assert seen["requests"] >= 2


def test_dropped_connections_are_retried_then_reported() -> None:
    http, _ = range_server(export_zip(), fail_first=2)
    assert RemoteFile(URL, http, sleep=lambda s: None).size > 0
    http, _ = range_server(export_zip(), fail_first=99)
    with pytest.raises(MastrError, match="after 3 attempts"):
        RemoteFile(URL, http, retries=3, sleep=lambda s: None)


def test_export_date_from_the_file_name() -> None:
    assert export_date(URL) == "2026-11-15"
    stichtag = (
        "https://download.marktstammdatenregister.de/Stichtag/Gesamtdatenexport_20261001_26.1.zip"
    )
    assert export_date(stichtag) == "2026-10-01"


# ---------- aggregation ----------


def test_sync_writes_the_aggregate_and_counts_what_it_skips(tmp_path, register) -> None:
    data = load_register(tmp_path / "r.json.gz")
    assert data["export_date"] == "2026-11-15" and data["complete"] is False
    assert "dl-de/by-2-0" in data["license"]
    assert data["skipped"] == {"outside Germany": 1}


def test_stock_additions_removals_and_planned(register) -> None:
    nrw = query(register, "wind_onshore", state="Nordrhein-Westfalen", from_year=2024)
    # installed: 3 + 5 + 3 (temporarily closed still counts); Kreuzau's 1.5 MW closed in 2025
    assert (nrw["installed_units"], nrw["installed_mw"]) == (3, 11.0)
    assert (nrw["added_mw"], nrw["removed_mw"], nrw["net_change_mw"]) == (8.0, 1.5, 6.5)
    assert (nrw["planned_units"], nrw["planned_mw"]) == (1, 6.0)
    assert nrw["added_as_community_energy_mw"] == 5.0
    before = query(register, "wind_onshore", state="Nordrhein-Westfalen", to_year=2023)
    assert before["installed_mw"] == 4.5  # 2010 + 2019 units


def test_by_year_is_a_running_total(register) -> None:
    years = query(register, "wind", state="Nordrhein-Westfalen", from_year=2023,
                  group_by="year")["groups"]  # fmt: skip
    assert [(y["name"], y["added_mw"], y["removed_mw"], y["installed_mw"]) for y in years] == [
        ("2023", 0.0, 0.0, 4.5), ("2024", 3.0, 0.0, 7.5), ("2025", 5.0, 1.5, 11.0),
        ("2026", 0.0, 0.0, 11.0)]  # fmt: skip


def test_large_plants_are_placed_in_their_district(register) -> None:
    lignite = query(register, "lignite", district="Rhein-Erft-Kreis", group_by="municipality")
    assert lignite["installed_mw"] == 944.0
    assert lignite["groups"][0]["name"].startswith(WITHOUT_MUNICIPALITY)


def test_places_are_matched_exactly_or_uniquely(register) -> None:
    assert query(register, "wind", municipality="kreuzau")["installed_mw"] == 0.0  # closed
    assert query(register, "wind", district="Würz")["installed_mw"] == 4.0  # unique part
    with pytest.raises(RegisterQueryError, match="No district"):
        query(register, "wind", district="Paris")
    with pytest.raises(RegisterQueryError, match="Unknown state"):
        query(register, "wind", state="NRW")


def test_a_light_register_refuses_solar_instead_of_reporting_zero(register) -> None:
    with pytest.raises(RegisterQueryError, match="no data for .*solar_rooftop"):
        query(register, "renewable")


def test_no_years_after_the_register_date(register) -> None:
    with pytest.raises(RegisterQueryError, match="2026-11-15"):
        query(register, "wind", to_year=2027)


def test_research_panel_is_balanced(register) -> None:
    rows = panel(register, "Nordrhein-Westfalen", "wind_onshore", first_year=2020)
    dueren = [r for r in rows if r["municipality"] == "Düren"]
    assert [r["year"] for r in dueren] == list(range(2020, 2027))  # also years without change
    assert dueren[-1]["installed_mw"] == 11.0 - 0.0 - 0  # 3 + 5 + 3 (Kreuzau is separate)
    kreuzau = next(r for r in rows if r["municipality"] == "Kreuzau" and r["year"] == 2025)
    assert (kreuzau["removed_mw"], kreuzau["installed_mw"]) == (1.5, 0.0)


# ---------- the real excerpt (1 Oct 2026) and the tool ----------


def tool(**kwargs) -> dict:
    args = {"area_type": "germany", "area": "Germany", "from_year": 2026, "to_year": 2026,
            "group_by": "none"} | kwargs  # fmt: skip
    box = ToolBox(FixtureDataSource(FIXTURE_DIR), today=lambda: date(2026, 11, 14))
    result = box.execute("get_plant_register", args)
    assert not result.is_error, result.content
    return json.loads(result.content)


def test_register_totals_agree_with_energy_charts() -> None:
    """Energy-Charts builds its capacity figures on the register: within 2 %.

    Not closer, because units are often registered months after they start: the
    1 Oct snapshot lacks late registrations that Energy-Charts' series (rebuilt from
    the latest register) already has, e.g. offshore 11.43 vs 11.60 GW.
    """
    monthly = json.loads((FIXTURE_DIR / "installed_power_de_monthly.json").read_text())
    row = next(r for r in monthly["data"] if r["timestamp"].startswith("2026-09"))
    for ours, theirs in (("wind_onshore", "wind_onshore"), ("wind_offshore", "wind_offshore"),
                         ("biomass", "biomass")):  # fmt: skip
        gw = tool(technology=ours, to_year=2026)["installed_mw"] / 1000
        assert gw == pytest.approx(row["values"][theirs], rel=0.02)


def test_nrw_lignite_closures_show_in_the_register() -> None:
    years = {y["name"]: y for y in tool(technology="lignite", area_type="state",
                                        area="Nordrhein-Westfalen", from_year=2020,
                                        group_by="year")["groups"]}  # fmt: skip
    assert years["2021"]["removed_mw"] > 1000 and years["2024"]["removed_mw"] > 2000


def test_tool_result_names_source_and_licence() -> None:
    result = tool(technology="wind", area_type="state", area="Nordrhein-Westfalen")
    assert result["register_date"] == "2026-10-01"
    assert (
        "dl-de/by-2-0" in result["license"] and "Marktstammdatenregister" in result["data_source"]
    )


def test_the_register_counts_as_a_cited_source() -> None:
    from gridagent.evals.scoring import SOURCE_PHRASES, contains_any

    assert contains_any("Source: Marktstammdatenregister (Bundesnetzagentur)", SOURCE_PHRASES)


def test_groups_carry_their_share_of_the_area(register) -> None:
    """A live run computed '42 % of the state's lignite' mentally: the tool now gives it."""
    groups = query(register, "wind", state="Nordrhein-Westfalen", group_by="municipality")["groups"]
    assert sum(g["share_of_installed_pct"] for g in groups) == pytest.approx(100)


def test_hybrid_falls_back_to_the_local_copy_without_a_database_register() -> None:
    from gridagent.db.source import HybridDataSource, NotInDatabase

    class EmptyDatabase:
        def plant_register(self):
            raise NotInDatabase("no plant register uploaded")

    hybrid = HybridDataSource(EmptyDatabase(), live=FixtureDataSource(FIXTURE_DIR))
    assert hybrid.plant_register()["export_date"] == "2026-10-01"
    assert hybrid.last_served_from == "live"
