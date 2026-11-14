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
    monkeypatch.setattr(cli, "build_llm", lambda cfg, model=None: fake)

    result = runner.invoke(
        cli.app,
        ["run", "energy_qa", "Average price yesterday?", "--fixtures", "--today", "2026-11-14"],
    )

    assert result.exit_code == 0, result.output
    assert "161.73" in result.output
    log = json.loads((tmp_path / "runs.jsonl").read_text().splitlines()[0])
    assert log["today"] == "2026-11-14"
    assert log["status"] == "ok"


def test_list_shows_energy_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(cli.app, ["list"], terminal_width=200)
    assert result.exit_code == 0
    assert "energy_qa" in result.output and "0.1.0" in result.output and "valid" in result.output


def test_validate_passes_for_repository_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


def test_validate_fails_with_exit_code_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "broken_agent" / "0.1.0"
    broken.mkdir(parents=True)
    (broken / "agent.yaml").write_text("name: broken_agent\nversion: 0.1.0\n")
    monkeypatch.setenv("GARDEN_AGENTS_DIR", str(tmp_path))
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 1
    assert "FAIL" in result.output and "Field required" in result.output


def test_new_then_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(ROOT)  # so the template is found
    monkeypatch.setenv("GARDEN_AGENTS_DIR", str(tmp_path))
    result = runner.invoke(
        cli.app,
        ["new", "price_bot", "--owner", "tests", "--description", "Answers price questions."],
    )
    assert result.exit_code == 0, result.output
    assert "Created price_bot@0.1.0" in result.output
    listed = runner.invoke(cli.app, ["list"], terminal_width=200)
    assert "price_bot" in listed.output


def test_new_rejects_bad_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GARDEN_AGENTS_DIR", str(tmp_path))
    result = runner.invoke(cli.app, ["new", "Bad-Name", "--owner", "x", "--description", "y" * 20])
    assert result.exit_code == 1
    assert "snake_case" in result.output


def test_eval_offline_uses_oracle_and_writes_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GARDEN_REPORTS_DIR", str(tmp_path))
    result = runner.invoke(cli.app, ["eval", "energy_qa", "--min-pass-rate", "1.0"])
    assert result.exit_code == 0, result.output
    assert "oracle" in result.output and "100.0%" in result.output
    assert len(list(tmp_path.glob("*.md"))) == 1 and len(list(tmp_path.glob("*.json"))) == 1


def test_eval_min_pass_rate_fails_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GARDEN_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "evaluate", _fake_evaluate_with_pass_rate(0.5))
    result = runner.invoke(cli.app, ["eval", "energy_qa", "--min-pass-rate", "0.9"])
    assert result.exit_code == 1
    assert "below" in result.output


def _fake_evaluate_with_pass_rate(rate: float):
    from gridagent.evals.session import evaluate

    def fake(*args, **kwargs):
        report, path = evaluate(*args, **kwargs)
        report.metrics.pass_rate = rate
        return report, path

    return fake


def test_eval_live_asks_before_spending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    called = []
    monkeypatch.setattr(cli, "evaluate", lambda *a, **k: called.append(1))
    result = runner.invoke(cli.app, ["eval", "energy_qa", "--live"], input="n\n")
    assert result.exit_code == 0
    assert "estimated cost about $" in result.output
    assert called == []  # declined -> nothing ran


def test_eval_live_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["eval", "energy_qa", "--live", "--yes"])
    assert result.exit_code == 1


def test_record_requires_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    assert runner.invoke(cli.app, ["eval", "energy_qa", "--record"]).exit_code == 1


def test_compare_two_versions_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GARDEN_REPORTS_DIR", str(tmp_path))
    result = runner.invoke(
        cli.app, ["compare", "energy_qa@0.1.0", "energy_qa@0.1.0"], terminal_width=200
    )
    assert result.exit_code == 0, result.output
    assert "Broken in B: none" in result.output
    assert len(list(tmp_path.glob("compare_*.md"))) == 1


def test_ui_explains_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
    result = runner.invoke(cli.app, ["ui"])
    assert result.exit_code == 1
    assert "--extra ui" in result.output


def test_ui_binds_to_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    result = runner.invoke(cli.app, ["ui", "--port", "8600"])
    assert result.exit_code == 0
    command = calls[0]
    assert command[command.index("--server.address") + 1] == "localhost"
    assert command[command.index("--server.port") + 1] == "8600"
    assert command[command.index("--browser.gatherUsageStats") + 1] == "false"
