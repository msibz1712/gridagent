"""Registry discovery and name@version resolution."""

from pathlib import Path

import pytest
import yaml

from gridagent.agent.config import AgentConfigError
from gridagent.garden.registry import discover, resolve

REPO_AGENTS = Path(__file__).parent.parent / "agents"
BASE = {
    "name": "demo_agent",
    "description": "A demo agent used in tests.",
    "owner": "tests",
    "model": {"name": "claude-haiku-5-5"},
    "tools": ["compute_stats"],
}


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """demo_agent with versions 0.2.0, 0.10.0 (valid) and a broken 0.9.0."""
    for version in ("0.2.0", "0.9.0", "0.10.0"):
        version_dir = tmp_path / "demo_agent" / version
        version_dir.mkdir(parents=True)
        config = {**BASE, "version": version}
        if version == "0.9.0":
            config["tools"] = ["does_not_exist"]
        (version_dir / "agent.yaml").write_text(yaml.safe_dump(config))
        (version_dir / "system_prompt.md").write_text("prompt")
    (tmp_path / "demo_agent" / "notes").mkdir()  # non-version folders are ignored
    return tmp_path


def test_discover_sorts_versions_and_reports_invalid(registry: Path) -> None:
    [entry] = discover(registry)
    assert [v.version for v in entry.versions] == ["0.2.0", "0.9.0", "0.10.0"]
    broken = entry.versions[1]
    assert broken.agent is None and "does_not_exist" in broken.error
    assert entry.latest_valid.config.version == "0.10.0"


def test_resolve_latest_and_pinned(registry: Path) -> None:
    assert resolve(registry, "demo_agent").config.version == "0.10.0"
    assert resolve(registry, "demo_agent@0.2.0").config.version == "0.2.0"


def test_resolve_pinned_invalid_version_raises(registry: Path) -> None:
    with pytest.raises(AgentConfigError, match="does_not_exist"):
        resolve(registry, "demo_agent@0.9.0")


def test_resolve_unknown_version_lists_available(registry: Path) -> None:
    with pytest.raises(AgentConfigError, match="Available: 0.2.0, 0.9.0, 0.10.0"):
        resolve(registry, "demo_agent@3.0.0")


def test_resolve_unknown_agent_lists_known(registry: Path) -> None:
    with pytest.raises(AgentConfigError, match="Known agents: demo_agent"):
        resolve(registry, "nope")


def test_repository_agents_are_all_valid() -> None:
    """Guards the real registry: every committed agent version must load."""
    entries = discover(REPO_AGENTS)
    assert entries, "no agents found"
    for entry in entries:
        assert entry.has_readme, f"{entry.name} has no README.md"
        for version in entry.versions:
            assert version.error is None, version.error
