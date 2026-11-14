"""Services shared by CLI and UI (no Streamlit needed)."""

from datetime import date
from pathlib import Path

import pytest

from gridagent import services
from gridagent.agent.runlog import append_run_log
from gridagent.evals.report import write_report
from gridagent.evals.session import evaluate, load_agent_eval_set
from gridagent.garden.registry import resolve

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
agent = resolve(AGENTS, "energy_qa")
eval_set, _ = load_agent_eval_set(agent)
WIND_CASE = next(c for c in eval_set.cases if c.id == "wind_share_last_week")


def demo_log():
    return services.oracle_demo(agent, WIND_CASE, FIXTURE_DIR, eval_set.today)


def test_valid_specs_lists_newest_first() -> None:
    assert services.valid_specs(AGENTS)[0] == "energy_qa@0.1.0"


def test_oracle_demo_runs_real_tools() -> None:
    log = demo_log()
    assert log.status == "ok"
    assert "23.3%" in log.final_answer
    assert [c.name for c in log.tool_calls] == ["get_generation", "compute_stats"]
    assert '"value": 23.3' in log.tool_calls[1].result  # computed by the real tool


def test_trace_rows_cover_model_steps_and_tool_calls() -> None:
    rows = services.trace_rows(demo_log())
    assert [r["kind"] for r in rows] == ["model", "tool", "model", "tool", "model"]
    assert rows[1]["name"] == "get_generation"


def test_build_data_picks_source(tmp_path: Path) -> None:
    from gridagent.data.client import EnergyChartsClient
    from gridagent.data.fixture_source import FixtureDataSource

    assert isinstance(services.build_data(True, FIXTURE_DIR, tmp_path), FixtureDataSource)
    assert isinstance(services.build_data(False, FIXTURE_DIR, tmp_path), EnergyChartsClient)


def test_build_llm_uses_agent_config() -> None:
    llm = services.build_llm(agent.config)
    assert llm.model_name == "claude-sonnet-5-5"
    assert llm.effort == agent.config.model.effort
    assert services.build_llm(agent.config, "claude-haiku-5-5").model_name == "claude-haiku-5-5"


def test_runs_roundtrip_and_summary(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    first, second = demo_log(), demo_log()
    second.status, second.latency_s, second.cost_usd = "error", 3.0, 0.01
    append_run_log(path, first)
    append_run_log(path, second)
    with path.open("a") as f:
        f.write("not json\n")  # broken lines are skipped
    runs = services.load_runs(path)
    assert [r.run_id for r in runs] == [second.run_id, first.run_id]  # newest first
    summary = services.summarize_runs(runs)
    assert summary.n_runs == 2
    assert summary.ok_rate == 0.5 and summary.error_rate == 0.5
    assert summary.total_cost_usd == pytest.approx(0.01)
    assert services.run_row(runs[0])["status"] == "error"


def test_no_runs() -> None:
    assert services.load_runs(Path("does/not/exist.jsonl")) == []
    assert services.summarize_runs([]).ok_rate is None


def test_reports_listing(tmp_path: Path) -> None:
    report, _ = evaluate(agent, "oracle", "oracle", FIXTURE_DIR)
    write_report(report, tmp_path)
    (tmp_path / "broken.json").write_text("{}")
    paths = services.list_reports(tmp_path)
    assert len(paths) == 2
    loaded = services.load_reports(paths)
    assert len(loaded) == 1  # the broken one is skipped


def test_budget() -> None:
    budget = services.Budget(limit_usd=0.05)
    assert budget.allows_another_call(0.02)
    budget.add(0.04)
    assert not budget.allows_another_call(0.02)
    assert budget.remaining_usd == pytest.approx(0.01)
    budget.add(None)  # unknown cost doesn't crash
    assert budget.spent_usd == pytest.approx(0.04)


def test_ask_logs_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from gridagent.llm.fake import FakeLLM, text

    monkeypatch.setattr(services, "build_llm", lambda cfg, model=None: FakeLLM([[text("hi")]]))
    log_path = tmp_path / "runs.jsonl"
    log = services.ask(agent, "q", None, services.build_data(True, FIXTURE_DIR, tmp_path),
                       date(2026, 11, 14), log_path)  # fmt: skip
    assert log.final_answer == "hi"
    assert services.load_runs(log_path)[0].run_id == log.run_id
