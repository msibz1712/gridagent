"""Agent configuration loaded from `agents/<name>/agent.yaml`.

M2 keeps this minimal; M3 adds stricter validation, friendly error
messages and the registry commands (`garden list`, `garden new`).
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str  # e.g. claude-sonnet-5-5; never hard-coded in the code
    max_tokens: int = Field(16000, gt=0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    refusal_fallback: bool = True


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(8, gt=0)  # model calls per run
    model_timeout_seconds: float = Field(60, gt=0)  # per model call
    model_max_retries: int = Field(2, ge=0)  # SDK retries on 429/5xx/network
    run_timeout_seconds: float = Field(180, gt=0)  # whole run


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    owner: str
    model: ModelConfig
    tools: list[str]
    limits: LimitsConfig = LimitsConfig()
    system_prompt_file: str = "system_prompt.md"


class LoadedAgent(BaseModel):
    """A config plus the files it points to."""

    config: AgentConfig
    system_prompt: str
    directory: Path


def load_agent(agents_dir: Path, name: str) -> LoadedAgent:
    """Read and validate `agents/<name>/agent.yaml` and its system prompt."""
    directory = agents_dir / name
    config_path = directory / "agent.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"No agent '{name}': {config_path} does not exist.")
    config = AgentConfig.model_validate(yaml.safe_load(config_path.read_text()))
    prompt_path = directory / config.system_prompt_file
    return LoadedAgent(config=config, system_prompt=prompt_path.read_text(), directory=directory)
