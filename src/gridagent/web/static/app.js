/* GridAgent website: plain JavaScript, no framework, no build step.
 *
 * Structure: helpers -> API client -> renderers (answer, chart, method) ->
 * ask flow + recent questions -> router and setup.
 * Security: all text from the server is escaped before it is inserted; the
 * small Markdown renderer only adds formatting tags afterwards.
 */
"use strict";

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

const formatNumber = (v, digits = 1) => (v === null || v === undefined ? "n/a"
  : Number(v).toLocaleString("en-GB", { maximumFractionDigits: digits }));

/** Inline Markdown on already-escaped text: `code`, **bold**, *italic*, [links](http...). */
function inlineMd(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

/** Minimal Markdown for answers: paragraphs, bullet lists (with wrapped lines), headings. */
function markdown(text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  const out = [];
  const bullet = /^\s*([-*]|\d+\.) /;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^#{1,4} /.test(line)) {
      out.push(`<p><strong>${inlineMd(esc(line.replace(/^#+ /, "")))}</strong></p>`);
      i += 1;
    } else if (bullet.test(line)) {
      const items = [];
      while (i < lines.length && bullet.test(lines[i])) {
        let item = lines[i].replace(bullet, "");
        i += 1;
        while (i < lines.length && /^\s+\S/.test(lines[i]) && !bullet.test(lines[i])) {
          item += ` ${lines[i].trim()}`;
          i += 1;
        }
        items.push(inlineMd(esc(item)));
      }
      out.push(`<ul>${items.map((it) => `<li>${it}</li>`).join("")}</ul>`);
    } else if (line.trim() === "") {
      i += 1;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() !== "" && !bullet.test(lines[i]) && !/^#{1,4} /.test(lines[i])) {
        para.push(lines[i++]);
      }
      out.push(`<p>${inlineMd(esc(para.join(" ")))}</p>`);
    }
  }
  return out.join("\n");
}

function toast(message) {
  let el = $(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

// ---------- API ----------

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let body = null;
  try { body = await response.json(); } catch { /* no JSON body */ }
  if (!response.ok) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = "Please enter a question between 3 and 500 characters.";
    throw new Error(detail || "Something went wrong. Please try again.");
  }
  return body;
}

// ---------- state ----------

const state = { config: null, history: [], busy: false };

// ---------- chart ----------

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

const resolutionLabel = (minutes) => {
  if (!minutes) return "";
  if (minutes % 1440 === 0) return `${minutes / 1440}-day averages`;
  if (minutes % 60 === 0) return minutes === 60 ? "hourly averages" : `${minutes / 60}-hour averages`;
  return minutes === 15 ? "15-minute values" : `${minutes}-minute averages`;
};

function chartHtml(chart, index) {
  if (chart.points.filter((p) => p.v !== null).length < 2) return "";
  const { W, H, L, R, T, B, times, lo, hi, t0, t1, x, y } = chartGeometry(chart);

  let path = "", started = false, first = null, last = null;
  chart.points.forEach((p, i) => {
    if (p.v === null) { started = false; return; }
    const px = x(times[i]).toFixed(1), py = y(p.v).toFixed(1);
    path += `${started ? "L" : "M"}${px},${py} `;
    if (first === null) first = px;
    last = px;
    started = true;
  });
  const base = y(Math.max(lo, Math.min(0, hi))).toFixed(1);
  const area = `${path} L${last},${base} L${first},${base} Z`;
  const ticks = [0, 1, 2, 3].map((k) => lo + ((hi - lo) * k) / 3);
  const grid = ticks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/>`).join("");
  const yLabels = ticks.map((v) => `<text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${esc(formatNumber(v, Math.abs(hi - lo) < 10 ? 1 : 0))}</text>`).join("");
  const xLabels = [0, 0.5, 1].map((f) => {
    const t = t0 + (t1 - t0) * f;
    const label = new Date(t).toLocaleString("en-GB", (t1 - t0) > 2 * 86400e3
      ? { day: "numeric", month: "short" } : { hour: "2-digit", minute: "2-digit" });
    return `<text x="${x(t)}" y="${H - 12}" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}">${esc(label)}</text>`;
  }).join("");
  const zero = lo < 0 && hi > 0 ? `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>` : "";

  return `<div class="card">
    <div class="chart-head">
      <div><h3>${esc(chart.title)}</h3>
        <div class="sub">${esc(chart.period)} · ${esc(chart.unit)} · ${esc(resolutionLabel(chart.resolution_minutes))}</div></div>
      <button class="btn btn-small" data-csv="${index}" title="Download the plotted values as CSV">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m0 0-4-4m4 4 4-4M5 20h14"/></svg>CSV</button>
    </div>
    <div class="chart-wrap chart" data-chart="${index}">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(chart.title)}, ${esc(chart.period)}">
        <g class="grid">${grid}</g>${zero}
        <path class="area" d="${area}"/><path class="line" d="${path}"/>
        <g class="axis">${yLabels}${xLabels}</g>
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
    const { W, H, L, R, times, t0, t1, x, y } = chartGeometry(chart);
    rect.addEventListener("mousemove", (event) => {
      const box = svg.getBoundingClientRect();
      const t = t0 + ((((event.clientX - box.left) / box.width) * W - L) / (W - L - R)) * (t1 - t0);
      let best = 0;
      times.forEach((tt, i) => { if (Math.abs(tt - t) < Math.abs(times[best] - t)) best = i; });
      const p = chart.points[best];
      if (p.v === null) return;
      const px = x(times[best]), py = y(p.v);
      cursor.setAttribute("x1", px); cursor.setAttribute("x2", px); cursor.setAttribute("visibility", "visible");
      dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.style.left = `${(px / W) * box.width}px`;
      tip.style.top = `${(py / H) * box.height}px`;
      tip.textContent = `${formatNumber(p.v, 2)} ${chart.unit} · ${new Date(p.t).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
    });
    rect.addEventListener("mouseleave", () => {
      tip.hidden = true;
      cursor.setAttribute("visibility", "hidden");
      dot.setAttribute("visibility", "hidden");
    });
  });
  container.querySelectorAll("[data-csv]").forEach((button) => {
    button.addEventListener("click", () => downloadCsv(charts[Number(button.dataset.csv)]));
  });
}

function downloadCsv(chart) {
  const header = `timestamp (German local time),${chart.title} [${chart.unit}]`;
  const rows = chart.points.map((p) => `${p.t},${p.v === null ? "" : p.v.toFixed(3)}`);
  const note = `# Source: Energy-Charts.info (Fraunhofer ISE), CC BY 4.0. ${resolutionLabel(chart.resolution_minutes)}; timestamps mark the start of each interval.`;
  const blob = new Blob([`${note}\n${header}\n${rows.join("\n")}\n`], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `gridagent_${chart.title.toLowerCase().replace(/[^a-z0-9]+/g, "_")}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// ---------- answer ----------

function answerHtml(result) {
  const asked = new Date(result.asked_at).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const charts = result.charts.map((c, i) => chartHtml(c, i)).join("");
  const method = result.steps.length || result.sources.length ? `
    <details class="card method">
      <summary>How this was calculated</summary>
      <div class="method-body">
        ${result.steps.length ? `<ol class="steps">${result.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` : ""}
        ${result.sources.length ? `<div class="sources">${result.sources.map((s) => `
          <div class="source"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>
          <span class="lic">Licence: ${esc(s.license)}</span></div>`).join("")}</div>` : ""}
      </div>
    </details>` : "";
  return `
    <article class="card answer ${esc(result.status)}">
      <p class="question">${esc(result.question)}</p>
      <div class="text">${markdown(result.answer)}</div>
      <div class="answer-actions">
        <span class="muted">Asked ${esc(asked)} · reference ${esc(result.id)}</span>
        <button class="btn btn-small" id="copy-answer">Copy answer</button>
      </div>
    </article>
    ${charts}
    ${method}`;
}

function showResult(result) {
  const container = $("#result");
  container.innerHTML = answerHtml(result);
  wireCharts(container, result.charts);
  $("#copy-answer").addEventListener("click", async () => {
    const sources = result.sources.map((s) => `${s.name} (${s.url}), ${s.license}`).join("; ");
    const text = `${result.question}\n\n${result.answer}${sources ? `\n\nSources: ${sources}` : ""}`;
    try { await navigator.clipboard.writeText(text); toast("Answer copied to clipboard."); }
    catch { toast("Copying is not allowed in this browser."); }
  });
}

// ---------- ask flow ----------

async function ask(question) {
  question = question.trim();
  if (state.busy) return;
  if (question.length < 3) { toast("Please type a question first."); return; }
  state.busy = true;
  $("#ask-btn").disabled = true;
  $("#result").innerHTML = `<div class="card"><div class="loading-card"><div class="spinner"></div>
    <span>Retrieving the data and calculating the answer…</span></div>
    <div class="skeleton" style="width:92%"></div><div class="skeleton" style="width:74%"></div></div>`;
  try {
    const result = await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
    showResult(result);
    state.history.unshift(result);
    renderHistory();
  } catch (error) {
    $("#result").innerHTML = `<article class="card answer failed"><p class="question">${esc(question)}</p>
      <div class="text"><p>${esc(error.message)}</p></div></article>`;
  } finally {
    state.busy = false;
    $("#ask-btn").disabled = !state.config.available;
  }
}

function renderHistory() {
  const previous = state.history.slice(1);
  $("#history-block").hidden = previous.length === 0;
  $("#history").innerHTML = previous.map((r, i) => `<li><button data-history="${i + 1}">
    <span>${esc(r.question)}</span>
    <span class="time">${esc(new Date(r.asked_at).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }))}</span>
  </button></li>`).join("");
}

// ---------- router & setup ----------

function route() {
  const page = location.hash.startsWith("#/about") ? "about" : "ask";
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${page}`; });
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === page));
  window.scrollTo(0, 0);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("gridagent-theme", theme); } catch { /* storage blocked */ }
}

async function init() {
  let theme = "light";
  try { theme = localStorage.getItem("gridagent-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch { /* ignore */ }
  applyTheme(theme);
  $("#theme-toggle").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  window.addEventListener("hashchange", route);
  route();

  try {
    state.config = await api("/api/config");
  } catch {
    state.config = { available: false, examples: [] };
  }
  $("#unavailable").hidden = state.config.available;
  $("#ask-btn").disabled = !state.config.available;
  if (state.config.recorded_data) {
    $("#footer-note").textContent = `Using recorded data up to ${state.config.today}`;
  }
  $("#examples").innerHTML = state.config.examples
    .map((q) => `<button class="chip" type="button" data-q="${esc(q)}">${esc(q)}</button>`).join("");

  $("#ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    ask($("#question").value);
  });
  $("#examples").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    $("#question").value = chip.dataset.q;
    if (state.config.available) ask(chip.dataset.q);
  });
  $("#history").addEventListener("click", (event) => {
    const button = event.target.closest("[data-history]");
    if (button) {
      showResult(state.history[Number(button.dataset.history)]);
      $("#result").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
}

init();
