"""Runs the agent's tools and keeps fetched data for the duration of one run.

Design choice: data stays here, not in the conversation. A week of
generation data is 672 rows x 21 series; sending it to the model would
cost thousands of tokens per question, and the model could copy numbers
wrongly. So `get_*` tools store the data under a short `ref` (e.g.
"gen_1") and return only a summary. `compute_stats` takes that ref.
Small tables (installed capacity, official statistics) are returned directly.

Every tool returns a `ToolResult`. Errors (bad input, no data, API down)
come back as `is_error=True` with a readable message, so the model can
correct itself or tell the user, and the run never crashes on a tool.
"""

import json
import math
from collections.abc import Callable
from datetime import UTC, date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.models import DataRow, Series, SeriesInfo, TimeSeriesResponse
from gridagent.data.netztransparenz import SERIES_NAMES, summarize, to_series
from gridagent.data.provider import DataProvider
from gridagent.data.reference import lookup
from gridagent.data.sources import SOURCE_GROUPS, available_sources, source_series
from gridagent.tools.conditional import Condition, stats_when
from gridagent.tools.definitions import (
    ALL_TOOLS,
    CAPACITY_TECHNOLOGIES,
    NEIGHBOUR_ZONES,
    REF_PREFIX,
)
from gridagent.tools.flexibility import Battery, BatteryRun, plan_load, simulate_battery
from gridagent.tools.grounding import extract_numbers, is_backed
from gridagent.tools.register import PlantRegister, query
from gridagent.tools.stats import Operation, StatsResult, capacity_factor, compute_stats, daily

MAX_DAYS = 93
MAX_REGIONAL_SOURCES = 3
ENERGY_CHARTS = "Energy-Charts.info (Fraunhofer ISE)"
DATA_SOURCE = {
    "public_power": ENERGY_CHARTS,
    "total_power": f"{ENERGY_CHARTS}, total net generation incl. industrial self-supply",
    "price": f"{ENERGY_CHARTS}; prices: Bundesnetzagentur | SMARD.de",
    "cbpf": f"{ENERGY_CHARTS}, physical cross-border flows",
    "cbet": f"{ENERGY_CHARTS}, scheduled commercial exchanges",
    "smard": "SMARD.de (Bundesnetzagentur)",
    "weather": "Deutscher Wetterdienst (DWD), 65 stations",
    "price_spread": f"{ENERGY_CHARTS}; prices: Bundesnetzagentur | SMARD.de",
    "redispatch": "netztransparenz.de (the four German TSOs)",
    "battery": f"Own calculation on day-ahead prices ({ENERGY_CHARTS}; BNetzA | SMARD.de)",
}
# Installed-capacity series used for capacity factors (solar: AC = inverter capacity).
CAPACITY_SERIES = {
    "solar": ["solar_ac"],
    "wind": ["wind_onshore", "wind_offshore"],
    "wind_onshore": ["wind_onshore"],
    "wind_offshore": ["wind_offshore"],
    "biomass": ["biomass"],
}
CAPACITY_COLUMNS = {
    "solar": ["solar_ac", "solar_dc", "solar_planned_eeg_2023"],
    "wind_onshore": ["wind_onshore", "wind_onshore_planned_eeg_2023"],
    "wind_offshore": ["wind_offshore", "wind_offshore_planned_windseeg"],
    "battery_storage": ["battery_storage_power", "battery_storage_capacity"],
}


class ToolResult(BaseModel):
    content: str  # JSON or a plain error message; sent back to the model
    is_error: bool = False


# ---------- validated tool inputs (defence in depth on top of `strict`) ----------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PeriodInput(_Input):
    start: date
    end: date


class GenerationInput(PeriodInput):
    sources: list[str] | None = None
    scope: Literal["public", "total"] | None = None


class FlowsInput(PeriodInput):
    kind: Literal["physical", "trading"]


class RegionalInput(PeriodInput):
    region: str
    sources: list[str] = Field(min_length=1, max_length=MAX_REGIONAL_SOURCES)


class CapacityInput(_Input):
    time_step: Literal["yearly", "monthly"]
    technologies: list[str] | None = None


class ReferenceInput(_Input):
    dataset: str
    period: str | None = None


class StatsInput(_Input):
    ref: str
    operation: Operation
    source: str | None = None
    relative_to: str | None = None
    per_day: bool = False
    threshold: float | None = None
    weight_ref: str | None = None
    weight_source: str | None = None


class CompareInput(_Input):
    a: float
    b: float
    a_label: str
    b_label: str
    unit: str


class ConditionInput(_Input):
    ref: str
    source: str | None = None
    below: float | None = None
    above: float | None = None


class WhenInput(_Input):
    operation: Literal["hours", "mean", "min", "max", "energy"]
    ref: str | None = None
    source: str | None = None
    conditions: list[ConditionInput] = Field(min_length=1, max_length=3)


class ZoneInput(PeriodInput):
    zone: str


class BatteryInput(_Input):
    ref: str
    power_mw: float = Field(gt=0, le=10_000)
    capacity_mwh: float = Field(gt=0, le=100_000)
    round_trip_efficiency: float | None = None
    wear_cost_eur_per_mwh: float | None = None


class RegisterInput(_Input):
    technology: str
    state: str | None = None
    district: str | None = None
    municipality: str | None = None
    from_year: int | None = Field(None, ge=1900, le=2100)
    to_year: int | None = Field(None, ge=1900, le=2100)
    group_by: Literal["state", "district", "municipality", "technology", "year"] | None = None


class LoadInput(_Input):
    ref: str
    energy_mwh: float = Field(gt=0)
    max_power_mw: float = Field(gt=0)


class ToolInputError(Exception):
    """The model asked for something invalid; message is shown to the model."""


class ToolBox:
    """Executes tools by name. Create one per agent run (it holds that run's data)."""

    def __init__(
        self,
        data: DataProvider,
        today: Callable[[], date],
        enabled: list[str] | None = None,
    ) -> None:
        self.data = data
        self.today = today
        self.enabled = enabled or list(ALL_TOOLS)
        unknown = set(self.enabled) - set(ALL_TOOLS)
        if unknown:
            raise ValueError(f"Unknown tools: {sorted(unknown)}")
        self.store: dict[str, TimeSeriesResponse] = {}
        self._counter = 0
        # Successful data requests -> their summary. In a conversation the model may ask
        # for the same data again; it then gets the existing ref instead of a copy.
        self._fetched: dict[str, str] = {}
        # Every successful result of this conversation: what numbers may be backed by
        # (compare_values inputs, and the check of the final answer; see grounding.py).
        self.results: list[str] = []
        self.questions: list[str] = []

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions for the API request, in a fixed order (stable for caching)."""
        return [ALL_TOOLS[name] for name in self.enabled]

    def execute(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        handlers = {
            "get_generation": self._get_generation,
            "get_day_ahead_price": self._get_day_ahead_price,
            "get_cross_border_flows": self._get_cross_border_flows,
            "get_regional_generation": self._get_regional_generation,
            "get_installed_capacity": self._get_installed_capacity,
            "get_reference_statistics": self._get_reference_statistics,
            "compute_stats": self._compute_stats,
            "compare_values": self._compare_values,
            "get_weather": self._get_weather,
            "compute_when": self._compute_when,
            "get_neighbour_price": self._get_neighbour_price,
            "get_price_spread": self._get_price_spread,
            "get_redispatch": self._get_redispatch,
            "simulate_battery": self._simulate_battery,
            "plan_flexible_load": self._plan_flexible_load,
            "get_plant_register": self._get_plant_register,
        }
        if name not in self.enabled:
            return ToolResult(content=f"Unknown tool '{name}'.", is_error=True)
        request_key = f"{name}:{json.dumps(tool_input, sort_keys=True)}"
        if request_key in self._fetched:
            summary = json.loads(self._fetched[request_key])
            summary["note"] = "Same data as fetched earlier in this conversation: reuse this ref."
            return ToolResult(content=json.dumps(summary))
        try:
            content = json.dumps(handlers[name](tool_input))
            if name in REF_PREFIX:
                self._fetched[request_key] = content
            self.results.append(content)
            return ToolResult(content=content)
        except ValidationError as exc:
            return ToolResult(content=f"Invalid input: {_short(exc)}", is_error=True)
        except (ToolInputError, KeyError, ValueError) as exc:
            return ToolResult(content=str(exc).strip("'\""), is_error=True)
        except NoDataError as exc:
            return ToolResult(content=f"No data available: {exc}", is_error=True)
        except EnergyChartsError as exc:
            return ToolResult(content=f"Data source unavailable: {exc}", is_error=True)

    # ---------- data tools ----------

    def _get_generation(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = GenerationInput.model_validate(raw)
        self._check_period(args.start, args.end, latest=self.today())
        for source in args.sources or []:
            _check_source(source)
        if args.scope == "total":
            response = self.data.total_power(args.start, args.end)
        else:
            response = self.data.public_power(args.start, args.end)
        ref = self._save("get_generation", response)
        return self._summary(ref, response, args.start, args.end) | {
            "sources": args.sources or "all",
            "scope": args.scope or "public",
        }

    def _get_day_ahead_price(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = PeriodInput.model_validate(raw)
        # Day-ahead prices for tomorrow exist once the auction has run (around midday).
        self._check_period(args.start, args.end, latest=self.today() + timedelta(days=1))
        response = self.data.day_ahead_price(args.start, args.end)
        ref = self._save("get_day_ahead_price", response)
        return self._summary(ref, response, args.start, args.end)

    def _get_cross_border_flows(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = FlowsInput.model_validate(raw)
        self._check_period(args.start, args.end, latest=self.today())
        response = self.data.cross_border(args.start, args.end, kind=args.kind)
        ref = self._save("get_cross_border_flows", response)
        return self._summary(ref, response, args.start, args.end) | {
            "kind": args.kind,
            "sign_convention": "positive = import into Germany, negative = export",
            "series": response.series_ids(),
        }

    def _get_regional_generation(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = RegionalInput.model_validate(raw)
        self._check_period(args.start, args.end, latest=self.today())
        response = self.data.regional(args.region, args.sources, args.start, args.end)
        ref = self._save("get_regional_generation", response)
        return self._summary(ref, response, args.start, args.end) | {
            "region": args.region,
            "sources": args.sources,
        }

    def _get_installed_capacity(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = CapacityInput.model_validate(raw)
        for tech in args.technologies or []:
            if tech not in CAPACITY_TECHNOLOGIES:
                raise ToolInputError(f"Unknown technology '{tech}'.")
        response = self.data.installed_power(args.time_step)
        wanted = args.technologies or CAPACITY_TECHNOLOGIES
        columns = [c for t in wanted for c in CAPACITY_COLUMNS.get(t, [t])]
        columns = [c for c in columns if c in response.series_ids()]
        units = {s.id: s.unit for s in response.series}
        rows = response.data[-12:] if args.time_step == "yearly" else response.data[-24:]
        table = []
        for row in rows:
            values = {c: row.values.get(c) for c in columns if row.values.get(c) is not None}
            if values:
                label = row.timestamp.strftime("%Y" if args.time_step == "yearly" else "%Y-%m")
                table.append({"period_end_of": label, **values})
        return {
            "time_step": args.time_step,
            "units": {c: units.get(c) for c in columns},
            "note": "Values are installed capacity at the END of each period. Columns with "
            "'planned' are legal targets. solar_ac = inverter capacity, solar_dc = module "
            "capacity.",
            "values": table,
            "data_source": ENERGY_CHARTS,
            "license": response.license,
        }

    def _get_reference_statistics(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = ReferenceInput.model_validate(raw)
        return lookup(args.dataset, args.period)

    # ---------- compute ----------

    def _zone_prices(self, args: ZoneInput) -> TimeSeriesResponse:
        if args.zone not in NEIGHBOUR_ZONES:
            raise ToolInputError(f"Zone must be one of: {', '.join(NEIGHBOUR_ZONES)}.")
        self._check_period(args.start, args.end, latest=self.today() + timedelta(days=1))
        response = self.data.day_ahead_price(args.start, args.end, bzn=args.zone)
        if response.bidding_zone != args.zone:  # never let one zone's prices pose as another's
            raise ToolInputError(f"Expected {args.zone} prices, got {response.bidding_zone}.")
        return response

    def _get_neighbour_price(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = ZoneInput.model_validate(raw)
        response = self._zone_prices(args)
        ref = self._save("get_neighbour_price", response)
        return self._summary(ref, response, args.start, args.end) | {
            "zone": f"{args.zone} ({NEIGHBOUR_ZONES[args.zone]})"
        }

    def _get_price_spread(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Germany minus neighbour on the timestamps both zones have (same resolution)."""
        args = ZoneInput.model_validate(raw)
        neighbour = self._zone_prices(args)
        germany = self.data.day_ahead_price(args.start, args.end)
        theirs = {row.timestamp.astimezone(UTC): row.values.get("day_ahead_price")
                  for row in neighbour.data}  # fmt: skip
        rows = []
        for row in germany.data:
            ours = row.values.get("day_ahead_price")
            other = theirs.get(row.timestamp.astimezone(UTC))
            spread = None if ours is None or other is None else round(ours - other, 2)
            values = {"germany": ours, "neighbour": other, "spread": spread}
            rows.append(DataRow(timestamp=row.timestamp, values=values))
        response = germany.model_copy(update={
            "endpoint": "price_spread",
            "attributes": {"zone": f"{args.zone} ({NEIGHBOUR_ZONES[args.zone]})",
                           "sign_convention": "spread = Germany minus neighbour"},
            "series": [SeriesInfo(id="germany", name="Germany (DE-LU) price", unit="EUR / MWh"),
                       SeriesInfo(id="neighbour", name=f"{args.zone} price", unit="EUR / MWh"),
                       SeriesInfo(id="spread", name="Germany minus neighbour", unit="EUR / MWh")],
            "data": rows,
        })  # fmt: skip
        ref = self._save("get_price_spread", response)
        spreads = [row.values["spread"] for row in rows if row.values["spread"] is not None]
        per_interval_h = (response.interval_minutes or 60) / 60
        return self._summary(ref, response, args.start, args.end) | {
            # Counted here so no answer depends on reading the sign correctly.
            "hours_germany_dearer": sum(s > 0 for s in spreads) * per_interval_h,
            "hours_germany_cheaper": sum(s < 0 for s in spreads) * per_interval_h,
            "hours_equal_price": sum(s == 0 for s in spreads) * per_interval_h,
            "zone": response.attributes["zone"],
            "series": ["germany", "neighbour", "spread"],
            "sign_convention": "spread = Germany minus neighbour; positive = Germany dearer",
        }

    def _get_redispatch(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = PeriodInput.model_validate(raw)
        self._check_period(args.start, args.end, latest=self.today() - timedelta(days=1))
        measures = self.data.redispatch(args.start, args.end)
        response = to_series(measures, args.start, args.end)
        ref = self._save("get_redispatch", response)
        return self._summary(ref, response, args.start, args.end) | {
            "summary": summarize(measures, args.start, args.end),
            "series": SERIES_NAMES,
            "method": response.attributes["method"],
        }

    def _get_weather(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = PeriodInput.model_validate(raw)
        # DWD publishes observations up to yesterday.
        self._check_period(args.start, args.end, latest=self.today() - timedelta(days=1))
        response = self.data.weather(args.start, args.end)
        ref = self._save("get_weather", response)
        return self._summary(ref, response, args.start, args.end) | {
            "series": {s.id: s.unit for s in response.series},
            "caveat": response.attributes.get("caveat", ""),
        }

    def _compute_when(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Conditions -> effect (see conditional.py)."""
        args = WhenInput.model_validate(raw)
        conditions = []
        for c in args.conditions:
            if c.below is None and c.above is None:
                raise ToolInputError(f"Condition on {c.ref} needs `below` and/or `above`.")
            response = self._ref(c.ref)
            series = self.series_for(response, c.source)
            meaning = _spread_meaning(response, c) if c.source == "spread" else ""
            conditions.append(Condition(series, below=c.below, above=c.above, meaning=meaning))
        target = None
        if args.operation != "hours":
            if args.ref is None:
                raise ToolInputError(f"'{args.operation}' needs a target `ref`.")
            target = self.series_for(self._ref(args.ref), args.source)
        result = stats_when(args.operation, conditions, target)
        if result.get("value") is not None:
            if result["unit"] == "EUR / MWh":
                result["value_ct_per_kwh"] = round(result["value"] / 10, 3)
            elif result["unit"] == "MWh":
                result["value_gwh"] = round(result["value"] / 1000, 2)
        return result

    def _price_ref(self, ref: str) -> tuple[TimeSeriesResponse, Series]:
        response = self._ref(ref)
        if response.endpoint != "price":
            raise ToolInputError("This tool needs a price ref (get_day_ahead_price or "
                                 "get_neighbour_price).")  # fmt: skip
        return response, response.get_series("day_ahead_price")

    def _simulate_battery(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Day-ahead arbitrage of a battery (see flexibility.py)."""
        args = BatteryInput.model_validate(raw)
        response, prices = self._price_ref(args.ref)
        battery = Battery(
            power_mw=args.power_mw,
            capacity_mwh=args.capacity_mwh,
            round_trip_efficiency=0.9 if args.round_trip_efficiency is None
            else args.round_trip_efficiency,
            wear_eur_per_mwh=args.wear_cost_eur_per_mwh or 0.0,
        )  # fmt: skip
        run = simulate_battery(prices, battery)
        ref = self._save("simulate_battery", _battery_response(response, run))
        missing = sum(p is None for p in run.prices)
        return {
            "ref": ref,
            "zone": response.bidding_zone,
            "period": f"{run.days[0].day} to {run.days[-1].day}",
            "battery": {"power_mw": battery.power_mw, "capacity_mwh": battery.capacity_mwh,
                        "duration_h": round(battery.capacity_mwh / battery.power_mw, 2),
                        "round_trip_efficiency": battery.round_trip_efficiency,
                        "wear_cost_eur_per_mwh": battery.wear_eur_per_mwh},
            **run.summary(),
            "price_intervals_missing": missing,
            "exact": run.grid.exact,
            "assumptions": [
                "day-ahead market only, every day optimised with that day's known prices",
                "starts and ends every day empty",
                "power limit applies to the energy entering or leaving the battery cells",
                "losses split evenly between charging and discharging",
                "no grid fees, taxes, intraday or balancing markets, or bidding risk",
            ] + ([] if run.grid.exact else
                 [f"capacity rounded down to {round(run.grid.levels * run.grid.step, 3)} MWh "
                  "for the calculation"]),
            "series": {"battery_power": "MW (+ charging, - discharging)",
                       "stored_energy": "MWh", "price": "EUR / MWh"},
        }  # fmt: skip

    def _plan_flexible_load(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Cheapest intervals for a shiftable consumption (see flexibility.py)."""
        args = LoadInput.model_validate(raw)
        response, prices = self._price_ref(args.ref)
        if not prices.points:
            raise NoDataError("No prices in this ref.")
        return {
            "zone": response.bidding_zone,
            "period": f"{prices.start} to {prices.end}",
            **plan_load(prices, args.energy_mwh, args.max_power_mw),
            "note": "Day-ahead prices only; grid fees, taxes and levies are not included.",
        }

    def _get_plant_register(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Installed capacity and changes from the MaStR aggregate (see tools/register.py)."""
        args = RegisterInput.model_validate(raw)
        register = PlantRegister(self.data.plant_register())
        result = query(register, args.technology, args.state, args.district, args.municipality,
                       args.from_year, args.to_year, args.group_by)  # fmt: skip
        return result | {
            "data_source": "Marktstammdatenregister (Bundesnetzagentur)",
            "license": register.data["license"],
            "note": "Years by commissioning date; installed = commissioned minus permanently "
            "decommissioned; planned units are only those registered as planned. Units are "
            "often registered months late, so the latest months are revised upward later.",
        }

    def _compare_values(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Difference, % difference, ratio and sum of two numbers the tools produced."""
        args = CompareInput.model_validate(raw)
        for name, value in (("a", args.a), ("b", args.b)):
            written = f"{value:.10f}".rstrip("0").rstrip(".")
            pool = [x for text in self.results + self.questions for x in extract_numbers(text)]
            if not is_backed(value, written, pool):
                raise ToolInputError(
                    f"{name} = {written} does not appear in any earlier tool result or the "
                    "question. Fetch and compute it with the tools first, then compare."
                )
        difference = args.a - args.b
        return {
            "a": {"value": args.a, "label": args.a_label},
            "b": {"value": args.b, "label": args.b_label},
            "unit": args.unit,
            "difference": _round(difference),
            "difference_unit": args.unit,
            "percent_difference": None if args.b == 0 else _round(100 * difference / abs(args.b)),
            "ratio": None if args.b == 0 else _round(args.a / args.b),
            "sum": _round(args.a + args.b),
        }

    def _compute_stats(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = StatsInput.model_validate(raw)
        response = self._ref(args.ref)

        if args.operation is Operation.CAPTURE_PRICE:
            if response.endpoint != "price":
                raise ToolInputError("capture_price needs a price ref as `ref`.")
            if args.weight_ref is None or args.weight_source is None:
                raise ToolInputError("capture_price needs weight_ref and weight_source.")
            weights = self.series_for(self._ref(args.weight_ref), args.weight_source)
            series = response.get_series("day_ahead_price")
            return _result_dict(compute_stats(series, Operation.CAPTURE_PRICE, weights=weights))

        series = self.series_for(response, args.source)

        if args.operation is Operation.CAPACITY_FACTOR:
            return _result_dict(self._capacity_factor(response, series, args.source))

        if args.operation is Operation.SHARE:
            if args.per_day:
                raise ToolInputError("per_day is not supported for share.")
            if response.endpoint not in ("public_power", "total_power"):
                raise ToolInputError("share is only defined for national generation refs.")
            total = source_series(response, args.relative_to or "total")
            return _result_dict(compute_stats(series, Operation.SHARE, total=total))

        if args.per_day:
            return {"per_day": [_result_dict(r) for r in daily(series, args.operation)]}
        return _result_dict(compute_stats(series, args.operation, threshold=args.threshold))

    def series_for(self, response: TimeSeriesResponse, source: str | None) -> Series:
        """The series a stats call means, for any kind of ref."""
        if response.endpoint == "price":
            if source not in (None, "day_ahead_price"):
                raise ToolInputError("Price refs have no sources: set source to null.")
            return response.get_series("day_ahead_price")
        if source is None:
            raise ToolInputError("source is required for this ref.")
        if response.endpoint in ("public_power", "total_power"):
            return source_series(response, source)
        if source not in response.series_ids():
            raise ToolInputError(
                f"'{source}' is not in this ref. Available: {', '.join(response.series_ids())}."
            )
        return response.get_series(source)

    def _capacity_factor(
        self, response: TimeSeriesResponse, series: Series, source: str | None
    ) -> StatsResult:
        if response.endpoint not in ("public_power", "total_power", "smard"):
            raise ToolInputError("capacity_factor needs a generation ref.")
        if response.endpoint == "smard" and response.attributes.get("region") != "DE":
            raise ToolInputError("capacity_factor needs national (DE) generation data.")
        if source not in CAPACITY_SERIES:
            raise ToolInputError(f"capacity_factor supports: {', '.join(CAPACITY_SERIES)}.")
        if series.start is None:
            raise NoDataError("No generation data in this ref.")
        # Capacity at the start of the period = value at the end of the previous month.
        previous_month = series.start.replace(day=1) - timedelta(days=1)
        monthly = self.data.installed_power("monthly")
        wanted_month = (previous_month.year, previous_month.month)
        row = None
        for candidate in monthly.data:
            if (candidate.timestamp.year, candidate.timestamp.month) == wanted_month:
                row = candidate
        values = [row.values.get(c) if row else None for c in CAPACITY_SERIES[source]]
        if row is None or any(v is None for v in values):
            raise NoDataError(
                f"No installed capacity for {source} at end of {previous_month:%Y-%m}."
            )
        capacity_mw = sum(values) * 1000  # GW -> MW
        kind = " (AC, inverter capacity)" if source == "solar" else ""
        note = (
            f"{sum(values):.3f} GW installed at end of {previous_month:%Y-%m}{kind}, Energy-Charts"
        )
        return capacity_factor(series, capacity_mw, note)

    # ---------- helpers ----------

    def _ref(self, ref: str) -> TimeSeriesResponse:
        if ref not in self.store:
            known = ", ".join(self.store) or "none yet"
            raise ToolInputError(f"Unknown ref '{ref}'. Known refs: {known}.")
        return self.store[ref]

    def _check_period(self, start: date, end: date, latest: date) -> None:
        if start > end:
            raise ToolInputError(f"start ({start}) is after end ({end}).")
        if (end - start).days + 1 > MAX_DAYS:
            raise ToolInputError(f"Period too long: max {MAX_DAYS} days per call.")
        if start > latest:
            raise NoDataError(
                f"{start} is in the future (today in Germany is {self.today()}). "
                "Only historical data is available."
            )

    def _save(self, tool_name: str, response: TimeSeriesResponse) -> str:
        self._counter += 1
        ref = f"{REF_PREFIX[tool_name]}_{self._counter}"
        self.store[ref] = response
        return ref

    @staticmethod
    def _summary(ref: str, response: TimeSeriesResponse, start: date, end: date) -> dict[str, Any]:
        rows = response.data
        return {
            "ref": ref,
            "requested_period": f"{start} to {end}",
            "data_from": rows[0].timestamp.isoformat() if rows else None,
            "data_until": rows[-1].timestamp.isoformat() if rows else None,
            "n_intervals": len(rows),
            "interval_minutes": response.interval_minutes,
            # e.g. 2,980 quarter-hours = 745 hours (October with the extra DST hour)
            "hours_covered": len(rows) * response.interval_minutes / 60
            if response.interval_minutes
            else None,
            "unit": response.unit,
            "timezone": response.timezone,
            "data_source": DATA_SOURCE.get(response.endpoint, ENERGY_CHARTS),
            "license": response.license,
        }


def _battery_response(prices: TimeSeriesResponse, run: BatteryRun) -> TimeSeriesResponse:
    """The schedule as a time series, so compute_stats and the charts can use it."""
    rows = [
        DataRow(timestamp=t, values={"battery_power": round(e / run.interval_h, 3),
                                     "stored_energy": round(s, 3), "price": p})
        for t, e, s, p in zip(run.timestamps, run.grid_mwh, run.stored, run.prices, strict=True)
    ]  # fmt: skip
    return prices.model_copy(update={
        "endpoint": "battery",
        "unit": "MW",
        "attributes": {"sign_convention": "battery_power: + charging, - discharging"},
        "series": [SeriesInfo(id="battery_power", name="Battery power (grid side)", unit="MW"),
                   SeriesInfo(id="stored_energy", name="Stored energy", unit="MWh"),
                   SeriesInfo(id="price", name="Day-ahead price", unit="EUR / MWh")],
        "data": rows,
    })  # fmt: skip


def _check_source(source: str) -> None:
    """Raise a helpful error for an unknown source name."""
    if source not in SOURCE_GROUPS:
        raise ToolInputError(
            f"Unknown source '{source}'. Choose from: {', '.join(available_sources())}."
        )


def _spread_meaning(response: TimeSeriesResponse, condition: "ConditionInput") -> str:
    """Spread conditions in words: the sign of 'Germany minus neighbour' is easy to misread."""
    zone = response.attributes.get("zone", "the neighbour").split(" ")[0]
    if condition.above is not None and condition.above >= 0 and condition.below is None:
        return f"Germany dearer than {zone}"
    if condition.below is not None and condition.below <= 0 and condition.above is None:
        return f"Germany cheaper than {zone}"
    return f"spread = Germany minus {zone}"


def _round(value: float) -> float:
    """4 significant digits, at least 2 decimals: small differences stay visible."""
    if value == 0:
        return 0.0
    return round(value, max(2, 3 - math.floor(math.log10(abs(value)))))


def _result_dict(result: StatsResult) -> dict[str, Any]:
    """Compact JSON for the model: rounded values, local timestamps, no noise."""
    out: dict[str, Any] = {
        "operation": result.operation.value,
        "series": result.series_id,
        "period": f"{result.start} to {result.end}",
        "value": None if result.value is None else round(result.value, 2),
        "unit": result.unit,
        "n_values": result.n_points,
        "n_missing": result.n_missing,
    }
    if result.timestamp is not None:
        out["at"] = result.timestamp.isoformat()
    if result.value is not None:
        if result.unit == "MWh":
            out["value_gwh"] = round(result.value / 1000, 2)
        elif result.unit == "GWh":
            out["value_twh"] = round(result.value / 1000, 3)
        elif result.unit == "EUR / MWh":  # every price also in ct/kWh (1 ct/kWh = 10 EUR/MWh)
            out["value_ct_per_kwh"] = round(result.value / 10, 3)
    if result.detail:
        out["detail"] = result.detail
    return out


def _short(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
