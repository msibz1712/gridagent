"""Questions on the plant register aggregate (see data/mastr.py for how it is built).

Installed capacity at the end of a year = everything commissioned up to that
year minus everything permanently decommissioned up to that year. Temporarily
closed units still count as installed (they can come back), as in the register.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from gridagent.data.mastr import TECHNOLOGIES, TECHNOLOGY_GROUPS

# Built from the two large files a light sync skips (EinheitenSolar, EinheitenStromSpeicher)
HEAVY_TECHNOLOGIES = {t for t in TECHNOLOGIES if t.startswith("solar") or t in
                      ("battery", "pumped_hydro", "other_storage")}  # fmt: skip

MAX_GROUPS = 25

STATES = [
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen", "Hamburg", "Hessen",
    "Mecklenburg-Vorpommern", "Niedersachsen", "Nordrhein-Westfalen", "Rheinland-Pfalz",
    "Saarland", "Sachsen", "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
    "Ausschließliche Wirtschaftszone",  # offshore, outside any state
]  # fmt: skip


class RegisterQueryError(ValueError):
    """The question does not match the register (unknown place, ambiguous name, ...)."""


@dataclass
class PlantRegister:
    """The aggregate, indexed for queries."""

    data: dict[str, Any]

    @property
    def export_date(self) -> str:
        return self.data["export_date"]

    @property
    def last_year(self) -> int:
        return int(self.export_date[:4])

    def place(self, key: str) -> tuple[str, str, str]:
        name, district, state = self.data["places"][key]
        return name, district, state

    def find_keys(self, state: str | None, district: str | None, municipality: str | None,
                  ) -> set[str]:  # fmt: skip
        """Municipality keys matching the area; names match exactly, else as a unique part."""
        keys = set(self.data["places"])
        if state is not None:
            if state not in STATES:
                raise RegisterQueryError(f"Unknown state '{state}'. Use: {', '.join(STATES)}.")
            keys = {k for k in keys if self.place(k)[2] == state}
        if district is not None:
            keys = self._match(keys, district, index=1, what="district")
        if municipality is not None:
            keys = self._match(keys, municipality, index=0, what="municipality")
        return keys

    def _match(self, keys: set[str], wanted: str, index: int, what: str) -> set[str]:
        names = {self.place(k)[index] for k in keys}
        exact = {n for n in names if n.casefold() == wanted.casefold()}
        found = exact or {n for n in names if wanted.casefold() in n.casefold()}
        if not found:
            raise RegisterQueryError(f"No {what} matching '{wanted}' in the register.")
        if len(found) > 1:
            listed = ", ".join(sorted(found)[:10])
            raise RegisterQueryError(f"'{wanted}' matches several {what}s: {listed}. Be exact.")
        name = found.pop()
        return {k for k in keys if self.place(k)[index] == name}


def _check_complete(register: PlantRegister, techs: set[str]) -> None:
    """A light sync has no solar and storage: refuse rather than report 0 MW."""
    missing = techs & HEAVY_TECHNOLOGIES
    if not register.data.get("complete", True) and missing:
        raise RegisterQueryError(
            f"This copy of the register has no data for {', '.join(sorted(missing))} "
            "(downloaded without solar and storage)."
        )


def technologies(name: str) -> list[str]:
    if name in TECHNOLOGY_GROUPS:
        return TECHNOLOGY_GROUPS[name]
    if name in TECHNOLOGIES:
        return [name]
    raise RegisterQueryError(f"Unknown technology '{name}'.")


@dataclass
class _Sums:
    installed_units: int = 0
    installed_kw: float = 0.0
    added_units: int = 0
    added_kw: float = 0.0
    removed_units: int = 0
    removed_kw: float = 0.0
    community_kw: float = 0.0

    def out(self) -> dict[str, Any]:
        return {
            "installed_units": self.installed_units,
            "installed_mw": round(self.installed_kw / 1000, 2),
            "added_units": self.added_units,
            "added_mw": round(self.added_kw / 1000, 2),
            "removed_units": self.removed_units,
            "removed_mw": round(self.removed_kw / 1000, 2),
            "net_change_mw": round((self.added_kw - self.removed_kw) / 1000, 2),
        }


def query(
    register: PlantRegister,
    technology: str,
    state: str | None = None,
    district: str | None = None,
    municipality: str | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    group_by: str | None = None,
) -> dict[str, Any]:
    """Installed at the end of `to_year`; added and removed from `from_year` to `to_year`."""
    techs = set(technologies(technology))
    _check_complete(register, techs)
    last = register.last_year
    to_year = last if to_year is None else to_year
    from_year = to_year if from_year is None else from_year
    if from_year > to_year:
        raise RegisterQueryError("from_year is after to_year.")
    if to_year > last:
        raise RegisterQueryError(f"The register is from {register.export_date}: no later data.")
    keys = register.find_keys(state, district, municipality)

    total = _Sums()
    groups: dict[str, _Sums] = defaultdict(_Sums)
    per_year: dict[int, list[tuple[int, float, int, float, float]]] = defaultdict(list)
    for key, tech, year, a_units, a_kw, r_units, r_kw, c_kw in register.data["rows"]:
        if key not in keys or tech not in techs or year > to_year:
            continue
        sums = [total]
        if group_by not in (None, "year"):
            sums.append(groups[_group(register, key, tech, year, group_by)])
        elif group_by == "year":
            per_year[year].append((a_units, a_kw, r_units, r_kw, c_kw))
        for s in sums:
            s.installed_units += a_units - r_units
            s.installed_kw += a_kw - r_kw
            if year >= from_year:
                s.added_units += a_units
                s.added_kw += a_kw
                s.removed_units += r_units
                s.removed_kw += r_kw
                s.community_kw += c_kw

    planned_units, planned_kw = 0, 0.0
    for key, tech, units, kw in register.data["planned"]:
        if key in keys and tech in techs:
            planned_units += units
            planned_kw += kw

    area = municipality or district or state or "Germany"
    result: dict[str, Any] = {
        "technology": technology,
        "includes": sorted(techs),
        "area": area,
        "installed_at_end_of": to_year if to_year < last else f"{register.export_date} (latest)",
        "changes_period": f"{from_year} to {to_year}"
        + (f" (to {register.export_date})" if to_year == last else ""),
        **total.out(),
        "planned_units": planned_units,
        "planned_mw": round(planned_kw / 1000, 2),
        "register_date": register.export_date,
    }
    if techs & {"wind_onshore", "wind_offshore"}:
        result["added_as_community_energy_mw"] = round(total.community_kw / 1000, 2)
    if group_by == "year":
        result["group_by"] = "year"
        result["groups"] = _by_year(per_year, from_year, to_year)
    elif group_by is not None:
        result["group_by"] = group_by
        result["groups"] = _ranked(groups, total.installed_kw)
    return result


def _by_year(per_year: dict[int, list], from_year: int, to_year: int) -> list[dict[str, Any]]:
    """Each year: added and removed that year, installed at its end (running total)."""
    stock, years = _Sums(), []
    for year in range(min([*per_year, from_year]), to_year + 1):
        year_sums = _Sums()
        for a_units, a_kw, r_units, r_kw, _community in per_year.get(year, []):
            year_sums.added_units += a_units
            year_sums.added_kw += a_kw
            year_sums.removed_units += r_units
            year_sums.removed_kw += r_kw
        stock.installed_units += year_sums.added_units - year_sums.removed_units
        stock.installed_kw += year_sums.added_kw - year_sums.removed_kw
        if year >= from_year:
            year_sums.installed_units = stock.installed_units
            year_sums.installed_kw = stock.installed_kw
            years.append({"name": str(year), **year_sums.out()})
    return years


def _group(register: PlantRegister, key: str, tech: str, year: int, group_by: str) -> str:
    name, district, state = register.place(key)
    if group_by == "state":
        return state
    if group_by == "district":
        return f"{district} ({state})"
    if group_by == "municipality":
        return f"{name}, {district} ({key})"
    if group_by == "technology":
        return tech
    raise RegisterQueryError(f"Unknown group_by '{group_by}'.")


def _ranked(groups: dict[str, _Sums], total_kw: float) -> list[dict[str, Any]]:
    """Places and technologies by added capacity (or installed, if nothing was added), top 25.
    Each with its share of the area's installed capacity (models otherwise compute it)."""

    def share(kw: float) -> float | None:
        return round(100 * kw / total_kw, 2) if total_kw else None

    rows = [
        {"name": name, **sums.out(), "share_of_installed_pct": share(sums.installed_kw)}
        for name, sums in groups.items()
    ]
    key = "added_mw" if any(r["added_mw"] for r in rows) else "installed_mw"
    rows.sort(key=lambda r: r[key], reverse=True)
    if len(rows) > MAX_GROUPS:
        rest = len(rows) - MAX_GROUPS
        return rows[:MAX_GROUPS] + [{"note": f"{rest} more, ranked by {key}"}]
    return rows


PANEL_COLUMNS = [
    "municipality_key", "municipality", "district", "state", "technology", "year",
    "added_units", "added_mw", "removed_units", "removed_mw", "installed_units", "installed_mw",
    "community_added_mw",
]  # fmt: skip


def panel(
    register: PlantRegister, state: str | None, technology: str, first_year: int = 2000
) -> list[dict[str, Any]]:
    """A balanced municipality x technology x year panel (every year, also those with no
    change), for research in R, Stata or pandas. Installed = at the end of the year."""
    techs = set(technologies(technology))
    _check_complete(register, techs)
    keys = register.find_keys(state, None, None)
    events: dict[tuple[str, str], dict[int, list[float]]] = defaultdict(dict)
    for key, tech, year, a_units, a_kw, r_units, r_kw, c_kw in register.data["rows"]:
        if key in keys and tech in techs:
            events[(key, tech)][year] = [a_units, a_kw, r_units, r_kw, c_kw]
    out = []
    for (key, tech), by_year in sorted(events.items()):
        name, district, state_name = register.place(key)
        units = kw = 0.0
        for year in range(min(by_year), register.last_year + 1):
            a_units, a_kw, r_units, r_kw, c_kw = by_year.get(year, [0, 0.0, 0, 0.0, 0.0])
            units += a_units - r_units
            kw += a_kw - r_kw
            if year < first_year:
                continue
            out.append(dict(zip(PANEL_COLUMNS, [
                key, name, district, state_name, tech, year, a_units, round(a_kw / 1000, 4),
                r_units, round(r_kw / 1000, 4), int(units), round(kw / 1000, 4),
                round(c_kw / 1000, 4),
            ], strict=True)))  # fmt: skip
    return out
