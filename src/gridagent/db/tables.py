"""Which tables exist and what goes in them: the single source for the database schema.

Design choices (and why):
- One table per data endpoint, in WIDE format (one column per series). With
  15-minute data this is far smaller than one row per (time, series, value):
  roughly 4 MB per table per year, so a decade fits Supabase's free 500 MB.
- Values are stored as `real` (4-byte float, ~7 significant digits). Energy-Charts
  values have at most 3 decimals and fewer than 7 significant digits, so they are
  rounded back to 3 decimals when read and come out exactly as delivered.
- Everything lives in its own schema `energy`, which Supabase does not expose
  through its web API (only `public` is exposed by default).
- `energy.sync_log` records which date ranges were loaded. A period is only served
  from the database if it is fully covered; anything else falls back to the API.
"""

from dataclasses import dataclass

from gridagent.data.smard import FILTERS as SMARD_SERIES
from gridagent.tools.definitions import NEIGHBOURS

SCHEMA = "energy"

GENERATION_COLUMNS = (
    "hydro_pumped_storage_consumption", "cross_border_electricity_trading", "hydro_run_of_river",
    "biomass", "fossil_brown_coal_lignite", "fossil_hard_coal", "fossil_oil",
    "fossil_coal_derived_gas", "fossil_gas", "geothermal", "hydro_water_reservoir",
    "hydro_pumped_storage", "others", "waste", "wind_offshore", "wind_onshore", "solar", "nuclear",
    "residual_load", "renewable_share_of_load", "renewable_share_of_generation",
)  # fmt: skip
ENERGY_CHARTS_LICENSE = (
    "CC BY 4.0 (creativecommons.org/licenses/by/4.0), attribution: energy-charts.info"
)


@dataclass(frozen=True)
class TableSpec:
    name: str  # table name inside the `energy` schema
    endpoint: str  # TimeSeriesResponse.endpoint of the data it stores
    series: tuple[str, ...]  # one column per series
    unit: str
    license: str
    regional: bool = False  # True: primary key (region, ts) instead of (ts)
    attributes: tuple[tuple[str, str], ...] = ()

    @property
    def qualified(self) -> str:
        return f"{SCHEMA}.{self.name}"

    @property
    def keys(self) -> tuple[str, ...]:
        return ("region", "ts") if self.regional else ("ts",)


PRICE_LICENSE = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"
SIGN_CONVENTION = (("sign_convention", "positive = import, negative = export"),)

TABLES: dict[str, TableSpec] = {
    t.name: t
    for t in (
        TableSpec(
            name="public_power",
            endpoint="public_power",
            series=(*GENERATION_COLUMNS, "load"),
            unit="MW",
            license=ENERGY_CHARTS_LICENSE,
        ),
        TableSpec(
            name="total_power",
            endpoint="total_power",
            series=(*GENERATION_COLUMNS, "load_incl_self_consumption"),
            unit="MW",
            license=ENERGY_CHARTS_LICENSE,
        ),
        TableSpec(
            name="day_ahead_price",
            endpoint="price",
            series=("day_ahead_price",),
            unit="EUR / MWh",
            license=PRICE_LICENSE,
        ),
        TableSpec(
            name="cross_border_physical",
            endpoint="cbpf",
            series=(*NEIGHBOURS, "sum"),
            unit="GW",
            license=ENERGY_CHARTS_LICENSE,
            attributes=SIGN_CONVENTION,
        ),
        TableSpec(
            name="cross_border_trading",
            endpoint="cbet",
            series=(*NEIGHBOURS, "sum"),
            unit="GW",
            license=ENERGY_CHARTS_LICENSE,
            attributes=SIGN_CONVENTION,
        ),
        TableSpec(
            name="smard_regional",
            endpoint="smard",
            series=tuple(SMARD_SERIES),
            unit="MW",
            license="CC BY 4.0, Bundesnetzagentur | SMARD.de",
            regional=True,
        ),
    )
}

PERCENT_SERIES = {"renewable_share_of_load", "renewable_share_of_generation"}


def schema_sql() -> str:
    """Idempotent DDL: running it again changes nothing (safe for every deploy)."""
    statements = [f"create schema if not exists {SCHEMA};"]
    for table in TABLES.values():
        key_columns = ["region text not null", "ts timestamptz not null"] if table.regional else [
            "ts timestamptz not null"]  # fmt: skip
        value_columns = [f'"{column}" real' for column in table.series]  # quoted: e.g. "sum"
        columns = ",\n  ".join([*key_columns, *value_columns])
        statements.append(
            f"create table if not exists {table.qualified} (\n  {columns},\n"
            f"  primary key ({', '.join(table.keys)})\n);"
        )
        for column in table.series:  # new series added later -> new nullable column
            statements.append(
                f'alter table {table.qualified} add column if not exists "{column}" real;'
            )
    statements.append(f"""create table if not exists {SCHEMA}.sync_log (
  id bigint generated always as identity primary key,
  table_name text not null,
  region text not null default '',
  date_from date not null,
  date_until date not null,
  rows_written integer not null,
  synced_at timestamptz not null default now()
);""")
    # Nothing in this schema is reachable through Supabase's web API roles.
    statements.append(
        f"do $$ begin if exists (select 1 from pg_roles where rolname = 'anon') then "
        f"revoke all on schema {SCHEMA} from anon, authenticated; end if; end $$;"
    )
    return "\n".join(statements)
