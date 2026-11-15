"""agent.yaml validation: every rule, and readable error messages."""

from pathlib import Path

import pytest
import yaml

from gridagent.agent.config import AgentConfigError, load_agent_dir, parse_semver

VALID = {
    "name": "demo_agent",
    "version": "0.1.0",
    "description": "A demo agent used in tests.",
    "owner": "tests",
    "model": {"name": "claude-haiku-5-5"},
    "tools": ["get_day_ahead_price", "compute_stats"],
}


def write_agent(root: Path, config: dict, prompt: str = "You are a test agent.") -> Path:
    """Create root/<name>/<version>/ with agent.yaml and system_prompt.md."""
    version_dir = root / "demo_agent" / str(config.get("version", "0.1.0"))
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "agent.yaml").write_text(yaml.safe_dump(config))
    (version_dir / "system_prompt.md").write_text(prompt)
    return version_dir


def with_changes(**changes) -> dict:
    return {**VALID, **changes}


def load_error(tmp_path: Path, config: dict) -> str:
    with pytest.raises(AgentConfigError) as info:
        load_agent_dir(write_agent(tmp_path, config))
    return str(info.value)


def test_valid_config_loads_with_defaults(tmp_path: Path) -> None:
    agent = load_agent_dir(write_agent(tmp_path, VALID))
    assert agent.spec == "demo_agent@0.1.0"
    assert agent.config.model.effort == "medium"
    assert agent.config.limits.max_steps == 8
    assert agent.system_prompt == "You are a test agent."


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"name": "Demo-Agent"}, "name: must be snake_case"),
        ({"version": "1.0"}, "version: must be a semantic version"),
        ({"model": {"name": "gpt-4"}}, "model.name: must be a Claude model id"),
        ({"model": {"name": "claude-haiku-5-5", "effort": "hgh"}}, "model.effort"),
        ({"model": {"name": "claude-haiku-5-5", "temperature": 0}}, "unknown field (typo?)"),
        ({"tools": ["get_stock_prices"]}, "unknown tool(s) ['get_stock_prices']"),
        ({"tools": ["compute_stats", "compute_stats"]}, "duplicates"),
        ({"tools": []}, "tools"),
        ({"description": "short"}, "description"),
        ({"limits": {"max_steps": 0}}, "limits.max_steps"),
        ({"owner_email": "x"}, "owner_email: unknown field"),
    ],
)
def test_invalid_fields_give_readable_errors(tmp_path: Path, changes: dict, expected: str) -> None:
    message = load_error(tmp_path, with_changes(**changes))
    assert expected in message
    assert "agent.yaml" in message  # always says which file


def test_missing_required_field(tmp_path: Path) -> None:
    config = {k: v for k, v in VALID.items() if k != "owner"}
    assert "owner: Field required" in load_error(tmp_path, config)


def test_name_and_version_must_match_folders(tmp_path: Path) -> None:
    version_dir = write_agent(tmp_path, VALID)
    renamed = version_dir.parent.parent / "other_name" / "0.2.0"
    renamed.parent.mkdir()
    version_dir.rename(renamed)
    with pytest.raises(AgentConfigError) as info:
        load_agent_dir(renamed)
    assert "must match folder 'other_name'" in str(info.value)
    assert "must match folder '0.2.0'" in str(info.value)


def test_missing_system_prompt_file(tmp_path: Path) -> None:
    assert "not found" in load_error(tmp_path, with_changes(system_prompt_file="nope.md"))


def test_missing_eval_set_file(tmp_path: Path) -> None:
    assert "eval_set 'evals.yaml' not found" in load_error(
        tmp_path, with_changes(eval_set="evals.yaml")
    )


def test_empty_system_prompt(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError, match="empty"):
        load_agent_dir(write_agent(tmp_path, VALID, prompt="  \n"))


def test_invalid_yaml(tmp_path: Path) -> None:
    version_dir = write_agent(tmp_path, VALID)
    (version_dir / "agent.yaml").write_text("name: [unclosed")
    with pytest.raises(AgentConfigError, match="invalid YAML"):
        load_agent_dir(version_dir)


def test_semver_sorts_numerically() -> None:
    versions = ["0.10.0", "0.2.0", "1.0.0", "0.9.1"]
    assert sorted(versions, key=parse_semver) == ["0.2.0", "0.9.1", "0.10.0", "1.0.0"]
