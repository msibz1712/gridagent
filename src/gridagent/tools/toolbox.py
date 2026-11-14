"""Runs the agent's tools and keeps fetched data for the duration of one run.

Design choice: data stays here, not in the conversation. A week of
generation data is 672 rows x 21 series; sending it to the model would
cost thousands of tokens per question, and the model could copy numbers
wrongly. So `get_*` tools store the data under a short `ref` (e.g.
"gen_1") and return only a summary. `compute_stats` takes that ref.

Every tool returns a `ToolResult`. Errors (bad input, no data, API down)
come back as `is_error=True` with a readable message, so the model can
correct itself or tell the user, and the run never crashes on a tool.
"""

import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from gridagent.data.client import EnergyChartsError, NoDataError
from gridagent.data.fixture_source import DataProvider
from gridagent.data.models import TimeSeriesResponse
from gridagent.data.sources import SOURCE_GROUPS, available_sources, source_series
from gridagent.tools.definitions import ALL_TOOLS
from gridagent.tools.stats import Operation, StatsResult, compute_stats, daily

MAX_DAYS = 93
SOURCE_NOTE = "Energy-Charts.info (Fraunhofer ISE)"


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


class StatsInput(_Input):
    ref: str
    operation: Operation
    source: str | None = None
    relative_to: str | None = None
    per_day: bool = False


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

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions for the API request, in a fixed order (stable for caching)."""
        return [ALL_TOOLS[name] for name in self.enabled]

    def execute(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        handlers = {
            "get_generation": self._get_generation,
            "get_day_ahead_price": self._get_day_ahead_price,
            "compute_stats": self._compute_stats,
        }
        if name not in self.enabled:
            return ToolResult(content=f"Unknown tool '{name}'.", is_error=True)
        try:
            return ToolResult(content=json.dumps(handlers[name](tool_input)))
        except ValidationError as exc:
            return ToolResult(content=f"Invalid input: {_short(exc)}", is_error=True)
        except (ToolInputError, KeyError, ValueError) as exc:
            return ToolResult(content=str(exc).strip("'\""), is_error=True)
        except NoDataError as exc:
            return ToolResult(content=f"No data available: {exc}", is_error=True)
        except EnergyChartsError as exc:
            return ToolResult(content=f"Data source unavailable: {exc}", is_error=True)

    # ---------- tools ----------

    def _get_generation(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = GenerationInput.model_validate(raw)
        self._check_period(args.start, args.end, latest=self.today())
        if args.sources:
            for source in args.sources:
                _check_source(source)
        response = self.data.public_power(args.start, args.end)
        ref = self._save("gen", response)
        return self._summary(ref, response, args.start, args.end) | {
            "sources": args.sources or "all",
        }

    def _get_day_ahead_price(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = PeriodInput.model_validate(raw)
        # Day-ahead prices for tomorrow exist once the auction has run (around midday).
        self._check_period(args.start, args.end, latest=self.today() + timedelta(days=1))
        response = self.data.day_ahead_price(args.start, args.end)
        ref = self._save("price", response)
        summary = self._summary(ref, response, args.start, args.end)
        summary["data_source"] = f"{SOURCE_NOTE}; prices: Bundesnetzagentur | SMARD.de"
        return summary

    def _compute_stats(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = StatsInput.model_validate(raw)
        if args.ref not in self.store:
            known = ", ".join(self.store) or "none yet"
            raise ToolInputError(f"Unknown ref '{args.ref}'. Known refs: {known}.")
        response = self.store[args.ref]

        if response.endpoint == "price":
            if args.source not in (None, "day_ahead_price"):
                raise ToolInputError("Price refs have no sources: set source to null.")
            series = response.get_series("day_ahead_price")
        else:
            if args.source is None:
                raise ToolInputError("source is required for generation refs.")
            series = source_series(response, args.source)

        if args.operation is Operation.SHARE:
            if args.per_day:
                raise ToolInputError("per_day is not supported for share.")
            if response.endpoint == "price":
                raise ToolInputError("share is only defined for generation data.")
            total = source_series(response, args.relative_to or "total")
            return _result_dict(compute_stats(series, Operation.SHARE, total=total))

        if args.per_day:
            return {"per_day": [_result_dict(r) for r in daily(series, args.operation)]}
        return _result_dict(compute_stats(series, args.operation))

    # ---------- helpers ----------

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

    def _save(self, prefix: str, response: TimeSeriesResponse) -> str:
        self._counter += 1
        ref = f"{prefix}_{self._counter}"
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
            "unit": response.unit,
            "timezone": response.timezone,
            "data_source": SOURCE_NOTE,
            "license": response.license,
        }


def _check_source(source: str) -> None:
    """Raise a helpful error for an unknown source name."""
    if source not in SOURCE_GROUPS:
        raise ToolInputError(
            f"Unknown source '{source}'. Choose from: {', '.join(available_sources())}."
        )


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
    if result.unit == "MWh" and result.value is not None:
        out["value_gwh"] = round(result.value / 1000, 2)
    return out


def _short(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
