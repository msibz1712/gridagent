"""Wire an eval run together for a given mode (used by `garden eval` and `garden compare`)."""

from collections.abc import Callable
from pathlib import Path

from gridagent.agent.config import AgentConfigError, LoadedAgent
from gridagent.agent.runlog import RunLog
from gridagent.data.fixture_source import FixtureDataSource
from gridagent.evals.dataset import EvalCase, EvalSet, load_eval_set
from gridagent.evals.offline import (
    CaseRecording,
    RecordingLLM,
    load_recording,
    new_recording,
    oracle_llm,
    recording_path,
    replay_llm,
    save_recording,
)
from gridagent.evals.runner import CaseResult, EvalReport, Mode, run_eval
from gridagent.llm.base import LLM, LLMError
from gridagent.llm.claude import ClaudeLLM
from gridagent.llm.fake import FakeLLM


def load_agent_eval_set(agent: LoadedAgent) -> tuple[EvalSet, str]:
    path = agent.eval_set_path()
    if path is None:
        raise AgentConfigError(f"{agent.spec} has no eval_set in agent.yaml")
    return load_eval_set(path)


def choose_offline_mode(agent: LoadedAgent, model: str) -> Mode:
    """Replay if a recording exists for this version + model, otherwise the oracle."""
    return "replay" if recording_path(agent.directory, model).is_file() else "oracle"


def evaluate(
    agent: LoadedAgent,
    model: str,
    mode: Mode,
    fixture_dir: Path,
    record: bool = False,
    progress: Callable[[int, int, CaseResult], None] | None = None,
) -> tuple[EvalReport, Path | None]:
    """Run the agent's eval set. Returns the report and the recording path (if written)."""
    eval_set, eval_hash = load_agent_eval_set(agent)
    data = FixtureDataSource(fixture_dir)
    cfg = agent.config
    rec_path = recording_path(agent.directory, model)
    recording = new_recording(cfg.name, cfg.version, model, eval_hash) if record else None
    stored = None  # the recording being replayed (replay mode only)

    if mode == "live":

        def llm_for_case(case: EvalCase) -> LLM:
            return RecordingLLM(
                ClaudeLLM(
                    model_name=model,
                    max_tokens=cfg.model.max_tokens,
                    effort=cfg.model.effort,
                    refusal_fallback=cfg.model.refusal_fallback,
                    timeout_seconds=cfg.limits.model_timeout_seconds,
                    max_retries=cfg.limits.model_max_retries,
                )
            )

        def after_case(case: EvalCase, log: RunLog, llm: LLM) -> RunLog:
            if recording is not None and isinstance(llm, RecordingLLM):
                recording.cases[case.id] = CaseRecording(
                    question=case.question, responses=llm.responses, latency_s=log.latency_s
                )
            return log

    elif mode == "replay":
        stored = load_recording(rec_path)
        if stored is None:
            raise AgentConfigError(f"No recording at {rec_path}; run with --live --record first")

        def llm_for_case(case: EvalCase) -> LLM:
            rec = stored.cases.get(case.id)
            if rec is None or rec.question != case.question:
                return FakeLLM([LLMError(f"No recording for case '{case.id}'")])
            return replay_llm(rec)

        def after_case(case: EvalCase, log: RunLog, llm: LLM) -> RunLog:
            rec = stored.cases.get(case.id)
            if rec is not None:
                log.latency_s = rec.latency_s  # report the original live latency
            return log

    else:  # oracle

        def llm_for_case(case: EvalCase) -> LLM:
            return oracle_llm(case)

        after_case = None

    report = run_eval(
        agent,
        eval_set,
        eval_hash,
        mode,
        model if mode != "oracle" else "oracle",
        llm_for_case,
        data,
        after_case=after_case,
        progress=progress,
    )
    if mode == "replay" and stored is not None and stored.eval_set_hash != eval_hash:
        report.notes.append("Recording was made with a different eval set version.")

    if recording is not None:
        save_recording(rec_path, recording)
        return report, rec_path
    return report, None
