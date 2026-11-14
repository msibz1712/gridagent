"""Discover and resolve agents in the registry folder.

Layout:
    agents/<name>/README.md
    agents/<name>/<version>/agent.yaml      (one folder per semantic version)
    agents/<name>/<version>/system_prompt.md

A spec is "name" (latest version) or "name@1.2.0" (a pinned version).
Older versions stay on disk unchanged, so they can be re-run and compared.
"""

import re
from pathlib import Path

from pydantic import BaseModel

from gridagent.agent.config import (
    SEMVER_PATTERN,
    AgentConfigError,
    LoadedAgent,
    load_agent_dir,
    parse_semver,
)


class VersionEntry(BaseModel):
    """One version folder: either a loaded agent or the reason it is invalid."""

    version: str
    agent: LoadedAgent | None = None
    error: str | None = None


class AgentEntry(BaseModel):
    name: str
    directory: Path
    versions: list[VersionEntry]  # sorted oldest -> newest

    @property
    def latest_valid(self) -> LoadedAgent | None:
        valid = [v.agent for v in self.versions if v.agent is not None]
        return valid[-1] if valid else None

    @property
    def has_readme(self) -> bool:
        return (self.directory / "README.md").is_file()


def _version_dirs(agent_dir: Path) -> list[Path]:
    """Subfolders whose names look like versions, sorted numerically."""
    dirs = [d for d in agent_dir.iterdir() if d.is_dir() and re.match(SEMVER_PATTERN, d.name)]
    return sorted(dirs, key=lambda d: parse_semver(d.name))


def discover(agents_dir: Path) -> list[AgentEntry]:
    """Every agent in the registry. Invalid versions are reported, not raised."""
    if not agents_dir.is_dir():
        return []
    entries = []
    for agent_dir in sorted(d for d in agents_dir.iterdir() if d.is_dir()):
        if agent_dir.name.startswith((".", "_")):
            continue
        versions = []
        for version_dir in _version_dirs(agent_dir):
            try:
                versions.append(
                    VersionEntry(version=version_dir.name, agent=load_agent_dir(version_dir))
                )
            except AgentConfigError as exc:
                versions.append(VersionEntry(version=version_dir.name, error=str(exc)))
        entries.append(AgentEntry(name=agent_dir.name, directory=agent_dir, versions=versions))
    return entries


def resolve(agents_dir: Path, spec: str) -> LoadedAgent:
    """Load 'name' (latest version) or 'name@version'. Raises AgentConfigError.

    An invalid latest version is an error, not silently skipped: you should
    never run an older version without asking for it.
    """
    name, _, version = spec.partition("@")
    agent_dir = agents_dir / name
    if not agent_dir.is_dir():
        known = ", ".join(e.name for e in discover(agents_dir)) or "none"
        raise AgentConfigError(f"No agent '{name}' in {agents_dir}. Known agents: {known}.")

    dirs = _version_dirs(agent_dir)
    if not dirs:
        raise AgentConfigError(f"Agent '{name}' has no version folders (e.g. {agent_dir}/0.1.0).")

    if not version:
        return load_agent_dir(dirs[-1])  # latest; an invalid latest version is an error
    if not (agent_dir / version).is_dir():
        available = ", ".join(d.name for d in dirs)
        raise AgentConfigError(f"Agent '{name}' has no version {version}. Available: {available}.")
    return load_agent_dir(agent_dir / version)
