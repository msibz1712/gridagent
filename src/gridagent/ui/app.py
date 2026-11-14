"""GridAgent web UI.

Start it with:  set -a && source .env && set +a && uv run --extra ui garden ui

Streamlit re-runs this whole script on every interaction (click, input);
anything that must survive a re-run lives in `st.session_state`.
All logic lives in `gridagent.services`; this file is layout only.
"""

import json
import os
from datetime import date
from pathlib import Path

import streamlit as st

from gridagent import services
from gridagent.agent.config import AgentConfigError, LoadedAgent
from gridagent.agent.runlog import RunLog
from gridagent.data.client import berlin_today
from gridagent.evals.report import METRIC_ROWS, compare, fmt, write_report
from gridagent.evals.runner import EvalReport, estimate_live_cost
from gridagent.evals.session import choose_offline_mode, evaluate, load_agent_eval_set
from gridagent.garden.registry import discover, resolve

AGENTS_DIR = Path(os.environ.get("GARDEN_AGENTS_DIR", "agents"))
REPORTS_DIR = Path(os.environ.get("GARDEN_REPORTS_DIR", "reports"))
LOG_PATH = Path(os.environ.get("GARDEN_LOG_DIR", "logs")) / "runs.jsonl"
CACHE_DIR = Path(os.environ.get("GARDEN_CACHE_DIR", ".cache/energy_charts"))
FIXTURE_DIR = Path("tests/fixtures")
BUDGET_USD = float(os.environ.get("GARDEN_UI_BUDGET_USD", "1.00"))
MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5"]
STATUS_ICON = {"ok": "✅", "refused": "🛑", "max_steps": "⚠️", "timeout": "⏱️", "error": "❌"}


def safe_markdown(text: str) -> str:
    """Streamlit renders $...$ as LaTeX; escape dollar signs in model output."""
    return text.replace("$", "\\$")


def fixture_today(agent: LoadedAgent) -> date:
    """The 'today' the fixtures were recorded for (from the eval set), else real today."""
    try:
        eval_set, _ = load_agent_eval_set(agent)
        return eval_set.today
    except AgentConfigError:
        return berlin_today()


# ---------- shared rendering ----------


def render_run(log: RunLog) -> None:
    icon = STATUS_ICON.get(log.status, "")
    with st.container(border=True):
        st.markdown(f"**Answer** {icon} `{log.status}`")
        st.markdown(safe_markdown(log.final_answer or "(no answer)"))
        if log.error:
            st.error(log.error)
    tokens_in = (
        log.usage.input_tokens
        + log.usage.cache_read_input_tokens
        + log.usage.cache_creation_input_tokens
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Steps", len(log.steps))
    c2.metric("Latency", f"{log.latency_s:.1f} s")
    c3.metric("Tokens in / out", f"{tokens_in:,} / {log.usage.output_tokens:,}")
    c4.metric("Est. cost", fmt(log.cost_usd, "usd"))

    st.markdown("**Trace**: every model call and tool call, in order")
    st.dataframe(services.trace_rows(log), hide_index=True)
    for i, call in enumerate(log.tool_calls, start=1):
        label = f"Tool result {i}: {call.name}" + (" (error)" if call.is_error else "")
        with st.expander(label):
            try:
                st.json(json.loads(call.result))
            except ValueError:
                st.code(call.result)
    st.caption(
        f"Run {log.run_id} · {log.agent}@{log.agent_version} · {log.model} · today={log.today}"
    )


def render_report(report: EvalReport) -> None:
    st.caption(
        f"{report.agent}@{report.agent_version} · {report.model} · mode={report.mode} · "
        f"{report.eval_set} v{report.eval_set_version} ({report.eval_set_hash}) · "
        f"{report.metrics.n_cases} cases"
    )
    for note in report.notes:
        st.info(note)
    m = report.metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pass rate", fmt(m.pass_rate, "pct"))
    c2.metric("Answer accuracy", fmt(m.answer_accuracy, "pct"))
    c3.metric("Refusal correctness", fmt(m.refusal_correctness, "pct"))
    c4.metric("Avg cost / question", fmt(m.avg_cost_usd, "usd"))
    st.dataframe(
        [{"metric": label, "value": fmt(getattr(m, f), kind)} for f, label, kind in METRIC_ROWS],
        hide_index=True,
    )
    st.dataframe(
        [
            {
                "case": c.id,
                "category": c.category,
                "result": "pass" if c.passed else "FAIL",
                "failed checks": ", ".join(c.failed_checks),
                "seconds": c.latency_s,
                "cost_usd": c.cost_usd,
            }
            for c in report.cases
        ],
        hide_index=True,
    )
    for c in [c for c in report.cases if not c.passed]:
        with st.expander(f"FAIL {c.id}: {', '.join(c.failed_checks)}"):
            st.markdown(f"**Question:** {c.question}")
            st.markdown(f"**Answer:** {safe_markdown(c.answer)}")
            st.json(c.tool_calls)


# ---------- tabs ----------


def ask_tab(agent: LoadedAgent, model: str, use_fixtures: bool) -> None:
    live, demo = st.tabs(["Live model", "Free demo (no API cost)"])

    with live:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not has_key:
            st.warning(
                "ANTHROPIC_API_KEY is not set, so live questions are disabled. Start the UI "
                "with `set -a && source .env && set +a && uv run --extra ui garden ui`, or use "
                "the free demo tab."
            )
        today = fixture_today(agent) if use_fixtures else berlin_today()
        st.caption(
            f"Data: {'recorded fixtures' if use_fixtures else 'live Energy-Charts API'} · "
            f"today = {today} · model = {model}"
        )
        question = st.text_area(
            "Question",
            placeholder="What was the average day-ahead price yesterday?",
            height=80,
            key="question",
        )
        budget: services.Budget = st.session_state.budget
        per_question = estimate_live_cost(model, 1) or 0.05
        if st.button("Ask", type="primary", disabled=not has_key, key="ask_button"):
            if not question.strip():
                st.warning("Type a question first.")
            elif not budget.allows_another_call(per_question):
                st.error(
                    f"Session budget of ${budget.limit_usd:.2f} reached. Restart the UI or set "
                    "GARDEN_UI_BUDGET_USD to allow more."
                )
            else:
                data = services.build_data(use_fixtures, FIXTURE_DIR, CACHE_DIR)
                with st.spinner(f"Asking {agent.spec} on {model}..."):
                    log = services.ask(agent, question, model, data, today, LOG_PATH)
                budget.add(log.cost_usd)
                st.session_state.last_live = log
                st.rerun()  # redraw so the sidebar shows the new session spend
        if "last_live" in st.session_state:
            render_run(st.session_state.last_live)

    with demo:
        st.markdown(
            "The **oracle** is a scripted, perfect agent from the eval set: it makes the "
            "expected tool calls, and the tools really run on the recorded data. Use it to "
            "show how the agent works without an API key. It is not the language model."
        )
        try:
            eval_set, _ = load_agent_eval_set(agent)
        except AgentConfigError as exc:
            st.info(f"No eval set for this agent: {exc}")
            return
        cases = {f"{c.question}  [{c.category}]": c for c in eval_set.cases}
        choice = st.selectbox("Eval question", list(cases))
        if st.button("Run demo", key="demo_button"):
            st.session_state.last_demo = services.oracle_demo(
                agent, cases[choice], FIXTURE_DIR, eval_set.today
            )
        if "last_demo" in st.session_state:
            render_run(st.session_state.last_demo)


def runs_tab() -> None:
    runs = services.load_runs(LOG_PATH)
    if not runs:
        st.info(f"No runs logged yet in {LOG_PATH}.")
        return
    s = services.summarize_runs(runs)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Runs (last 200)", s.n_runs)
    c2.metric("OK", fmt(s.ok_rate, "pct"))
    c3.metric("Errors", fmt(s.error_rate, "pct"))
    c4.metric(
        "Latency p50 / p95", f"{fmt(s.latency_p50_s, 'num')} / {fmt(s.latency_p95_s, 'num')} s"
    )
    c5.metric("Total cost", fmt(s.total_cost_usd, "usd"))
    st.dataframe([services.run_row(r) for r in runs], hide_index=True)
    by_id = {f"{r.run_id} · {r.question[:70]}": r for r in runs}
    picked = st.selectbox("Inspect a run", list(by_id))
    render_run(by_id[picked])


def evals_tab(agent: LoadedAgent, model: str) -> None:
    mode = choose_offline_mode(agent, model)
    st.markdown(
        f"Offline evaluation is free (mode for {agent.spec} on {model}: **{mode}**). "
        "Live evaluations cost money and ask for confirmation, so they are run from the CLI: "
        f"`garden eval {agent.config.name} --live --record --model {model}`."
    )
    if st.button(f"Run offline eval of {agent.spec}", key="eval_button"):
        with st.spinner("Evaluating..."):
            report, _ = evaluate(agent, model, mode, FIXTURE_DIR)
            md_path, _ = write_report(report, REPORTS_DIR)
        st.success(f"Report written: {md_path}")

    reports = services.load_reports(services.list_reports(REPORTS_DIR))
    if not reports:
        st.info(f"No reports in {REPORTS_DIR} yet.")
        return
    names = list(reports)
    view, versus = st.tabs(["View report", "Compare two reports"])
    with view:
        render_report(reports[st.selectbox("Report", names)])
    with versus:
        if len(names) < 2:
            st.info("Need at least two reports to compare.")
            return
        a = st.selectbox("A", names, index=1)
        b = st.selectbox("B", names, index=0)
        result = compare(reports[a], reports[b])
        for warning in result.warnings:
            st.warning(warning)
        st.dataframe(
            [{"metric": d.label, "A": d.a, "B": d.b, "better": d.better.upper() or "="}
             for d in result.metrics],
            hide_index=True,
        )  # fmt: skip
        st.markdown(f"**Fixed in B:** {', '.join(result.fixed) or 'none'}")
        st.markdown(f"**Broken in B:** {', '.join(result.broken) or 'none'}")


def garden_tab() -> None:
    rows = []
    for entry in discover(AGENTS_DIR):
        latest = entry.latest_valid
        rows.append(
            {
                "agent": entry.name,
                "versions": ", ".join(v.version for v in entry.versions),
                "model": latest.config.model.name if latest else "-",
                "owner": latest.config.owner if latest else "-",
                "status": "invalid" if any(v.error for v in entry.versions) else "valid",
            }
        )
    st.dataframe(rows, hide_index=True)
    name = st.selectbox("Agent", [r["agent"] for r in rows])
    readme = AGENTS_DIR / name / "README.md"
    if readme.is_file():
        st.markdown(readme.read_text())
    try:
        agent = resolve(AGENTS_DIR, name)
    except AgentConfigError as exc:
        st.error(str(exc))
        return
    with st.expander(f"agent.yaml ({agent.spec})"):
        st.code((agent.directory / "agent.yaml").read_text(), language="yaml")
    with st.expander("System prompt"):
        st.markdown(agent.system_prompt)


# ---------- page ----------


def main() -> None:
    st.set_page_config(page_title="GridAgent", page_icon="⚡", layout="wide")
    if "budget" not in st.session_state:
        st.session_state.budget = services.Budget(limit_usd=BUDGET_USD)

    st.title("⚡ GridAgent")
    st.caption(
        "An evaluated AI agent for Germany's electricity system · data: Energy-Charts.info "
        "(Fraunhofer ISE), prices: Bundesnetzagentur | SMARD.de"
    )
    specs = services.valid_specs(AGENTS_DIR)
    if not specs:
        st.error(f"No valid agents in {AGENTS_DIR}. Run the UI from the project folder.")
        return

    with st.sidebar:
        st.header("Settings")
        spec = st.selectbox("Agent", specs)
        agent = resolve(AGENTS_DIR, spec)
        default = agent.config.model.name
        models = [default] + [m for m in MODELS if m != default]
        model = st.selectbox("Model", models, help="Default comes from agent.yaml")
        use_fixtures = st.toggle(
            "Use recorded data (fixtures)", value=True,
            help="Reproducible and free of rate limits. Off = live Energy-Charts API.",
        )  # fmt: skip
        budget: services.Budget = st.session_state.budget
        st.metric("Session spend", f"${budget.spent_usd:.4f}", help="Live model calls only")
        st.progress(min(budget.spent_usd / budget.limit_usd, 1.0) if budget.limit_usd else 1.0)
        st.caption(f"Limit ${budget.limit_usd:.2f} per session (GARDEN_UI_BUDGET_USD)")

    ask, runs, evals, garden = st.tabs(["Ask", "Run history", "Evaluations", "Agent Garden"])
    with ask:
        ask_tab(agent, model, use_fixtures)
    with runs:
        runs_tab()
    with evals:
        evals_tab(agent, model)
    with garden:
        garden_tab()


main()
