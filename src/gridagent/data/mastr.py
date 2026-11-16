"""Marktstammdatenregister (MaStR): every power plant and storage unit in Germany.

The Bundesnetzagentur publishes the whole register daily as one ZIP of XML
files (about 3.3 GB, https://www.marktstammdatenregister.de/MaStR/Datendownload),
licensed under the Datenlizenz Deutschland - Namensnennung - 2.0 (dl-de/by-2-0).

We never download the whole ZIP. Its server accepts HTTP range requests, so
`RemoteZip` reads the ZIP's table of contents (at the end of the file) and then
only the XML files we need (units; ~1.7 GB of which ~1.1 GB is rooftop solar).
Each XML file is parsed as a stream (`iterparse`), so memory stays small.

What we keep is an aggregate, not the 7 million single units:

    (municipality key, technology, year) -> units and kW commissioned that year,
                                            units and kW permanently decommissioned,
                                            kW commissioned as community energy (wind)

plus the name, district and state of every municipality, and the planned units.
That answers "how much wind was added in NRW districts since 2023?" and gives
a municipality x year panel for research, at ~1/100 of the size.

Codes (state, energy carrier, status, ...) are translated with the catalogue
shipped in the same export (Katalogwerte.xml), never hardcoded numbers.
"""

import gzip
import io
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx

DOWNLOAD_PAGE = "https://www.marktstammdatenregister.de/MaStR/Datendownload"
EXPORT_PATTERN = re.compile(
    r"https://download\.marktstammdatenregister\.de/Gesamtdatenexport_(\d{8})_[\d.]+\.zip"
)
LICENSE = "Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)"
ATTRIBUTION = "Marktstammdatenregister – © Bundesnetzagentur, " + LICENSE
GERMANY = "84"  # catalogue value of `Land` for Germany
WITHOUT_MUNICIPALITY = "(municipality not published)"

# Unit files of the export (without the _1, _2 ... suffix) and what they contain
UNIT_FILES = {
    "EinheitenWind": "EinheitWind",
    "EinheitenSolar": "EinheitSolar",
    "EinheitenBiomasse": "EinheitBiomasse",
    "EinheitenWasser": "EinheitWasser",
    "EinheitenVerbrennung": "EinheitVerbrennung",
    "EinheitenKernkraft": "EinheitKernkraft",
    "EinheitenGeothermieGrubengasDruckentspannung": "EinheitGeothermieGrubengasDruckentspannung",
    "EinheitenStromSpeicher": "EinheitStromSpeicher",
}
HEAVY_FILES = {"EinheitenSolar", "EinheitenStromSpeicher"}  # ~1.6 GB of the ~1.7 GB

# Status values, by their catalogue text
IN_OPERATION = "In Betrieb"
TEMPORARILY_CLOSED = "Vorübergehend stillgelegt"
CLOSED = "Endgültig stillgelegt"
PLANNED = "In Planung"

COMBUSTION = {
    "Braunkohle": "lignite",
    "Steinkohle": "hard_coal",
    "Erdgas": "natural_gas",
    "Mineralölprodukte": "oil",
    "nicht biogener Abfall": "waste",
    "Wasserstoff": "hydrogen",
}
SOLAR_TYPES = {
    "Gebäudesolaranlage": "solar_rooftop",
    "Freiflächensolaranlage": "solar_ground",
    "Steckerfertige Solaranlage (sog. Balkonkraftwerk)": "solar_balcony",
}
STORAGE = {"Batterie": "battery", "Pumpspeicher": "pumped_hydro"}

TECHNOLOGIES = [
    "wind_onshore", "wind_offshore", "solar_rooftop", "solar_ground", "solar_balcony",
    "solar_other", "biomass", "hydro", "lignite", "hard_coal", "natural_gas", "oil",
    "waste", "hydrogen", "other_combustion", "nuclear", "geothermal_mine_gas_other",
    "battery", "pumped_hydro", "other_storage",
]  # fmt: skip
# Groups a question may ask for
TECHNOLOGY_GROUPS = {
    "wind": ["wind_onshore", "wind_offshore"],
    "solar": ["solar_rooftop", "solar_ground", "solar_balcony", "solar_other"],
    "renewable": ["wind_onshore", "wind_offshore", "solar_rooftop", "solar_ground",
                  "solar_balcony", "solar_other", "biomass", "hydro"],
    "coal": ["lignite", "hard_coal"],
    "fossil": ["lignite", "hard_coal", "natural_gas", "oil", "other_combustion"],
    "storage": ["battery", "pumped_hydro", "other_storage"],
}  # fmt: skip


class MastrError(Exception):
    """The register could not be downloaded or read."""


# ---------- reading single files out of the remote ZIP ----------


class RemoteFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests (what `zipfile` needs)."""

    def __init__(self, url: str, http: httpx.Client | None = None, retries: int = 5,
                 sleep: Callable[[float], None] = time.sleep) -> None:  # fmt: skip
        self.url, self.retries, self.sleep = url, retries, sleep
        self.http = http or httpx.Client(timeout=120)
        self.pos, self.requests = 0, 0
        first = self._get(0, 0)  # also tells the total size (HEAD is unreliable there)
        self.size = int(first.headers["Content-Range"].split("/")[1])

    def _get(self, start: int, end: int) -> httpx.Response:
        for attempt in range(self.retries):
            try:
                response = self.http.get(self.url, headers={"Range": f"bytes={start}-{end}"})
                if response.status_code == 206:
                    return response
                error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:  # the server sometimes drops connections
                error = type(exc).__name__
            self.sleep(2.0 * (attempt + 1))
        raise MastrError(f"Download failed after {self.retries} attempts ({error}).")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buffer: Any) -> int:
        n = min(len(buffer), self.size - self.pos)
        if n <= 0:
            return 0
        data = self._get(self.pos, self.pos + n - 1).content
        self.requests += 1
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def open_remote_zip(url: str, http: httpx.Client | None = None) -> zipfile.ZipFile:
    """4 MB reads: few requests, small memory."""
    return zipfile.ZipFile(io.BufferedReader(RemoteFile(url, http), buffer_size=4 << 20))


def latest_export_url(http: httpx.Client | None = None) -> str:
    """The newest daily export linked on the download page."""
    page = (http or httpx.Client(timeout=60)).get(DOWNLOAD_PAGE).text
    urls = sorted(
        set(m.group(0) for m in EXPORT_PATTERN.finditer(page)),
        key=lambda u: EXPORT_PATTERN.match(u).group(1),  # type: ignore[union-attr]
    )
    if not urls:
        raise MastrError("No export link found on the MaStR download page.")
    return urls[-1]


def export_date(url: str) -> str:
    """'...Gesamtdatenexport_20261115_26.2.zip' -> '2026-11-15'."""
    match = EXPORT_PATTERN.match(url) or re.search(r"_(\d{8})_", url)
    if not match:
        raise MastrError(f"No date in export name: {url}")
    d = match.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


# ---------- parsing ----------


def records(stream: IO[bytes], tag: str) -> Iterator[dict[str, str]]:
    """One dict per <tag> element (field -> text), streamed; UTF-16 is handled by expat."""
    for _, element in ET.iterparse(stream):
        if element.tag == tag:
            yield {child.tag: child.text or "" for child in element}
            element.clear()


def read_catalogue(stream: IO[bytes]) -> dict[str, str]:
    """Katalogwerte.xml -> {id: text}."""
    return {r["Id"]: r["Wert"] for r in records(stream, "Katalogwert")}


def technology(kind: str, unit: dict[str, str], catalogue: dict[str, str]) -> str:
    """Our technology name for one unit of a given file kind."""
    text = lambda name: catalogue.get(unit.get(name, ""), "")  # noqa: E731
    if kind == "EinheitenWind":
        at_sea = text("WindAnLandOderAufSee").endswith("auf See")
        return "wind_offshore" if at_sea else "wind_onshore"
    if kind == "EinheitenSolar":
        art = text("ArtDerSolaranlage") or text("SolaranlagenKategorie")
        return SOLAR_TYPES.get(art, "solar_other")
    if kind == "EinheitenVerbrennung":
        return COMBUSTION.get(text("Energietraeger"), "other_combustion")
    if kind == "EinheitenStromSpeicher":
        return STORAGE.get(text("Technologie"), "other_storage")
    return {
        "EinheitenBiomasse": "biomass",
        "EinheitenWasser": "hydro",
        "EinheitenKernkraft": "nuclear",
        "EinheitenGeothermieGrubengasDruckentspannung": "geothermal_mine_gas_other",
    }[kind]


# ---------- aggregation ----------


@dataclass
class Totals:
    added_units: int = 0
    added_kw: float = 0.0
    removed_units: int = 0
    removed_kw: float = 0.0
    community_kw: float = 0.0  # added as community energy (Bürgerenergie; wind only)


@dataclass
class Aggregator:
    """Collects units into (municipality, technology, year) totals."""

    catalogue: dict[str, str]
    rows: dict[tuple[str, str, int], Totals] = field(default_factory=lambda: defaultdict(Totals))
    planned: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    places: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, kind: str, unit: dict[str, str]) -> None:
        if unit.get("Land") and unit["Land"] != GERMANY:
            self.skipped["outside Germany"] += 1
            return
        status = self.catalogue.get(unit.get("EinheitBetriebsstatus", ""), "")
        tech = technology(kind, unit, self.catalogue)
        kw = float(unit.get("Nettonennleistung") or 0)
        place = self._place(unit)
        if status == PLANNED:
            entry = self.planned.setdefault((place, tech), [0, 0.0])
            entry[0] += 1
            entry[1] += kw
            return
        if status not in (IN_OPERATION, TEMPORARILY_CLOSED, CLOSED):
            self.skipped[f"status '{status or 'none'}'"] += 1
            return
        commissioned = unit.get("Inbetriebnahmedatum", "")
        if not commissioned:
            self.skipped["no commissioning date"] += 1
            return
        totals = self.rows[(place, tech, int(commissioned[:4]))]
        totals.added_units += 1
        totals.added_kw += kw
        if unit.get("Buergerenergie") == "1":
            totals.community_kw += kw
        if status == CLOSED:
            closed = unit.get("DatumEndgueltigeStilllegung", "")
            if not closed:
                self.skipped["closed without date (counted as never closed)"] += 1
                return
            removed = self.rows[(place, tech, int(closed[:4]))]
            removed.removed_units += 1
            removed.removed_kw += kw

    def _place(self, unit: dict[str, str]) -> str:
        """Municipality key (AGS, 8 digits).

        Large plants on the high-voltage grid are published with their district but
        without their municipality (critical infrastructure): they get a district key.
        Units at sea or without any place get a state key.
        """
        state = self.catalogue.get(unit.get("Bundesland", ""), "unknown")
        key = unit.get("Gemeindeschluessel", "")
        district = unit.get("Landkreis", "")
        if not key and district:
            key = f"district:{state}:{district}"
            self.places.setdefault(key, (WITHOUT_MUNICIPALITY, district, state))
        elif not key:
            key = f"state:{state}"
            self.places.setdefault(key, (WITHOUT_MUNICIPALITY, "(no district)", state))
        elif key not in self.places:
            self.places[key] = (unit.get("Gemeinde", ""), unit.get("Landkreis", ""), state)
        return key

    def to_json(self, source_url: str) -> dict[str, Any]:
        return {
            "export_date": export_date(source_url),
            "source_url": source_url,
            "license": LICENSE,
            "attribution": ATTRIBUTION,
            "columns": ["municipality_key", "technology", "year", "added_units", "added_kw",
                        "removed_units", "removed_kw", "community_kw"],
            "rows": [[k[0], k[1], k[2], t.added_units, round(t.added_kw, 3), t.removed_units,
                      round(t.removed_kw, 3), round(t.community_kw, 3)]
                     for k, t in sorted(self.rows.items())],
            "planned": [[k[0], k[1], v[0], round(v[1], 3)]
                        for k, v in sorted(self.planned.items())],
            "places": {k: list(v) for k, v in sorted(self.places.items())},
            "skipped": dict(self.skipped),
        }  # fmt: skip


def sync_register(
    out_file: Path,
    url: str | None = None,
    skip_heavy: bool = False,
    progress: Callable[[str], None] = lambda message: None,
    http: httpx.Client | None = None,
) -> dict[str, Any]:
    """Download what we need from the newest export and write the aggregate (gzip JSON)."""
    url = url or latest_export_url(http)
    archive = open_remote_zip(url, http)
    names = archive.namelist()
    with archive.open("Katalogwerte.xml") as stream:
        aggregator = Aggregator(read_catalogue(stream))
    for kind, tag in UNIT_FILES.items():
        if skip_heavy and kind in HEAVY_FILES:
            continue
        parts = sorted((n for n in names if re.fullmatch(rf"{kind}(_\d+)?\.xml", n)),
                       key=lambda n: int(re.sub(r"\D", "", n) or 0))  # fmt: skip
        for i, name in enumerate(parts, 1):
            progress(f"{name} ({i}/{len(parts)})")
            with archive.open(name) as stream:
                for unit in records(stream, tag):
                    aggregator.add(kind, unit)
    data = aggregator.to_json(url)
    data["complete"] = not skip_heavy
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(out_file)  # never leave a half-written register behind
    return data


def load_register(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)
