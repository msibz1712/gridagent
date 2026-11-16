"""Agent configuration: `agents/<name>/<version>/agent.yaml`, validated with pydantic.

Every rule a registry needs to trust an agent is checked here, and errors
are turned into short, readable messages (file, field, problem) instead of
stack traces. `load_agent_dir` is the only way agents are loaded.
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from gridagent.tools.definitions import ALL_TOOLS

NAME_PATTERN = r"^[a-z][a-z0-9_]{1,39}$"  # snake_case, 2-40 characters
SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"  # MAJOR.MINOR.PATCH
MODEL_PATTERN = r"^claude-[a-z0-9-]+$"


class AgentConfigError(Exception):
    """An agent's files are missing or invalid. The message is user-facing."""


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=MODEL_PATTERN)  # e.g. claude-sonnet-5-5
    max_tokens: int = Field(16000, gt=0, le=128000)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    refusal_fallback: bool = True


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(8, gt=0, le=50)  # model calls per run
    model_timeout_seconds: float = Field(60, gt=0)  # per model call
    model_max_retries: int = Field(2, ge=0, le=10)  # SDK retries on 429/5xx/network
    run_timeout_seconds: float = Field(180, gt=0)  # whole run


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=10, max_length=300)
    owner: str = Field(min_length=1)
    model: ModelConfig
    # Empty for an agent that only works from its input (e.g. the news writer).
    tools: list[str] = []
    limits: LimitsConfig = LimitsConfig()
    system_prompt_file: str = "system_prompt.md"
    eval_set: str | None = None  # path relative to the agent folder, e.g. ../evals.yaml

    @field_validator("tools")
    @classmethod
    def tools_must_exist_and_be_unique(cls, tools: list[str]) -> list[str]:
        unknown = [t for t in tools if t not in ALL_TOOLS]
        if unknown:
            raise ValueError(f"unknown tool(s) {unknown}; available: {sorted(ALL_TOOLS)}")
        if len(set(tools)) != len(tools):
            raise ValueError("tools must not contain duplicates")
        return tools


class LoadedAgent(BaseModel):
    """A validated config plus the files it points to."""

    config: AgentConfig
    system_prompt: str
    directory: Path  # the version folder

    @property
    def spec(self) -> str:
        return f"{self.config.name}@{self.config.version}"

    def eval_set_path(self) -> Path | None:
        if self.config.eval_set is None:
            return None
        return (self.directory / self.config.eval_set).resolve()


def load_agent_dir(directory: Path) -> LoadedAgent:
    """Load and fully validate one agent version folder.

    Checks, in order: agent.yaml exists and is valid YAML; it matches the
    schema; name and version match the folder names; referenced files exist.
    """
    config_path = directory / "agent.yaml"
    if not config_path.is_file():
        raise AgentConfigError(f"{config_path}: file not found")

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise AgentConfigError(f"{config_path}: expected a mapping of fields")

    try:
        config = AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise AgentConfigError(f"{config_path}:\n{format_validation_error(exc)}") from exc

    problems = []
    if config.name != directory.parent.name:
        problems.append(f"name '{config.name}' must match folder '{directory.parent.name}'")
    if config.version != directory.name:
        problems.append(f"version '{config.version}' must match folder '{directory.name}'")
    prompt_path = directory / config.system_prompt_file
    if not prompt_path.is_file():
        problems.append(f"system_prompt_file '{config.system_prompt_file}' not found")
    if config.eval_set is not None and not (directory / config.eval_set).is_file():
        problems.append(f"eval_set '{config.eval_set}' not found")
    if problems:
        raise AgentConfigError(f"{config_path}:\n" + "\n".join(f"  - {p}" for p in problems))

    prompt = prompt_path.read_text()
    if not prompt.strip():
        raise AgentConfigError(f"{prompt_path}: system prompt is empty")
    return LoadedAgent(config=config, system_prompt=prompt, directory=directory)


def format_validation_error(exc: ValidationError) -> str:
    """One readable line per problem: '  - model.effort: Input should be ... (got 'hgh')'."""
    lines = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(root)"
        message = err["msg"].removeprefix("Value error, ")
        if err["type"] == "extra_forbidden":
            message = "unknown field (typo?)"
        elif err["type"] == "string_pattern_mismatch":
            message = _pattern_hint(err["ctx"]["pattern"])
        got = f" (got {err['input']!r})" if err["type"] not in ("missing",) else ""
        lines.append(f"  - {field}: {message}{got}")
    return "\n".join(lines)


def _pattern_hint(pattern: str) -> str:
    hints = {
        NAME_PATTERN: "must be snake_case: lowercase letters, digits, _, 2-40 chars",
        SEMVER_PATTERN: "must be a semantic version like 1.2.3",
        MODEL_PATTERN: "must be a Claude model id like claude-sonnet-5-5",
    }
    return hints.get(pattern, f"must match {pattern}")


def parse_semver(version: str) -> tuple[int, int, int]:
    """'1.10.0' -> (1, 10, 0), so versions sort numerically, not as text."""
    if not re.match(SEMVER_PATTERN, version):
        raise ValueError(f"not a semantic version: {version!r}")
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch
