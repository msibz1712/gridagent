"""North Rhine-Westphalia from the plant register, for the public page.

Capacity, not generation: the register lists plants, not what they produced (NRW's
official monthly generation leaves out wind and solar). Every figure says so.
"""

from datetime import date
from typing import Any

from gridagent.tools.register import PlantRegister, query

STATE = "Nordrhein-Westfalen"
FIRST_YEAR = 2021


def nrw_snapshot(register: PlantRegister, day: date) -> dict[str, Any]:
    """Installed capacity by technology, onshore wind added per year, top districts this year."""
    year = min(day.year, register.last_year)

    def installed(technology: str) -> dict[str, Any]:
        q = query(register, technology, state=STATE, from_year=year, to_year=year)
        return {"mw": q["installed_mw"], "units": q["installed_units"],
                "added_this_year_mw": q["added_mw"], "planned_mw": q["planned_mw"]}  # fmt: skip

    wind_years = query(register, "wind_onshore", state=STATE, from_year=FIRST_YEAR,
                       to_year=year, group_by="year")["groups"]  # fmt: skip
    districts = query(register, "wind_onshore", state=STATE, from_year=year, to_year=year,
                      group_by="district")["groups"]  # fmt: skip
    return {
        "register_date": register.export_date,
        "year": year,
        "wind_onshore": installed("wind_onshore"),
        "solar": installed("solar") if register.data.get("complete", True) else None,
        "battery": installed("battery") if register.data.get("complete", True) else None,
        "lignite": installed("lignite"),
        "wind_added_per_year": [{"year": int(g["name"]), "mw": g["added_mw"]} for g in wind_years],
        "top_wind_districts": [
            {"district": g["name"].rsplit(" (", 1)[0], "mw": g["added_mw"]}
            for g in districts[:5] if "name" in g and g["added_mw"] > 0
        ],
    }  # fmt: skip
