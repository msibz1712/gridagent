"""Official reference statistics (UBA, BDEW/AGEB, netztransparenz) for reconciliation.

These are published annual/monthly figures, not time series, so they live in a
curated, cited YAML file instead of an API client. Every entry states its
definition, source, publication date and status, so the agent can say e.g.
"our share of public net generation is X%; the official share of gross
generation is 57.9% (BDEW, preliminary)".
"""

from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

REFERENCE_FILE = Path(__file__).with_name("reference_data.yaml")
Status = Literal["final", "preliminary", "estimate", "reported"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceSource(_Strict):
    name: str
    url: str = Field(pattern=r"^https://")
    published: str


class ReferenceValue(_Strict):
    period: str = Field(pattern=r"^\d{4}(-\d{2})?$")  # "2025" or "2026-10"
    key: str | None = None  # e.g. technology for market values
    value: float
    status: Status


class ReferenceDataset(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    unit: str
    definition: str
    source: ReferenceSource
    checked: str
    values: list[ReferenceValue] = Field(min_length=1)


class ReferenceData(_Strict):
    datasets: list[ReferenceDataset]

    def get(self, dataset_id: str) -> ReferenceDataset:
        for dataset in self.datasets:
            if dataset.id == dataset_id:
                return dataset
        known = ", ".join(d.id for d in self.datasets)
        raise KeyError(f"Unknown reference dataset '{dataset_id}'. Known: {known}.")

    def ids(self) -> list[str]:
        return [d.id for d in self.datasets]


@cache
def load_reference_data(path: Path = REFERENCE_FILE) -> ReferenceData:
    """Load and validate the reference file (cached: it only changes with a release)."""
    return ReferenceData.model_validate(yaml.safe_load(path.read_text()))


def lookup(dataset_id: str, period: str | None = None) -> dict:
    """One dataset as plain data for the agent, optionally filtered to one period."""
    dataset = load_reference_data().get(dataset_id)
    values = [v for v in dataset.values if period is None or v.period == period]
    if not values:
        available = sorted({v.period for v in dataset.values})
        raise KeyError(
            f"No {dataset_id} value for {period}. Available periods: {', '.join(available)}."
        )
    return {
        "dataset": dataset.id,
        "title": dataset.title,
        "unit": dataset.unit,
        "definition": dataset.definition,
        "values": [v.model_dump(exclude_none=True) for v in values],
        "source": dataset.source.model_dump(),
    }
