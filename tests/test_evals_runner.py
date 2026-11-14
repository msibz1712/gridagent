"""Eval runner, metrics, record/replay, reports and comparison (all offline)."""

import shutil
from pathlib import Path

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.evals.offline import (
    CaseRecording,
    load_recording,
    new_recording,
    oracle_llm,
    recording_path,
    save_recording,
)
from gridagent.evals.report import compare, comparison_markdown, load_report, write_report
from gridagent.evals.runner import compute_metrics, estimate_live_cost, percentile, run_eval
from gridagent.evals.session import evaluate, load_agent_eval_set
from gridagent.garden.registry import resolve
from gridagent.llm.base import ModelResponse, TokenUsage
from gridagent.llm.fake import FakeLLM, text

from conftest import FIXTURE_DIR

AGENTS = Path(__file__).parent.parent / "agents"
agent = resolve(AGENTS, "energy_qa@0.1.0")  # pinned: tests eval mechanics
eval_set, eval_hash = load_agent_eval_set(agent)


def agent_in(tmp_path: Path):
    """A copy of energy_qa@0.1.0 in tmp_path (with its eval set), for recording tests."""
    shutil.copytree(AGENTS / "energy_qa", tmp_path / "energy_qa")
    return resolve(tmp_path, "energy_qa@0.1.0")


def run_with(llm_for_case, mode="live"):
    return run_eval(
        agent, eval_set, eval_hash, mode, "fake", llm_for_case, FixtureDataSource(FIXTURE_DIR)
    )


def test_oracle_passes_every_case() -> None:
    """CI guard: harness, tools, scoring and eval set are consistent."""
    report, _ = evaluate(agent, "oracle", "oracle", FIXTURE_DIR)
    failures = {c.id: c.failed_checks for c in report.cases if not c.passed}
    assert failures == {}
    assert report.metrics.pass_rate == 1.0


def test_lazy_agent_scores_badly() -> None:
    """An agent that always answers '42' without tools must fail almost everything."""
    report = run_with(lambda case: FakeLLM([[text("The answer is 42.")]]))
    m = report.metrics
    assert m.answer_accuracy == 0.0
    assert m.tool_use_correctness < 0.3
    assert m.pass_rate < 0.2


def test_always_refusing_agent_fails_answerable_cases() -> None:
    report = run_with(lambda case: FakeLLM([[text("I can't help with that.")]]))
    refuse_cases = [c for c in report.cases if c.outcome == "refuse"]
    answer_cases = [c for c in report.cases if c.outcome == "answer"]
    assert all(c.passed for c in refuse_cases)
    assert not any(c.passed for c in answer_cases)
    assert report.metrics.refusal_correctness < 0.4


def test_percentile_nearest_rank() -> None:
    values = sorted([1.0, 2.0, 3.0, 4.0, 10.0])
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 10.0
    assert percentile([], 50) is None


def test_cost_metrics_with_known_model() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=100)
    report = run_with(
        lambda case: FakeLLM([[text("I can't help.")]], model_name="claude-haiku-5-5", usage=usage)
    )
    assert report.metrics.avg_cost_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
    assert report.metrics.total_cost_usd == pytest.approx(report.metrics.avg_cost_usd * 29)


def test_live_cost_estimate() -> None:
    assert estimate_live_cost("claude-haiku-5-5", 29) < estimate_live_cost("claude-sonnet-5-5", 29)
    assert estimate_live_cost("unknown-model", 29) is None


def test_record_then_replay_reproduces_results(tmp_path: Path) -> None:
    # Build a "recording" from oracle turns, as a live --record run would.
    recording = new_recording("energy_qa", "0.1.0", "claude-sonnet-5-5", eval_hash)
    for case in eval_set.cases:
        turns = oracle_llm(case).turns
        responses = [
            ModelResponse(
                content=t,
                stop_reason="tool_use" if t[0]["type"] == "tool_use" else "end_turn",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model="claude-sonnet-5-5",
            )
            for t in turns
        ]
        recording.cases[case.id] = CaseRecording(
            question=case.question, responses=responses, latency_s=1.5
        )
    replay_agent = agent_in(tmp_path)
    path = recording_path(replay_agent.directory, "claude-sonnet-5-5")
    save_recording(path, recording)
    assert load_recording(path).cases.keys() == recording.cases.keys()

    report, _ = evaluate(replay_agent, "claude-sonnet-5-5", "replay", FIXTURE_DIR)
    assert report.metrics.pass_rate == 1.0
    assert report.metrics.latency_p50_s == 1.5  # original latency reported
    assert report.metrics.total_cost_usd > 0


def test_replay_without_recording_explains(tmp_path: Path) -> None:
    from gridagent.agent.config import AgentConfigError

    replay_agent = agent_in(tmp_path)
    with pytest.raises(AgentConfigError, match="--live --record"):
        evaluate(replay_agent, "claude-sonnet-5-5", "replay", FIXTURE_DIR)


def test_reports_written_and_reloadable(tmp_path: Path) -> None:
    report, _ = evaluate(agent, "oracle", "oracle", FIXTURE_DIR)
    md_path, json_path = write_report(report, tmp_path)
    markdown = md_path.read_text()
    for heading in (
        "# Eval report: energy_qa@0.1.0",
        "## Metrics",
        "## By category",
        "## Failures (0)",
        "## All cases",
    ):
        assert heading in markdown
    assert load_report(json_path) == report


def test_compare_finds_fixed_and_broken_cases() -> None:
    good, _ = evaluate(agent, "oracle", "oracle", FIXTURE_DIR)
    bad = run_with(lambda case: FakeLLM([[text("I can't help with that.")]]))
    result = compare(bad, good)
    assert "price_mean_yesterday" in result.fixed
    assert result.broken == []
    pass_rate = next(d for d in result.metrics if d.label.startswith("Pass rate"))
    assert pass_rate.better == "b"
    assert any("Different modes" in w for w in result.warnings)
    assert "Fixed in B" in comparison_markdown(result)


def test_compute_metrics_ignores_not_applicable_checks() -> None:
    report = run_with(lambda case: FakeLLM([[text("I can't help with that.")]]))
    metrics = compute_metrics([c for c in report.cases if c.outcome == "refuse"])
    assert metrics.answer_accuracy is None  # no answerable cases in this subset
    assert metrics.refusal_correctness == 1.0


def test_oracle_catches_a_tool_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation test: break the definition of total generation (count pumped storage)
    and the oracle eval must notice, through the grounding check on share questions."""
    from gridagent.data import sources

    broken_total = {**sources.SOURCE_GROUPS["total"], "hydro_pumped_storage": 1.0}
    monkeypatch.setitem(sources.SOURCE_GROUPS, "total", broken_total)
    report, _ = evaluate(agent, "oracle", "oracle", FIXTURE_DIR)
    failed = {c.id for c in report.cases if "grounded" in c.failed_checks}
    assert {"wind_share_last_week", "solar_share_october", "renewable_share_last_week"} <= failed
    assert "price_mean_yesterday" not in failed  # price questions are unaffected
