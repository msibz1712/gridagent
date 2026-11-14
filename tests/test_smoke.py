"""Smoke tests: the package imports and the CLI starts."""

from typer.testing import CliRunner

import gridagent
from gridagent.cli import app

runner = CliRunner()


def test_package_has_version() -> None:
    assert gridagent.__version__ == "0.1.0"


def test_cli_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
