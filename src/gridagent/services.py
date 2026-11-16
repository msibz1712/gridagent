"""Application services shared by the CLI and the web app.

Keeping this logic here (not in the web layer) means it is written once and
tested on its own; the web API only translates HTTP to these functions.

The "presentation" helpers at the bottom turn an agent run into what an end
user needs (answer, period, sources, plain-language method, charts) without
any agent or model vocabulary.
"""

import importlib.util
import json
import math
import os
import threading
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from gridagent.agent.config import AgentConfig, LoadedAgent
from gridagent.agent.conversation import Conversation
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog, ToolCallLog, append_run_log
from gridagent.data.client import EnergyChartsError
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.data.models import Series
from gridagent.data.provider import DataProvider, LiveDataSource
from gridagent.evals.runner import percentile
from gridagent.llm.claude import ClaudeLLM
from gridagent.tools.definitions import NEIGHBOUR_ZONES, REF_PREFIX
from gridagent.tools.toolbox import ToolBox, ToolInputError


def build_llm(config: AgentConfig, model: str | None = None) -> ClaudeLLM:
    """The real Claude model, configured from agent.yaml (model can be overridden)."""
    return ClaudeLLM(
        model_name=model or config.model.name,
        max_tokens=config.model.max_tokens,
        effort=config.model.effort,
        refusal_fallback=config.model.refusal_fallback,
        timeout_seconds=config.limits.model_timeout_seconds,
        max_retries=config.limits.model_max_retries,
    )


def reader_database_url() -> str | None:
    """The database the app reads from: the read-only role if configured, else the owner.
    None (live APIs only) if neither is set or the optional `db` extra is not installed."""
    url = os.environ.get("DATABASE_URL_READER") or os.environ.get("DATABASE_URL")
    if not url or importlib.util.find_spec("psycopg") is None:
        return None
    return url


def build_data(
    use_fixtures: bool, fixture_dir: Path, cache_dir: Path, database_url: str | None = None
) -> DataProvider:
    """Recorded fixtures (offline, reproducible), or live Energy-Charts + SMARD (cached),
    with the synced database in front of them when `database_url` is given."""
    if use_fixtures:
        return FixtureDataSource(fixture_dir)
    live = LiveDataSource(cache_dir)
    if database_url is None:
        return live
    from gridagent.db.source import DatabaseDataSource, HybridDataSource  # optional `db` extra

    return HybridDataSource(DatabaseDataSource(database_url, cache_dir / "database"), live)


def ask(
    agent: LoadedAgent,
    question: str,
    model: str | None,
    data: DataProvider,
    today: date,
    log_path: Path | None,
    conversation: Conversation | None = None,
) -> RunLog:
    """Run one question (optionally as a follow-up) with the real model and log the run."""
    runner = Agent(agent, build_llm(agent.config, model), data, today=lambda: today)
    log = runner.run(question, conversation)
    if log_path is not None:
        append_run_log(log_path, log)
    return log


# ---------- run history (monitoring, used by `garden runs`) ----------


def load_runs(log_path: Path, limit: int = 200) -> list[RunLog]:
    """The most recent runs, newest first. Unreadable lines are skipped."""
    if not log_path.is_file():
        return []
    runs = []
    for line in log_path.read_text().splitlines()[-limit:]:
        try:
            runs.append(RunLog.model_validate_json(line))
        except ValueError:
            continue
    return list(reversed(runs))


class RunSummary(BaseModel):
    n_runs: int
    ok_rate: float | None
    error_rate: float | None
    latency_p50_s: float | None
    latency_p95_s: float | None
    total_cost_usd: float
    # Share of checked answers containing a number no tool result backs (None: none checked)
    unsupported_rate: float | None = None


def summarize_runs(runs: list[RunLog]) -> RunSummary:
    n = len(runs)
    latencies = sorted(r.latency_s for r in runs)
    failed = sum(r.status in ("error", "timeout", "max_steps") for r in runs)
    checked = [r for r in runs if r.unsupported_numbers is not None]
    return RunSummary(
        n_runs=n,
        ok_rate=sum(r.status == "ok" for r in runs) / n if n else None,
        error_rate=failed / n if n else None,
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        total_cost_usd=round(sum(r.cost_usd or 0 for r in runs), 6),
        unsupported_rate=sum(bool(r.unsupported_numbers) for r in checked) / len(checked)
        if checked
        else None,
    )


# ---------- spending guard ----------


class Budget:
    """A spending limit for live model calls (per web-server process).

    A lock makes it safe when several requests run at the same time.
    """

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self._lock = threading.Lock()

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd, 0.0)

    def allows_another_call(self, worst_case_usd: float) -> bool:
        with self._lock:
            return self.spent_usd + worst_case_usd <= self.limit_usd

    def add(self, cost_usd: float | None) -> None:
        with self._lock:
            self.spent_usd += cost_usd or 0.0


# ---------- presentation: plain language for end users ----------

SOURCE_LABELS = {
    "wind_speed": "wind speed (DWD stations, ~10 m above ground)",
    "solar_radiation": "solar radiation (DWD stations)",
    "solar": "solar",
    "wind": "wind (onshore + offshore)",
    "wind_onshore": "onshore wind",
    "wind_offshore": "offshore wind",
    "biomass": "biomass",
    "hydro": "hydropower",
    "pumped_storage": "pumped storage",
    "geothermal": "geothermal",
    "lignite": "lignite",
    "hard_coal": "hard coal",
    "gas": "natural gas",
    "oil": "oil",
    "fossil": "fossil fuels",
    "renewable": "renewables",
    "waste": "waste",
    "nuclear": "nuclear power",
    "others": "other sources",
    "other_renewable": "other renewables",
    "other_conventional": "other conventional sources",
    "load": "electricity demand (load)",
    "residual_load": "residual load",
    "total": "total public net generation",
    "sum": "all borders combined",
}
GENERATION_SOURCE = {
    "name": "Energy-Charts.info (Fraunhofer ISE)",
    "url": "https://energy-charts.info",
    "license": "CC BY 4.0",
}
PRICE_SOURCE = {
    "name": "Bundesnetzagentur | SMARD.de, via Energy-Charts.info",
    "url": "https://www.smard.de",
    "license": "CC BY 4.0",
}
SMARD_SOURCE = {
    "name": "SMARD.de (Bundesnetzagentur), control-area data",
    "url": "https://www.smard.de",
    "license": "CC BY 4.0",
}
REDISPATCH_SOURCE = {
    "name": "netztransparenz.de (the four German TSOs), redispatch measures",
    "url": "https://www.netztransparenz.de",
    "license": "© German TSOs; not openly licensed, used for own analysis",
}
MASTR_SOURCE = {
    "name": "Marktstammdatenregister (Bundesnetzagentur), power plant register",
    "url": "https://www.marktstammdatenregister.de",
    "license": "dl-de/by-2-0",
}
OPEN_METEO_SOURCE = {
    "name": "Open-Meteo.com, weather forecasts",
    "url": "https://open-meteo.com",
    "license": "CC BY 4.0",
}
WEATHER_SOURCE = {
    "name": "Deutscher Wetterdienst (DWD), station observations",
    "url": "https://opendata.dwd.de",
    "license": "CC BY 4.0",
}
OPERATION_WORDS = {
    "count_below": "Counted the hours below",
    "count_above": "Counted the hours above",
}


COUNTRY_NAMES = {
    "austria": "Austria", "belgium": "Belgium", "czech_republic": "the Czech Republic",
    "denmark": "Denmark", "france": "France", "luxembourg": "Luxembourg",
    "netherlands": "the Netherlands", "norway": "Norway", "poland": "Poland",
    "sweden": "Sweden", "switzerland": "Switzerland",
}  # fmt: skip


def source_label(source: str | None) -> str:
    """Readable name for a series id: 'wind_onshore' -> 'onshore wind', 'france' -> 'France'."""
    if source in COUNTRY_NAMES:
        return COUNTRY_NAMES[source]
    return SOURCE_LABELS.get(source or "", (source or "").replace("_", " "))


def format_period(start: str, end: str) -> str:
    """'2026-11-02', '2026-11-08' -> '2 Nov 2026 – 8 Nov 2026' (or one date)."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    first = f"{a.day} {a:%b %Y}"
    return first if a == b else f"{first} – {b.day} {b:%b %Y}"


def _fetch_step(call: ToolCallLog) -> str:
    args = call.input
    period = format_period(args.get("start", ""), args.get("end", ""))
    if call.name == "get_generation":
        what = "electricity generation and demand data for Germany" + (
            " including industrial self-supply" if args.get("scope") == "total" else ""
        )
    elif call.name == "get_day_ahead_price":
        what = "day-ahead electricity prices (Germany–Luxembourg)"
    elif call.name == "get_redispatch":
        what = "congestion-management measures (redispatch, countertrading) of the German TSOs"
        if call.is_error:
            return f"Looked for {what} for {period}: no data available"
        return f"Retrieved {what} for {period}"
    elif call.name in ("get_neighbour_price", "get_price_spread"):
        zone = NEIGHBOUR_ZONES.get(args.get("zone", ""), args.get("zone", "a neighbour"))
        what = (f"day-ahead electricity prices for {zone}" if call.name == "get_neighbour_price"
                else f"the day-ahead price difference between Germany and {zone}")  # fmt: skip
        if call.is_error:
            return f"Looked for {what} for {period}: no data available"
        return f"Retrieved {what} for {period}"
    elif call.name == "get_weather":
        what = "hourly wind speed and solar radiation for Germany (65 DWD stations)"
        if call.is_error:
            return f"Looked for {what} for {period}: no data available"
        return f"Retrieved {what} for {period}"
    elif call.name == "get_cross_border_flows":
        kind = "physical" if args.get("kind") == "physical" else "scheduled commercial"
        what = f"{kind} cross-border electricity flows between Germany and its neighbours"
    else:  # get_regional_generation
        sources = ", ".join(source_label(s) for s in args.get("sources", []))
        what = f"{sources} data for the {args.get('region')} control area"
    if call.is_error:
        return f"Looked for {what} for {period}: no data available"
    return f"Retrieved {what} in 15-minute resolution for {period}"  # fmt: skip


def _stats_step(call: ToolCallLog, where: str = "") -> str:
    """`where` names the data a ref came from, e.g. ' in the TenneT control area'."""
    args = call.input
    subject = source_label(args.get("source")) if args.get("source") else "the price"
    per_day = " for each day" if args.get("per_day") else ""
    operation = args.get("operation")
    if operation == "share":
        whole = source_label(args.get("relative_to") or "total")
        return f"Calculated the share of {subject} in {whole}"
    if operation == "energy":
        if args.get("source") in COUNTRY_NAMES or args.get("source") == "sum":
            return f"Calculated the net energy exchanged with {subject}{per_day}"
        return f"Calculated the total energy from {subject}{where}{per_day}"
    if operation == "mean":
        return f"Calculated the average of {subject}{per_day}"
    if operation in ("max", "min"):
        word = "highest" if operation == "max" else "lowest"
        return f"Found the {word} value of {subject}{per_day} and when it occurred"
    if operation in OPERATION_WORDS:
        return f"{OPERATION_WORDS[operation]} {args.get('threshold')} for {subject}"
    if operation == "capture_price":
        weighted = source_label(args.get("weight_source"))
        return f"Calculated the price weighted by {weighted} generation (market value)"
    if operation == "capacity_factor":
        return (f"Calculated the capacity factor of {subject} from generation and the "
                "installed capacity at the start of the period")  # fmt: skip
    return f"Calculated the {operation} of {subject}{per_day}"


def method_step(call: ToolCallLog, where: str = "") -> str:
    """Describe one tool call the way a methods section would."""
    if call.name == "get_installed_capacity":
        text = f"Looked up installed generation capacity ({call.input.get('time_step')} data)"
    elif call.name == "get_reference_statistics":
        text = _reference_step(call)
    elif call.name == "compute_stats":
        text = _stats_step(call, where)
    elif call.name == "compute_when":
        text = _when_step(call)
    elif call.name == "compare_values":
        a, b = call.input.get("a_label", "the first value"), call.input.get("b_label", "the second")
        text = f"Compared {a} with {b} (difference, percentage difference and ratio)"
    elif call.name == "simulate_battery":
        args = call.input
        efficiency = args.get("round_trip_efficiency") or 0.9
        text = (f"Optimised a {args.get('power_mw'):g} MW / {args.get('capacity_mwh'):g} MWh "
                f"battery day by day on these prices ({efficiency:.0%} round-trip efficiency, "
                "day-ahead market only)")  # fmt: skip
    elif call.name == "get_power_forecast":
        period = format_period(call.input.get("start", ""), call.input.get("end", ""))
        text = (f"Retrieved the grid operators' day-ahead forecasts of wind, solar and demand "
                f"for {period}")  # fmt: skip
    elif call.name == "get_weather_outlook":
        text = ("Retrieved the 7-day outlook of wind, solar and temperature (weather forecasts "
                "calibrated on actual output over the last 60 days)")  # fmt: skip
    elif call.name == "get_notable_events":
        day = format_period(call.input.get("date", ""), call.input.get("date", ""))
        text = (f"Checked {day} for unusual prices, records, streaks and forecast misses "
                "against the four weeks and two years before")  # fmt: skip
    elif call.name == "investigate_price_event":
        args = call.input
        day = format_period(args.get("date", ""), args.get("date", ""))
        text = (f"Compared the price on {day}, "
                f"{args.get('from_hour', 0):02d}:00–{args.get('to_hour', 0):02d}:00, with the same "
                "hours on comparable days of the 4 weeks before, and estimated the price residual "
                "load would normally give")  # fmt: skip
    elif call.name == "get_plant_register":
        text = _register_step(call.input)
    elif call.name == "plan_flexible_load":
        args = call.input
        text = (f"Chose the cheapest intervals for {args.get('energy_mwh'):g} MWh at up to "
                f"{args.get('max_power_mw'):g} MW and compared with running evenly")  # fmt: skip
    else:
        return _fetch_step(call)
    return text + (" (failed)" if call.is_error else "")


def _register_step(args: dict) -> str:
    area = args.get("area") or "Germany"
    what = source_label(args.get("technology")) if args.get("technology") else "power plants"
    first, last = args.get("from_year"), args.get("to_year")
    years = f", {first}" if first == last else f", {first}–{last}"
    split = f", by {args['group_by']}" if args.get("group_by") not in (None, "none") else ""
    return f"Looked up {what} capacity in {area} in the power plant register{years}{split}"


def _reference_step(call: ToolCallLog) -> str:
    try:
        title = json.loads(call.result)["title"]
    except (ValueError, KeyError, TypeError):
        title = "an official statistic"
    period = call.input.get("period")
    return f"Looked up the official figure: {title}" + (f" ({period})" if period else "")


def _when_step(call: ToolCallLog) -> str:
    args = call.input
    rules = []
    for c in args.get("conditions", []):
        name = source_label(c.get("source")) if c.get("source") else "the price"
        bounds = [f"above {c['above']:g}" if c.get("above") is not None else "",
                  f"below {c['below']:g}" if c.get("below") is not None else ""]  # fmt: skip
        rules.append(f"{name} {' and '.join(b for b in bounds if b)}")
    selected = "the hours in which " + " and ".join(rules)
    if args.get("operation") == "hours":
        return f"Counted {selected}"
    subject = source_label(args.get("source")) if args.get("source") else "the price"
    word = {"mean": "average", "min": "lowest", "max": "highest", "energy": "total energy"}
    return f"Calculated the {word.get(args.get('operation'), args.get('operation'))} of "\
           f"{subject} in {selected}"  # fmt: skip


def method_steps(log: RunLog, toolbox: ToolBox | None = None) -> list[str]:
    """All steps; calculations on regional data name their control area.

    In a conversation, pass its ToolBox: a follow-up may calculate on data that
    was fetched in an earlier turn and so does not appear in this run's log.
    """
    region_of_ref: dict[str, str] = {}
    if toolbox is not None:
        for ref, response in toolbox.store.items():
            if response.endpoint == "smard":
                region_of_ref[ref] = response.attributes.get("region", "")
    fetches = 0
    steps = []
    for call in log.tool_calls:
        if call.name in REF_PREFIX and not call.is_error and toolbox is None:
            fetches += 1  # the ToolBox numbers refs in this order (gen_1, region_2, ...)
            if call.name == "get_regional_generation":
                region_of_ref[f"region_{fetches}"] = call.input.get("region", "")
        where = ""
        if call.name == "compute_stats" and call.input.get("ref") in region_of_ref:
            region = region_of_ref[call.input["ref"]]
            where = " for Germany" if region == "DE" else f" in the {region} control area"
        steps.append(method_step(call, where))
    return steps


def sources_for_run(log: RunLog) -> list[dict[str, str]]:
    """The data sources an answer is based on (only for successful data fetches)."""
    ok = [c for c in log.tool_calls if not c.is_error]
    names = {c.name for c in ok}
    sources = []
    if names & {"get_generation", "get_cross_border_flows", "get_installed_capacity"}:
        sources.append(GENERATION_SOURCE)
    if names & {"get_day_ahead_price", "get_neighbour_price", "get_price_spread"}:
        sources.append(PRICE_SOURCE)
    if "get_regional_generation" in names:
        sources.append(SMARD_SOURCE)
    if "get_weather" in names:
        sources.append(WEATHER_SOURCE)
    if "get_redispatch" in names:
        sources.append(REDISPATCH_SOURCE)
    if "get_weather_outlook" in names:
        sources.append(OPEN_METEO_SOURCE)
    if names & {"get_power_forecast", "get_notable_events"} and GENERATION_SOURCE not in sources:
        sources.append(GENERATION_SOURCE)
    if "get_notable_events" in names and PRICE_SOURCE not in sources:
        sources.append(PRICE_SOURCE)
    if "investigate_price_event" in names:
        for extra in (PRICE_SOURCE, GENERATION_SOURCE, WEATHER_SOURCE):
            if extra not in sources:
                sources.append(extra)
    if "get_plant_register" in names:
        sources.append(MASTR_SOURCE)
    for call in ok:
        if call.name != "get_reference_statistics":
            continue
        try:
            source = json.loads(call.result)["source"]
        except (ValueError, KeyError, TypeError):
            continue
        entry = {"name": source["name"], "url": source["url"],
                 "license": f"published {source['published']}"}  # fmt: skip
        if entry not in sources:
            sources.append(entry)
    return sources


# ---------- charts of the data behind an answer ----------

MAX_CHART_POINTS = 400
MAX_CHARTS = 4


class ChartPoint(BaseModel):
    t: str  # ISO timestamp (start of the interval, German local time)
    v: float | None


class Chart(BaseModel):
    title: str
    unit: str
    period: str
    resolution_minutes: int | None  # after averaging; for labels and CSV
    points: list[ChartPoint]


def downsample(series: Series, max_points: int = MAX_CHART_POINTS) -> list[ChartPoint]:
    """Average consecutive values so a month of 15-minute data fits in a chart."""
    size = bucket_size(series, max_points)
    points = []
    for i in range(0, len(series.points), size):
        bucket = series.points[i : i + size]
        values = [p.value for p in bucket if p.value is not None]
        mean = sum(values) / len(values) if values else None
        points.append(ChartPoint(t=bucket[0].timestamp.isoformat(), v=mean))
    return points


def bucket_size(series: Series, max_points: int = MAX_CHART_POINTS) -> int:
    return max(1, math.ceil(len(series.points) / max_points))


def _chart_title(
    response_endpoint: str, source: str, region: str | None, zone: str | None = None
) -> str:
    if response_endpoint == "price":
        name = "Germany–Luxembourg" if zone in (None, "DE-LU") else NEIGHBOUR_ZONES.get(zone, zone)
        return f"Day-ahead electricity price, {name}"
    if response_endpoint == "price_spread":
        titles = {
            "germany": "Day-ahead price, Germany",
            "neighbour": "Day-ahead price, neighbour",
            "spread": "Price difference: Germany minus neighbour",
        }
        return titles.get(source, source)
    if response_endpoint == "redispatch":
        titles = {
            "reduce_total": "Redispatch: generation reduced",
            "increase_total": "Redispatch: generation increased",
            "reduce_renewable": "Redispatch: renewable generation curtailed",
            "reduce_conventional": "Redispatch: conventional generation reduced",
            "increase_conventional": "Redispatch: conventional generation increased",
            "increase_other": "Redispatch: increases via the exchange and other",
        }
        return titles.get(source, source) + " (MW, measures spread evenly)"
    if response_endpoint == "event_window":
        titles = {
            "price": "Day-ahead price, hourly (event and the 4 weeks before)",
            "residual_load": "Residual load (demand minus wind and solar), hourly",
            "load": "Demand, hourly",
            "wind": "Wind generation, hourly",
            "solar": "Solar generation, hourly",
        }
        return titles.get(source, source)
    if response_endpoint == "battery":
        titles = {
            "battery_power": "Battery: charging (+) and discharging (-), MW",
            "stored_energy": "Battery: stored energy, MWh",
            "price": "Day-ahead price used by the battery",
        }
        return titles.get(source, source)
    if response_endpoint == "weather":
        if source == "wind_speed":
            return "Mean wind speed, Germany (DWD, ~10 m)"
        return "Mean solar radiation, Germany (DWD)"
    if response_endpoint in ("cbpf", "cbet"):
        kind = "physical" if response_endpoint == "cbpf" else "commercial"
        return f"Net {kind} imports: {source_label(source)} (negative = export)"
    label = source_label(source)
    title = label[0].upper() + label[1:]
    if source not in ("load", "total", "residual_load"):
        title += " generation"
    return f"{title}, {region} control area" if region else title


def charts_for_run(log: RunLog, data: DataProvider, toolbox: ToolBox | None = None) -> list[Chart]:
    """Re-create the series the agent analysed, for plotting next to the answer.

    With a conversation's ToolBox, its stored data is used directly (follow-ups
    may use refs from earlier turns). Otherwise the logged data calls are
    replayed in a fresh ToolBox (served from cache), which reproduces exactly
    the same refs.
    """
    replay = toolbox or ToolBox(data, today=lambda: date.fromisoformat(log.today))
    wanted: list[tuple[str, str | None]] = []
    for call in log.tool_calls:
        if call.is_error:
            continue
        if call.name in REF_PREFIX:
            if toolbox is None:
                replay.execute(call.name, call.input)
        if call.name == "investigate_price_event":
            try:
                ref = json.loads(call.result)["ref"]
            except (ValueError, KeyError, TypeError):
                ref = None
            if ref:
                wanted += [p for p in [(ref, "price"), (ref, "residual_load")] if p not in wanted]
        if call.name in ("simulate_battery", "plan_flexible_load"):  # simulate also stores a ref
            pairs = [(call.input.get("ref", ""), None)]
            if call.name == "simulate_battery":
                try:
                    pairs.append((json.loads(call.result)["ref"], "stored_energy"))
                except (ValueError, KeyError, TypeError):
                    pass
            wanted += [p for p in pairs if p not in wanted]
        elif call.name == "compute_when":
            inputs = call.input
            pairs = [(c.get("ref", ""), c.get("source")) for c in inputs.get("conditions", [])]
            if inputs.get("ref"):
                pairs.append((inputs["ref"], inputs.get("source")))
            wanted += [p for p in pairs if p not in wanted]
        elif call.name == "compute_stats":
            inputs = call.input
            if inputs.get("operation") == "capture_price" and inputs.get("weight_ref"):
                pairs = [(inputs.get("ref", ""), None),
                         (inputs["weight_ref"], inputs.get("weight_source"))]  # fmt: skip
            else:
                pairs = [(inputs.get("ref", ""), inputs.get("source"))]
            wanted += [p for p in pairs if p not in wanted]

    charts = []
    for ref, source in wanted[:MAX_CHARTS]:
        response = replay.store.get(ref)
        if response is None:
            continue
        try:
            series = replay.series_for(response, source)
        except (ToolInputError, EnergyChartsError, KeyError, ValueError):
            continue  # a chart is a nice-to-have; never fail the answer for it
        if not series.points:
            continue
        minutes = series.interval_minutes * bucket_size(series) if series.interval_minutes else None
        charts.append(
            Chart(
                title=_chart_title(
                    response.endpoint,
                    source or "",
                    response.attributes.get("region"),
                    response.bidding_zone,
                ),
                unit=series.unit,
                period=format_period(series.start.isoformat(), series.end.isoformat()),
                resolution_minutes=minutes,
                points=downsample(series),
            )
        )
    return charts
