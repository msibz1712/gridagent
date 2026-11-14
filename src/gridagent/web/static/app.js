/* GridAgent front end: plain JavaScript, no framework, no build step.
 *
 * Structure: small helpers -> API client -> shared renderers (answer, KPIs,
 * chart, trace) -> one function per page (ask, runs, evals, agents) -> router.
 * Security: all text from the server (model answers, READMEs) is escaped
 * before it is inserted; the tiny Markdown renderer only adds tags afterwards.
 */
"use strict";

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

const fmt = {
  usd: (v) => (v === null || v === undefined ? "n/a" : `$${Number(v).toFixed(4)}`),
  pct: (v) => (v === null || v === undefined ? "n/a" : `${(v * 100).toFixed(1)}%`),
  sec: (v) => (v === null || v === undefined ? "n/a" : `${Number(v).toFixed(1)} s`),
  int: (v) => Number(v || 0).toLocaleString("en-US"),
  num: (v, digits = 1) => (v === null || v === undefined ? "n/a"
    : Number(v).toLocaleString("en-US", { maximumFractionDigits: digits })),
  time: (iso) => new Date(iso).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  }),
};

/** Inline Markdown on already-escaped text: `code`, **bold**, *italic*, [links](http...). */
function inlineMd(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

/** Minimal block Markdown: headings, paragraphs, lists, tables, code fences, quotes. */
function markdown(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i += 1;
      out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
    } else if (/^#{1,4} /.test(line)) {
      const level = line.match(/^#+/)[0].length;
      out.push(`<h${level}>${inlineMd(esc(line.slice(level + 1)))}</h${level}>`);
      i += 1;
    } else if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inlineMd(esc(c.trim())));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    } else if (/^\s*([-*]|\d+\.) /.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*]|\d+\.) /, "");
        i += 1;
        // Indented lines that are not a new bullet continue the current item.
        while (i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*([-*]|\d+\.) /.test(lines[i])) {
          item += ` ${lines[i].trim()}`;
          i += 1;
        }
        items.push(inlineMd(esc(item)));
      }
      out.push(`<ul>${items.map((it) => `<li>${it}</li>`).join("")}</ul>`);
    } else if (line.startsWith(">")) {
      out.push(`<div class="note">${inlineMd(esc(line.replace(/^>\s?/, "")))}</div>`);
      i += 1;
    } else if (line.trim() === "") {
      i += 1;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() !== "" && !/^(#|```|>|\s*\||\s*([-*]|\d+\.) )/.test(lines[i])) {
        para.push(lines[i++]);
      }
      out.push(`<p>${inlineMd(esc(para.join(" ")))}</p>`);
    }
  }
  return out.join("\n");
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

const loadingHtml = (text) => `<div class="card loading"><div class="spinner"></div><span>${esc(text)}</span></div>`;

const STATUS = {
  ok: ["ok", "Answered"],
  refused: ["warn", "Declined"],
  max_steps: ["bad", "Step limit"],
  timeout: ["bad", "Timed out"],
  error: ["bad", "Error"],
};
const statusBadge = (status) => {
  const [cls, label] = STATUS[status] || ["neutral", status];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
};

// ---------- API ----------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await response.json(); } catch { /* no JSON body */ }
  if (!response.ok) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new Error(detail || `Request failed (HTTP ${response.status})`);
  }
  return body;
}

// ---------- state ----------

const state = {
  config: null,
  mode: "live",
  selectedRun: null,
  selectedReport: null,
  selectedAgent: null,
};

const currentAgent = () => state.config.agents.find((a) => a.spec === $("#agent-select").value);

function updateBudget(budget) {
  if (!budget) return;
  const pill = $("#spend-pill");
  pill.textContent = `$${budget.spent_usd.toFixed(4)} / $${budget.limit_usd.toFixed(2)}`;
  pill.classList.toggle("warn", budget.spent_usd >= 0.8 * budget.limit_usd);
}

// ---------- shared renderers ----------

function kpiHtml(items) {
  return `<div class="kpis">${items.map(([label, value, sub]) => `
    <div class="kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>
    ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("")}</div>`;
}

function runKpis(run) {
  const u = run.usage;
  const tokensIn = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens;
  return kpiHtml([
    ["Steps", String(run.steps.length), `${run.steps.flatMap((s) => s.tool_calls).length} tool calls`],
    ["Latency", fmt.sec(run.latency_s), "end to end"],
    ["Tokens", `${fmt.int(tokensIn)} / ${fmt.int(u.output_tokens)}`, `in / out · ${fmt.int(u.cache_read_input_tokens)} cached`],
    ["Est. cost", fmt.usd(run.cost_usd), run.model],
  ]);
}

function answerHtml(run) {
  return `<div class="card answer-card status-${esc(run.status)}">
    <div class="answer-head">${statusBadge(run.status)}<span class="q">${esc(run.question)}</span></div>
    <div class="answer-text">${markdown(run.final_answer || "(no answer)")}</div>
    ${run.error ? `<div class="error-box">${esc(run.error)}</div>` : ""}
    <div class="answer-foot">
      <span>${esc(run.agent)}@${esc(run.agent_version)}</span>
      <span>model ${esc(run.model)}</span>
      <span>today = ${esc(run.today)}</span>
      <span>run ${esc(run.run_id)}</span>
    </div></div>`;
}

function traceHtml(run) {
  const items = [];
  for (const step of run.steps) {
    const u = step.usage;
    const tokensIn = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens;
    items.push(`<li class="tl-item model"><span class="tl-dot"></span>
      <div class="tl-title">Model call ${step.step}
        <span class="tl-meta">${esc(step.model)} · ${fmt.sec(step.latency_s)} · ${fmt.int(tokensIn)} in / ${fmt.int(u.output_tokens)} out · ${fmt.usd(step.cost_usd)}</span>
      </div>
      <div class="tl-args">stop_reason = ${esc(step.stop_reason)}</div></li>`);
    for (const call of step.tool_calls) {
      let pretty = call.result;
      try { pretty = JSON.stringify(JSON.parse(call.result), null, 2); } catch { /* plain text */ }
      items.push(`<li class="tl-item tool ${call.is_error ? "error" : ""}"><span class="tl-dot"></span>
        <div class="tl-title">${esc(call.name)} ${call.is_error ? '<span class="badge bad">error</span>' : ""}
          <span class="tl-meta">${fmt.sec(call.latency_s)}</span></div>
        <div class="tl-args">${esc(JSON.stringify(call.input))}</div>
        <details class="tl-result"><summary>Result the model saw</summary><pre class="code">${esc(pretty)}</pre></details></li>`);
    }
  }
  return `<div class="card"><h2>Trace</h2><ol class="timeline">${items.join("")}</ol></div>`;
}

/** Scales shared by drawing and hovering: value range, time range, x()/y(). */
function chartGeometry(chart) {
  const g = { W: 760, H: 280, L: 70, R: 16, T: 16, B: 38 };
  g.times = chart.points.map((p) => new Date(p.t).getTime());
  const values = chart.points.filter((p) => p.v !== null).map((p) => p.v);
  let lo = Math.min(...values), hi = Math.max(...values);
  if (lo > 0 && lo < hi * 0.35) lo = 0; // start at zero unless that wastes the chart
  const pad = (hi - lo) * 0.08 || 1;
  g.lo = lo === 0 ? 0 : lo - pad;
  g.hi = hi + pad;
  g.t0 = g.times[0];
  g.t1 = g.times[g.times.length - 1];
  g.x = (t) => g.L + ((t - g.t0) / (g.t1 - g.t0 || 1)) * (g.W - g.L - g.R);
  g.y = (v) => g.T + (1 - (v - g.lo) / (g.hi - g.lo)) * (g.H - g.T - g.B);
  return g;
}

/** A dependency-free SVG line chart with a hover tooltip. */
function chartHtml(chart, index) {
  if (chart.points.filter((p) => p.v !== null).length < 2) return "";
  const { W, H, L, R, T, B, times, lo, hi, t0, t1, x, y } = chartGeometry(chart);

  let path = "", area = "", started = false, first = null, last = null;
  chart.points.forEach((p, i) => {
    if (p.v === null) { started = false; return; }
    const px = x(times[i]).toFixed(1), py = y(p.v).toFixed(1);
    path += `${started ? "L" : "M"}${px},${py} `;
    if (first === null) first = px;
    last = px;
    started = true;
  });
  const base = y(Math.max(lo, Math.min(0, hi)));
  area = `${path} L${last},${base.toFixed(1)} L${first},${base.toFixed(1)} Z`;

  const ticks = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3);
  const yAxis = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`).join("");
  const yLabels = ticks.map((v) => `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(fmt.num(v, Math.abs(hi - lo) < 10 ? 1 : 0))}</text>`).join("");
  const xLabelAt = [0, 0.5, 1].map((f) => {
    const t = t0 + (t1 - t0) * f;
    const label = new Date(t).toLocaleString("en-GB", (t1 - t0) > 2 * 86400e3
      ? { day: "2-digit", month: "short" } : { hour: "2-digit", minute: "2-digit" });
    return `<text x="${x(t)}" y="${H - 10}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${esc(label)}</text>`;
  }).join("");
  const zero = lo < 0 && hi > 0 ? `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>` : "";

  return `<div class="card chart-card"><h3><span>${esc(chart.title)}</span><span class="muted small">${esc(chart.period)} · ${esc(chart.unit)}</span></h3>
    <div class="chart-wrap chart" data-chart="${index}">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(chart.title)}">
        <g class="grid">${yAxis}</g>${zero}
        <path class="area" d="${area}"/><path class="line" d="${path}"/>
        <g class="axis">${yLabels}${xLabelAt}</g>
        <line class="cursor" y1="${T}" y2="${H - B}" visibility="hidden"/>
        <circle class="dot" r="4" visibility="hidden"/>
        <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>
      </svg>
      <div class="chart-tip" hidden></div>
    </div></div>`;
}

function wireCharts(container, charts) {
  container.querySelectorAll("[data-chart]").forEach((wrap) => {
    const chart = charts[Number(wrap.dataset.chart)];
    const svg = wrap.querySelector("svg");
    const rect = svg.querySelector("rect");
    const cursor = svg.querySelector(".cursor");
    const dot = svg.querySelector(".dot");
    const tip = wrap.querySelector(".chart-tip");
    const { W, H, L, R, T, B, times, lo, hi, t0, t1, x, y } = chartGeometry(chart);
    rect.addEventListener("mousemove", (event) => {
      const box = svg.getBoundingClientRect();
      const sx = ((event.clientX - box.left) / box.width) * W;
      const t = t0 + ((sx - L) / (W - L - R)) * (t1 - t0);
      let best = 0;
      times.forEach((tt, i) => { if (Math.abs(tt - t) < Math.abs(times[best] - t)) best = i; });
      const p = chart.points[best];
      if (p.v === null) return;
      const px = x(times[best]);
      const py = y(p.v);
      cursor.setAttribute("x1", px); cursor.setAttribute("x2", px); cursor.setAttribute("visibility", "visible");
      dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.style.left = `${(px / W) * box.width}px`;
      tip.style.top = `${(py / H) * box.height}px`;
      tip.textContent = `${fmt.num(p.v, 2)} ${chart.unit} · ${new Date(p.t).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}`;
    });
    rect.addEventListener("mouseleave", () => {
      tip.hidden = true;
      cursor.setAttribute("visibility", "hidden");
      dot.setAttribute("visibility", "hidden");
    });
  });
}

function renderRunInto(container, payload) {
  const { run, charts = [] } = payload;
  const chartBlock = charts.map((c, i) => chartHtml(c, i)).join("")
    || `<div class="card empty small">No chart: this answer did not analyse a data series.</div>`;
  container.innerHTML = `${answerHtml(run)}${runKpis(run)}
    <div class="grid-2"><div>${chartBlock}</div><div>${traceHtml(run)}</div></div>`;
  wireCharts(container, charts);
}

// ---------- page: ask ----------

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".seg").forEach((b) => {
    const active = b.dataset.mode === mode;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", String(active));
  });
  $("#live-panel").hidden = mode !== "live";
  $("#demo-panel").hidden = mode !== "demo";
  if (mode === "demo") loadDemoCases();
}

function refreshAskPanel() {
  const agent = currentAgent();
  const model = state.config.models.find((m) => m.id === $("#model-select").value);
  const hasKey = state.config.has_api_key;
  $("#key-status").innerHTML = hasKey
    ? '<span class="badge ok">API key loaded</span>'
    : '<span class="badge warn">No API key on the server: use the free demo</span>';
  $("#ask-btn").disabled = !hasKey;
  $("#cost-hint").textContent = model && model.est_cost_per_question_usd
    ? `≈ ${fmt.usd(model.est_cost_per_question_usd)} max per question on ${model.id}` : "";
  $("#example-chips").innerHTML = (agent.examples || [])
    .map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("");
}

async function askLive() {
  const question = $("#question").value.trim();
  if (question.length < 3) { toast("Type a question first."); return; }
  const result = $("#result");
  $("#ask-btn").disabled = true;
  result.innerHTML = loadingHtml(`Asking ${$("#model-select").value}: calling tools and computing…`);
  try {
    const payload = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        agent: $("#agent-select").value,
        question,
        model: $("#model-select").value,
        use_fixtures: $("#fixtures-toggle").checked,
      }),
    });
    renderRunInto(result, payload);
    updateBudget(payload.budget);
  } catch (error) {
    result.innerHTML = "";
    toast(error.message, true);
  } finally {
    $("#ask-btn").disabled = !state.config.has_api_key;
  }
}

async function loadDemoCases() {
  const list = $("#demo-list");
  list.innerHTML = loadingHtml("Loading eval questions…");
  try {
    const cases = await api(`/api/demo-cases?agent=${encodeURIComponent($("#agent-select").value)}`);
    list.innerHTML = cases.map((c) => `<button class="demo-item" data-case="${esc(c.id)}">
      <span class="badge neutral">${esc(c.category.replace("_", " "))}</span>${esc(c.question)}</button>`).join("");
  } catch (error) {
    list.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

async function runDemo(caseId) {
  const result = $("#result");
  result.innerHTML = loadingHtml("Running the oracle on recorded data…");
  try {
    const payload = await api("/api/demo", {
      method: "POST",
      body: JSON.stringify({ agent: $("#agent-select").value, case_id: caseId }),
    });
    renderRunInto(result, payload);
    result.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    result.innerHTML = "";
    toast(error.message, true);
  }
}

// ---------- page: runs ----------

async function loadRuns() {
  $("#runs-kpis").innerHTML = "";
  $("#runs-table").innerHTML = loadingHtml("Loading runs…");
  try {
    const data = await api("/api/runs");
    const s = data.summary;
    $("#runs-kpis").innerHTML = kpiHtml([
      ["Runs", fmt.int(s.n_runs), "most recent 200"],
      ["Answered", fmt.pct(s.ok_rate), "status ok"],
      ["Errors", fmt.pct(s.error_rate), "error, timeout or step limit"],
      ["Latency p50 / p95", `${fmt.num(s.latency_p50_s)} / ${fmt.num(s.latency_p95_s)} s`, "end to end"],
      ["Total cost", fmt.usd(s.total_cost_usd), "estimated"],
    ]);
    if (!data.runs.length) {
      $("#runs-table").innerHTML = '<div class="empty">No runs yet. Ask a question on the Ask page.</div>';
      return;
    }
    state.runs = data.runs;
    $("#runs-table").innerHTML = `<table><thead><tr>
      <th>Time</th><th>Status</th><th>Question</th><th>Agent</th><th>Model</th>
      <th class="num">Steps</th><th class="num">Latency</th><th class="num">Cost</th></tr></thead><tbody>
      ${data.runs.map((r, i) => `<tr class="clickable" data-run="${i}">
        <td class="nowrap">${esc(fmt.time(r.run.started_at))}</td><td>${statusBadge(r.status)}</td>
        <td class="q">${esc(r.question)}</td><td class="nowrap">${esc(r.agent)}</td><td class="nowrap">${esc(r.model)}</td>
        <td class="num">${r.steps}</td><td class="num">${fmt.sec(r.seconds)}</td><td class="num">${fmt.usd(r.cost_usd)}</td>
      </tr>`).join("")}</tbody></table>`;
  } catch (error) {
    $("#runs-table").innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

function showRun(index) {
  document.querySelectorAll("#runs-table tr.selected").forEach((tr) => tr.classList.remove("selected"));
  document.querySelector(`#runs-table tr[data-run="${index}"]`)?.classList.add("selected");
  const detail = $("#run-detail");
  const run = state.runs[index].run;
  detail.innerHTML = `${answerHtml(run)}${runKpis(run)}${traceHtml(run)}`;
  detail.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------- page: evaluations ----------

async function loadReports(selectName) {
  const list = $("#report-list");
  list.innerHTML = loadingHtml("Loading reports…");
  try {
    const reports = await api("/api/reports");
    state.reports = reports;
    const options = reports.map((r) => `<option value="${esc(r.name)}">${esc(`${r.agent} · ${r.model} · ${r.mode} · ${fmt.time(r.created_at)}`)}</option>`).join("");
    $("#compare-a").innerHTML = options;
    $("#compare-b").innerHTML = options;
    if (reports.length > 1) $("#compare-a").selectedIndex = 1;
    if (!reports.length) {
      list.innerHTML = '<div class="empty">No reports yet. Click “Run offline eval”.</div>';
      $("#report-detail").innerHTML = "";
      return;
    }
    list.innerHTML = reports.map((r) => {
      const cls = r.pass_rate >= 0.9 ? "ok" : r.pass_rate >= 0.7 ? "warn" : "bad";
      return `<button class="report-item" data-report="${esc(r.name)}">
        <div class="ri-top"><span>${esc(r.agent)}</span><span class="badge ${cls}">${fmt.pct(r.pass_rate)}</span></div>
        <div class="ri-sub">${esc(r.model)} · ${esc(r.mode)} · ${esc(fmt.time(r.created_at))}</div></button>`;
    }).join("");
    showReport(selectName || reports[0].name);
  } catch (error) {
    list.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

async function showReport(name) {
  document.querySelectorAll(".report-item").forEach((b) => b.classList.toggle("active", b.dataset.report === name));
  const detail = $("#report-detail");
  detail.innerHTML = loadingHtml("Loading report…");
  try {
    const data = await api(`/api/reports/${encodeURIComponent(name)}`);
    const report = data.report;
    const metric = (key) => data.metrics.find((m) => m.key === key);
    const cats = {};
    report.cases.forEach((c) => {
      cats[c.category] = cats[c.category] || { pass: 0, total: 0 };
      cats[c.category].total += 1;
      if (c.passed) cats[c.category].pass += 1;
    });
    detail.innerHTML = `
      ${data.notes.map((n) => `<div class="note">${esc(n)}</div>`).join("")}
      ${kpiHtml([
        ["Pass rate", metric("pass_rate").display, `${data.n_cases} cases`],
        ["Answer accuracy", metric("answer_accuracy").display, "numbers ±1% + dates"],
        ["Grounded", metric("grounding_rate").display, "numbers in tool results"],
        ["Refusal correctness", metric("refusal_correctness").display, "declines exactly when it should"],
      ])}
      <div class="grid-2">
        <div class="card"><h2>All metrics</h2><table><tbody>
          ${data.metrics.map((m) => `<tr><td>${esc(m.label)}</td><td class="num">${esc(m.display)}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="card"><h2>By category</h2><div class="cat-bars">
          ${Object.entries(cats).map(([cat, v]) => `<div class="cat-row"><span>${esc(cat.replace("_", " "))}</span>
            <div class="bar"><span style="width:${(100 * v.pass) / v.total}%"></span></div>
            <span class="muted">${v.pass}/${v.total}</span></div>`).join("")}
        </div>
        <p class="muted small" style="margin-top:14px">${esc(report.agent)}@${esc(report.agent_version)} · ${esc(report.model)} · eval set ${esc(report.eval_set)} v${esc(report.eval_set_version)} (${esc(report.eval_set_hash)}) · data today = ${esc(report.today)}</p></div>
      </div>
      <div class="card table-card"><div class="table-wrap"><table><thead><tr>
        <th>Case</th><th>Category</th><th>Result</th><th>Failed checks</th><th class="num">Steps</th><th class="num">Latency</th><th class="num">Cost</th>
      </tr></thead><tbody>
        ${report.cases.map((c) => `<tr><td><strong>${esc(c.id)}</strong><div class="muted small">${esc(c.question)}</div></td>
          <td>${esc(c.category.replace("_", " "))}</td>
          <td>${c.passed ? '<span class="badge ok">pass</span>' : '<span class="badge bad">fail</span>'}</td>
          <td>${esc(c.failed_checks.join(", ") || "–")}</td>
          <td class="num">${c.steps}</td><td class="num">${fmt.sec(c.latency_s)}</td><td class="num">${fmt.usd(c.cost_usd)}</td></tr>`).join("")}
      </tbody></table></div></div>`;
  } catch (error) {
    detail.innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
  }
}

async function runOfflineEval() {
  const button = $("#eval-run");
  button.disabled = true;
  $("#report-detail").innerHTML = loadingHtml("Running 29 eval questions offline (free)…");
  try {
    const summary = await api("/api/evals/run", {
      method: "POST",
      body: JSON.stringify({ agent: $("#agent-select").value, model: $("#model-select").value }),
    });
    toast(`Eval finished: pass rate ${fmt.pct(summary.pass_rate)} (${summary.mode} mode)`);
    await loadReports(summary.name);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function compareReports() {
  const a = $("#compare-a").value, b = $("#compare-b").value;
  const box = $("#compare-result");
  if (!a || !b) { box.innerHTML = '<div class="empty">Need two reports.</div>'; return; }
  box.innerHTML = loadingHtml("Comparing…");
  try {
    const c = await api(`/api/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    box.innerHTML = `${c.warnings.map((w) => `<div class="note warn">${esc(w)}</div>`).join("")}
      <div class="table-wrap"><table><thead><tr><th>Metric</th><th>A: ${esc(c.a_label)}</th><th>B: ${esc(c.b_label)}</th><th>Better</th></tr></thead><tbody>
      ${c.metrics.map((m) => `<tr><td>${esc(m.label)}</td><td class="num">${esc(m.a)}</td><td class="num">${esc(m.b)}</td>
        <td class="${m.better ? `better-${m.better}` : "muted"}">${m.better ? m.better.toUpperCase() : "="}</td></tr>`).join("")}
      </tbody></table></div>
      <p><strong>Fixed in B:</strong> ${esc(c.fixed.join(", ") || "none")}<br><strong>Broken in B:</strong> ${esc(c.broken.join(", ") || "none")}</p>`;
  } catch (error) {
    box.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
  }
}

// ---------- page: agents ----------

function renderAgentGrid() {
  const byName = {};
  state.config.agents.forEach((a) => { (byName[a.name] = byName[a.name] || []).push(a); });
  $("#agent-grid").innerHTML = Object.entries(byName).map(([name, versions]) => {
    const latest = versions[0];
    return `<div class="card agent-card" data-agent="${esc(name)}">
      <div class="ac-top"><span class="ac-name">${esc(name)}</span><span class="badge ok">valid</span></div>
      <p>${esc(latest.description)}</p>
      <div class="tags">
        <span class="badge neutral">v${esc(latest.version)} latest</span>
        <span class="badge neutral">${versions.length} version${versions.length > 1 ? "s" : ""}</span>
        <span class="badge neutral">${esc(latest.model)}</span>
        <span class="badge neutral">owner ${esc(latest.owner)}</span>
      </div></div>`;
  }).join("");
  const first = Object.keys(byName)[0];
  if (first) showAgent(state.selectedAgent || first);
}

async function showAgent(name) {
  state.selectedAgent = name;
  document.querySelectorAll(".agent-card").forEach((c) => c.classList.toggle("active", c.dataset.agent === name));
  const detail = $("#agent-detail");
  detail.innerHTML = loadingHtml("Loading agent…");
  try {
    const data = await api(`/api/agents/${encodeURIComponent(name)}`);
    const versionBlock = (v) => (v.valid
      ? `<h3>agent.yaml</h3><pre class="code">${esc(v.config_yaml)}</pre>
         <details class="tl-result" style="margin-top:12px"><summary>System prompt</summary><div class="markdown">${markdown(v.system_prompt)}</div></details>`
      : `<div class="error-box">${esc(v.error)}</div>`);
    detail.innerHTML = `<div class="grid-2">
      <div class="card markdown">${markdown(data.readme)}</div>
      <div class="card"><h2>Versions</h2>
        <div class="version-tabs">${data.versions.map((v, i) => `<button class="chip ${i === 0 ? "active" : ""}" data-version="${i}">v${esc(v.version)}${i === 0 ? " · latest" : ""}</button>`).join("")}</div>
        <div id="version-body">${versionBlock(data.versions[0])}</div></div></div>`;
    detail.querySelectorAll("[data-version]").forEach((button) => button.addEventListener("click", () => {
      detail.querySelectorAll("[data-version]").forEach((b) => b.classList.toggle("active", b === button));
      $("#version-body").innerHTML = versionBlock(data.versions[Number(button.dataset.version)]);
    }));
  } catch (error) {
    detail.innerHTML = `<div class="card empty">${esc(error.message)}</div>`;
  }
}

// ---------- router & setup ----------

const ROUTES = {
  ask: () => {},
  runs: loadRuns,
  evals: () => loadReports(),
  agents: renderAgentGrid,
};

function route() {
  const name = (location.hash.replace(/^#\//, "") || "ask").split("?")[0];
  const page = ROUTES[name] ? name : "ask";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${page}`; });
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === page));
  ROUTES[page]();
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("gridagent-theme", theme); } catch { /* storage blocked */ }
}

function populateControls() {
  const agentSelect = $("#agent-select");
  agentSelect.innerHTML = state.config.agents
    .map((a) => `<option value="${esc(a.spec)}">${esc(a.spec)}${a === state.config.agents[0] ? " (latest)" : ""}</option>`).join("");
  syncModelToAgent();
}

function syncModelToAgent() {
  const agent = currentAgent();
  const ids = state.config.models.map((m) => m.id);
  if (!ids.includes(agent.model)) ids.unshift(agent.model);
  $("#model-select").innerHTML = ids
    .map((id) => `<option value="${esc(id)}" ${id === agent.model ? "selected" : ""}>${esc(id)}${id === agent.model ? " (agent default)" : ""}</option>`).join("");
  updateTodayHint();
  refreshAskPanel();
}

function updateTodayHint() {
  const fixtures = $("#fixtures-toggle").checked;
  $("#fixtures-label").textContent = fixtures ? "Recorded (fixtures)" : "Live Energy-Charts API";
  $("#today-hint").textContent = fixtures
    ? `Recorded data: “today” is ${currentAgent().fixture_today}`
    : `Live data: today is ${state.config.today}`;
}

async function init() {
  let theme = "light";
  try { theme = localStorage.getItem("gridagent-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch { /* ignore */ }
  applyTheme(theme);

  try {
    state.config = await api("/api/config");
  } catch (error) {
    $("main").innerHTML = `<div class="card empty">Could not reach the GridAgent server: ${esc(error.message)}</div>`;
    return;
  }
  if (!state.config.agents.length) {
    $("main").innerHTML = '<div class="card empty">No valid agents found. Start the server from the project folder.</div>';
    return;
  }
  $("#footer-version").textContent = `GridAgent ${state.config.version}`;
  updateBudget(state.config.budget);
  populateControls();

  $("#theme-toggle").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#agent-select").addEventListener("change", () => {
    syncModelToAgent();
    if (state.mode === "demo") loadDemoCases();
  });
  $("#model-select").addEventListener("change", refreshAskPanel);
  $("#fixtures-toggle").addEventListener("change", updateTodayHint);
  document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("#ask-btn").addEventListener("click", askLive);
  $("#question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) askLive();
  });
  $("#example-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (chip) { $("#question").value = chip.dataset.q; $("#question").focus(); }
  });
  $("#demo-list").addEventListener("click", (event) => {
    const item = event.target.closest(".demo-item");
    if (!item) return;
    document.querySelectorAll(".demo-item").forEach((d) => d.classList.toggle("active", d === item));
    runDemo(item.dataset.case);
  });
  $("#runs-refresh").addEventListener("click", loadRuns);
  $("#runs-table").addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-run]");
    if (row) showRun(Number(row.dataset.run));
  });
  $("#report-list").addEventListener("click", (event) => {
    const item = event.target.closest(".report-item");
    if (item) showReport(item.dataset.report);
  });
  $("#eval-run").addEventListener("click", runOfflineEval);
  $("#compare-btn").addEventListener("click", compareReports);
  $("#agent-grid").addEventListener("click", (event) => {
    const card = event.target.closest(".agent-card");
    if (card) showAgent(card.dataset.agent);
  });

  window.addEventListener("hashchange", route);
  route();
}

init();
