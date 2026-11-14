"""CLI: `garden run` wiring, with the Claude model replaced by a FakeLLM."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gridagent import cli
from gridagent.llm.fake import FakeLLM, text, tool_use

runner = CliRunner()
ROOT = Path(__file__).parent.parent


def test_run_without_api_key_explains_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["run", "energy_qa", "hi"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_run_unknown_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    result = runner.invoke(cli.app, ["run", "nope", "hi"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_run_end_to_end_with_fake_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GARDEN_LOG_DIR", str(tmp_path))
    monkeypatch.chdir(ROOT)  # agents/ and tests/fixtures are found relative to here
    fake = FakeLLM(
        [
            [tool_use("get_day_ahead_price", {"start": "2026-11-13", "end": "2026-11-13"})],
            [text("Average 161.73 EUR/MWh on 2026-11-13. Source: SMARD.de via Energy-Charts.info")],
        ]
    )
    monkeypatch.setattr(cli, "ClaudeLLM", lambda **kwargs: fake)

    result = runner.invoke(
        cli.app,
        ["run", "energy_qa", "Average price yesterday?", "--fixtures", "--today", "2026-11-14"],
    )

    assert result.exit_code == 0, result.output
    assert "161.73" in result.output
    log = json.loads((tmp_path / "runs.jsonl").read_text().splitlines()[0])
    assert log["today"] == "2026-11-14"
    assert log["status"] == "ok"
