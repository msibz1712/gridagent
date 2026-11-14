"""Write eval reports (Markdown for people, JSON for machines) and compare two reports."""

import json
from pathlib import Path

from pydantic import BaseModel

from gridagent.evals.runner import EvalReport, Metrics

# (metric field, label, format): the order used in every table.
METRIC_ROWS = [
    ("pass_rate", "Pass rate (all checks)", "pct"),
    ("answer_accuracy", "Answer accuracy (numbers ±1%, dates)", "pct"),
    ("grounding_rate", "Numbers grounded in tool results", "pct"),
    ("tool_use_correctness", "Tool-use correctness", "pct"),
    ("refusal_correctness", "Refusal correctness", "pct"),
    ("no_data_correctness", "Missing-data handling", "pct"),
    ("source_citation_rate", "Source cited", "pct"),
    ("error_rate", "Error rate", "pct"),
    ("latency_p50_s", "Latency p50 (s)", "num"),
    ("latency_p95_s", "Latency p95 (s)", "num"),
    ("avg_input_tokens", "Avg input tokens / question", "int"),
    ("avg_output_tokens", "Avg output tokens / question", "int"),
    ("avg_cost_usd", "Avg cost / question (USD)", "usd"),
    ("total_cost_usd", "Total cost (USD)", "usd"),
]
LOWER_IS_BETTER = {
    "error_rate",
    "latency_p50_s",
    "latency_p95_s",
    "avg_input_tokens",
    "avg_output_tokens",
    "avg_cost_usd",
    "total_cost_usd",
}


def fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value:.1%}"
    if kind == "usd":
        return f"${value:.4f}"
    if kind == "int":
        return f"{value:,.0f}"
    return f"{value:.2f}"


def report_stem(report: EvalReport) -> str:
    stamp = report.created_at.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{report.agent}_{report.agent_version}_{report.model}_{report.mode}"


def write_report(report: EvalReport, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = report_stem(report)
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n")
    md_path.write_text(to_markdown(report))
    return md_path, json_path


def load_report(path: Path) -> EvalReport:
    return EvalReport.model_validate(json.loads(path.read_text()))


def to_markdown(report: EvalReport) -> str:
    m = report.metrics
    lines = [
        f"# Eval report: {report.agent}@{report.agent_version}",
        "",
        "| | |",
        "|---|---|",
        f"| Model | `{report.model}` |",
        f"| Mode | {report.mode} |",
        f"| Eval set | {report.eval_set} v{report.eval_set_version} "
        f"(`{report.eval_set_hash}`), {m.n_cases} cases |",
        f"| Data | fixtures, today = {report.today} |",
        f"| Created | {report.created_at:%Y-%m-%d %H:%M UTC} |",
        "",
        *[f"> {note}" for note in report.notes],
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        *[f"| {label} | {fmt(getattr(m, field), kind)} |" for field, label, kind in METRIC_ROWS],
        "",
        "## By category",
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    categories = sorted({c.category for c in report.cases})
    for category in categories:
        cases = [c for c in report.cases if c.category == category]
        lines.append(f"| {category} | {sum(c.passed for c in cases)}/{len(cases)} |")

    failures = [c for c in report.cases if not c.passed]
    lines += ["", f"## Failures ({len(failures)})", ""]
    if not failures:
        lines.append("None.")
    for c in failures:
        calls = ", ".join(f"{t['name']}({json.dumps(t['input'])})" for t in c.tool_calls) or "none"
        lines += [
            f"### {c.id} ({c.category})",
            "",
            f"- **Question:** {c.question}",
            f"- **Failed checks:** {', '.join(c.failed_checks)}",
            f"- **Status:** {c.status}" + (f" ({c.error})" if c.error else ""),
            f"- **Tool calls:** {calls}",
            f"- **Answer:** {c.answer.strip()[:600]}",
            "",
        ]

    lines += [
        "## All cases",
        "",
        "| Case | Category | Result | Failed checks | Steps | Latency (s) | Cost (USD) |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in report.cases:
        result = "pass" if c.passed else "**FAIL**"
        lines.append(
            f"| {c.id} | {c.category} | {result} | {', '.join(c.failed_checks) or '-'} | "
            f"{c.steps} | {c.latency_s:.2f} | {fmt(c.cost_usd, 'usd')} |"
        )
    return "\n".join(lines) + "\n"


# ---------- comparison ----------


class MetricDelta(BaseModel):
    label: str
    a: str
    b: str
    better: str  # "a", "b" or "" (equal / not comparable)


class Comparison(BaseModel):
    a_label: str
    b_label: str
    warnings: list[str]
    metrics: list[MetricDelta]
    fixed: list[str]  # failed in A, passed in B
    broken: list[str]  # passed in A, failed in B


def label(report: EvalReport) -> str:
    return f"{report.agent}@{report.agent_version} / {report.model} / {report.mode}"


def compare(a: EvalReport, b: EvalReport) -> Comparison:
    warnings = []
    if a.eval_set_hash != b.eval_set_hash:
        warnings.append("Different eval set contents: results are not directly comparable.")
    if a.mode != b.mode:
        warnings.append(f"Different modes ({a.mode} vs {b.mode}).")
    if "oracle" in (a.mode, b.mode):
        warnings.append("Oracle mode does not measure model quality.")

    deltas = [_delta(a.metrics, b.metrics, f, lbl, kind) for f, lbl, kind in METRIC_ROWS]
    a_pass = {c.id: c.passed for c in a.cases}
    b_pass = {c.id: c.passed for c in b.cases}
    shared = [i for i in a_pass if i in b_pass]
    return Comparison(
        a_label=label(a),
        b_label=label(b),
        warnings=warnings,
        metrics=deltas,
        fixed=[i for i in shared if not a_pass[i] and b_pass[i]],
        broken=[i for i in shared if a_pass[i] and not b_pass[i]],
    )


def _delta(a: Metrics, b: Metrics, field: str, lbl: str, kind: str) -> MetricDelta:
    va, vb = getattr(a, field), getattr(b, field)
    better = ""
    if va is not None and vb is not None and va != vb:
        b_higher = vb > va
        better = "a" if b_higher == (field in LOWER_IS_BETTER) else "b"
    return MetricDelta(label=lbl, a=fmt(va, kind), b=fmt(vb, kind), better=better)


def comparison_markdown(c: Comparison) -> str:
    lines = [
        "# Eval comparison",
        "",
        f"- **A:** {c.a_label}",
        f"- **B:** {c.b_label}",
        "",
        *[f"> Warning: {w}" for w in c.warnings],
        "",
        "| Metric | A | B | Better |",
        "|---|---|---|---|",
        *[f"| {d.label} | {d.a} | {d.b} | {d.better.upper() or '='} |" for d in c.metrics],
        "",
        f"**Fixed in B ({len(c.fixed)}):** {', '.join(c.fixed) or 'none'}",
        "",
        f"**Broken in B ({len(c.broken)}):** {', '.join(c.broken) or 'none'}",
    ]
    return "\n".join(lines) + "\n"
