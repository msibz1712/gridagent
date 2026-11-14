"""`garden new`: scaffolding from the real template."""

from pathlib import Path

import pytest

from gridagent.agent.config import AgentConfigError
from gridagent.garden.registry import resolve
from gridagent.garden.scaffold import create_agent

TEMPLATE = Path(__file__).parent.parent / "templates" / "agent_template"


def test_creates_a_valid_agent(tmp_path: Path) -> None:
    agent = create_agent(tmp_path, TEMPLATE, "price_bot", "tests", "Answers price questions.")
    assert agent.spec == "price_bot@0.1.0"
    assert agent.config.owner == "tests"
    assert "Price Bot" in agent.system_prompt
    assert (tmp_path / "price_bot" / "README.md").read_text().startswith("# price_bot")
    assert resolve(tmp_path, "price_bot").spec == "price_bot@0.1.0"


def test_special_characters_cannot_break_yaml(tmp_path: Path) -> None:
    description = 'Says "hi": prices, #hashtags & more'
    agent = create_agent(tmp_path, TEMPLATE, "quirky", "Team: Data", description)
    assert agent.config.description == description
    assert agent.config.owner == "Team: Data"


def test_rejects_existing_agent(tmp_path: Path) -> None:
    create_agent(tmp_path, TEMPLATE, "price_bot", "tests", "Answers price questions.")
    with pytest.raises(AgentConfigError, match="already exists"):
        create_agent(tmp_path, TEMPLATE, "price_bot", "tests", "Answers price questions.")


@pytest.mark.parametrize("name", ["Price-Bot", "1bot", "x", "has space"])
def test_rejects_bad_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(AgentConfigError, match="snake_case"):
        create_agent(tmp_path, TEMPLATE, name, "tests", "Answers price questions.")


def test_invalid_result_is_rolled_back(tmp_path: Path) -> None:
    """A description that fails validation leaves no half-created folder."""
    with pytest.raises(AgentConfigError, match="description"):
        create_agent(tmp_path, TEMPLATE, "tiny", "tests", "short")
    assert not (tmp_path / "tiny").exists()
