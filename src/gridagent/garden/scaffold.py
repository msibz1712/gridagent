"""`garden new`: create a new agent from templates/agent_template.

Template files use `$placeholders` (Python's standard `string.Template`).
The new agent is validated right after it is written, so a broken template
is caught immediately instead of at the first run.
"""

import json
import re
import shutil
from pathlib import Path
from string import Template

from gridagent.agent.config import NAME_PATTERN, AgentConfigError, LoadedAgent, load_agent_dir

FIRST_VERSION = "0.1.0"
PER_VERSION_FILES = ("agent.yaml", "system_prompt.md")
PER_AGENT_FILES = ("README.md",)


def create_agent(
    agents_dir: Path, template_dir: Path, name: str, owner: str, description: str
) -> LoadedAgent:
    """Write agents/<name>/README.md and agents/<name>/0.1.0/*, then validate them."""
    if not re.match(NAME_PATTERN, name):
        raise AgentConfigError(
            f"Invalid agent name '{name}': use snake_case (lowercase letters, digits, _), "
            "2-40 characters, starting with a letter."
        )
    agent_dir = agents_dir / name
    if agent_dir.exists():
        raise AgentConfigError(f"Agent '{name}' already exists at {agent_dir}.")
    missing = [f for f in PER_VERSION_FILES + PER_AGENT_FILES if not (template_dir / f).is_file()]
    if missing:
        raise AgentConfigError(f"Template {template_dir} is missing: {', '.join(missing)}")

    values = {
        "name": name,
        "title": name.replace("_", " ").title(),
        "owner": owner,
        "description": description,
        # JSON strings are valid YAML scalars, so quotes/colons can't break agent.yaml.
        "owner_yaml": json.dumps(owner),
        "description_yaml": json.dumps(description),
    }
    version_dir = agent_dir / FIRST_VERSION
    version_dir.mkdir(parents=True)
    try:
        for filename in PER_VERSION_FILES:
            _render(template_dir / filename, version_dir / filename, values)
        for filename in PER_AGENT_FILES:
            _render(template_dir / filename, agent_dir / filename, values)
        return load_agent_dir(version_dir)
    except Exception:
        shutil.rmtree(agent_dir)  # don't leave a half-created agent behind
        raise


def _render(source: Path, target: Path, values: dict[str, str]) -> None:
    # substitute() (not safe_substitute) fails loudly on a missing placeholder.
    target.write_text(Template(source.read_text()).substitute(values))
