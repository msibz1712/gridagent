"""The public website: static pages built from saved briefings (no server, no database).

`build_site` turns reports/briefings/*.json into:
    index.html              the latest briefing
    days/<date>.html        every briefing (the archive)
    archive.html            list of all days
    about.html              method, sources, licences, AI notice
    data/<date>.json        the briefing as open data
    style.css

Design rules (from the mock-up): one headline per section that states the finding,
charts as inline SVG (no JavaScript library), two accents (blue = wind and cheap hours,
orange = price and dear hours), system fonts (no Google Fonts: embedding them is a
GDPR problem in Germany), no cookies, no tracking, no external requests.

Text: every caption is written by code (monitor/captions.py). The AI story is shown
only with `ai_story=True`, under a visible label.
"""

import html
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any

from gridagent.monitor.anomalies import Story
from gridagent.monitor.captions import (
    four_weeks_caption,
    headline,
    lead_caption,
    nrw_caption,
    plan_caption,
    week_caption,
)
from gridagent.monitor.daily import Briefing

STATIC = Path(__file__).parent / "static"
BLUE, ORANGE, GREY, INK = "#0066cc", "#b64400", "#d2d2d7", "#1d1d1f"
SITE_NAME = "GridAgent"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B %Y}"


# ---------- charts (inline SVG) ----------


def bars(values: list[float | None], labels: list[str], colors: list[str], height: int = 160,
         unit: str = "", label: str = "") -> str:  # fmt: skip
    """A bar chart scaled to the largest value; hover shows each bar's label and value."""
    known = [v for v in values if v is not None]
    if not known:
        return ""
    top = max(max(known), 1e-9)
    bottom = min(0.0, min(known))
    span = top - bottom
    n, gap = len(values), 2
    width = 1000
    bar_w = (width - gap * (n - 1)) / n
    rects = []
    for i, (v, lab, color) in enumerate(zip(values, labels, colors, strict=True)):
        if v is None:
            continue
        x = i * (bar_w + gap)
        y0 = height * (top / span)  # the zero line
        h = height * abs(v) / span
        y = y0 - h if v >= 0 else y0
        rects.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(h, 1):.1f}" '
                     f'rx="2" fill="{color}"><title>{esc(lab)}: {v:,.2f} {esc(unit)}</title></rect>')  # fmt: skip
    return (f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
            f'role="img" aria-label="{esc(label)}">{"".join(rects)}</svg>')  # fmt: skip


def sparkline(values: list[float], color: str) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * 280 / (len(values) - 1):.1f},{4 + 28 * (1 - (v - lo) / span):.1f}"
                   for i, v in enumerate(values))  # fmt: skip
    last_y = 4 + 28 * (1 - (values[-1] - lo) / span)
    return (f'<svg class="spark" viewBox="0 0 280 36" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{GREY}" stroke-width="1.5" '
            f'vector-effect="non-scaling-stroke"/><circle cx="280" cy="{last_y:.1f}" r="3.5" '
            f'fill="{color}"/></svg>')  # fmt: skip


# ---------- sections ----------

KPIS = [  # figure name in the briefing, label, series in `daily` for the sparkline, decimals
    ("Average day-ahead price", "Average day-ahead price", "price_mean", 2),
    ("Renewable share of public net generation", "Renewable share", "renewable_share_pct", 1),
    ("Wind energy", "Wind energy", "wind_gwh", 1),
    ("Peak residual load (demand minus wind and solar)", "Peak residual load",
     "residual_peak_gw", 1),
]  # fmt: skip


def _kpis(b: dict[str, Any]) -> str:
    figures = {f["name"]: f for f in b["figures"]}
    items = []
    for name, label, key, decimals in KPIS:
        f = figures.get(name)
        if not f or f["value"] is None:
            continue
        if f.get("change_pct") is not None:
            delta = f"{f['change_pct']:+.0f} %"
        elif f.get("difference") is not None:
            delta = f"{f['difference']:+.{decimals}f} {'pts' if f['unit'] == '%' else f['unit']}"
        else:
            delta = ""
        series = [d[key] for d in b.get("daily", []) if key in d]
        items.append(f"""<article class="kpi">
  <h3>{esc(label)}</h3>
  <p class="kpi-value"><span>{f["value"]:,.{decimals}f}</span> {esc(f["unit"])}</p>
  <p class="kpi-delta"><strong>{esc(delta)}</strong> vs {f["normal"]:,.{decimals}f} normal</p>
  {sparkline(series, BLUE)}
  <p class="fine">4 weeks, daily</p>
</article>""")
    return f'<section class="wrap kpis" aria-label="Key figures">{"".join(items)}</section>'


def _lead(b: dict[str, Any], ai_story: bool) -> str:
    story = b.get("story") if ai_story else None
    if story:
        label = ("Written by AI from the data · every figure checked against it"
                 if story["written_by"] == "model" else "Written by code from the data")  # fmt: skip
        body = "".join(f"<p>{esc(p)}</p>" for p in story["paragraphs"])
        brief = "".join(f"<li>{esc(i)}</li>" for i in story.get("in_brief", []))
        # Dashboard first: headline and summary up top, the full story one click away.
        return f"""<section class="wrap hero">
  <p class="eyebrow">Daily briefing · {esc(_day(b["day"]))}</p>
  <h1>{esc(story["headline"])}</h1>
  <p class="lead">{esc(story["standfirst"])}</p>
  <details class="story"><summary>Read the full story</summary>{body}{f'<ul class="brief">{brief}</ul>' if brief else ""}</details>
  <p class="label">{esc(label)}</p>
</section>"""
    lead = Story(**b["events"][0]) if b["events"] else None
    return f"""<section class="wrap hero">
  <p class="eyebrow">Daily briefing · {esc(_day(b["day"]))}</p>
  <h1>{esc(headline(lead))}.</h1>
  <p class="lead">{esc(lead_caption(b))}</p>
</section>"""


def _today(b: dict[str, Any]) -> str:
    if not b["plans"]:
        return ""
    plan = b["plans"][0]
    prices = plan.get("hourly_prices", [])
    cheap = int(plan["cheapest_hours"][:2])
    dear = int(plan["dearest_hours"][:2])
    colors = ["#2997ff" if cheap <= h < cheap + 3 else "#ff7a2f" if dear <= h < dear + 3
              else "#48484a" for h in range(len(prices))]  # fmt: skip
    chart = bars(prices, [f"{h:02d}:00" for h in range(len(prices))], colors, height=220,
                 unit="€/MWh", label=f"Hourly day-ahead prices, {_day(plan['day'])}")  # fmt: skip
    forecast = next((o for o in b.get("outlooks", []) if o["day"] == plan["day"]), None)
    outlook = f"<p class='dim'>Forecast: {esc(forecast['headline'])}.</p>" if forecast else ""
    return f"""<section class="band"><div class="wrap split">
  <div>
    <p class="eyebrow">{esc(_day(plan["day"]))}</p>
    <h2>Use power between <span class="cheap">{esc(plan["cheapest_hours"])}</span>.</h2>
    <p class="dim">{esc(plan_caption(plan))}</p>{outlook}
    <dl class="facts">
      <div><dt>Cheapest 3 hours</dt><dd class="cheap">{plan["cheapest_price"]:,.2f} <small>€/MWh</small></dd></div>
      <div><dt>Dearest 3 hours</dt><dd class="dear">{plan["dearest_price"]:,.2f} <small>€/MWh</small></dd></div>
      <div><dt>1 MW / 2 MWh battery, day-ahead</dt><dd>{plan["battery_profit_eur_per_mw"]:,.2f} <small>€</small></dd></div>
    </dl>
  </div>
  <figure><figcaption>Day-ahead price by hour, €/MWh</figcaption>{chart}
    <div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
  </figure>
</div></section>"""  # noqa: E501


def _price_forecast(b: dict[str, Any]) -> str:
    pf = b.get("price_forecast")
    if not pf:
        return ""
    hours = pf["hourly_eur_mwh"]
    cheap, dear = int(pf["cheapest_hours"][:2]), int(pf["dearest_hours"][:2])
    colors = [BLUE if cheap <= h < cheap + 3 else ORANGE if dear <= h < dear + 3 else GREY
              for h in range(len(hours))]  # fmt: skip
    chart = bars(hours, [f"{h:02d}:00" for h in range(len(hours))], colors, height=180,
                 unit="€/MWh", label=f"Forecast day-ahead price by hour, {_day(pf['date'])}")  # fmt: skip
    err = pf["recent_error"]
    mae = err["mae_eur_mwh"]
    published = ""
    if "published_mean_eur_mwh" in pf:
        published = (
            f"<p class='lead small'>The auction has run: published average "
            f"{pf['published_mean_eur_mwh']:,.2f} €/MWh against our forecast of "
            f"{pf['mean_eur_mwh']:,.2f}; hourly error {pf['mae_vs_published_eur_mwh']:,.2f} €/MWh.</p>"
        )
    check = ""
    if "check_today" in pf:
        c = pf["check_today"]
        check = (
            f"<p class='fine'>Check: our forecast for {esc(_day(c['date']))}, made the day before, averaged "
            f"{c['forecast_mean_eur_mwh']:,.2f} €/MWh; the published prices averaged {c['actual_mean_eur_mwh']:,.2f} "
            f"(hourly error {c['mae_eur_mwh']:,.2f}).</p>"
        )
    return f"""<section class="wrap section">
  <p class="eyebrow">Tomorrow · our forecast</p>
  <h2>{esc(_day(pf["date"]))}: about {pf["mean_eur_mwh"]:,.0f} €/MWh on average.</h2>
  <p class="lead small">Lowest from {esc(pf["cheapest_hours"])} (about {pf["cheapest_eur_mwh"]:,.0f} €/MWh), highest from
  {esc(pf["dearest_hours"])} (about {pf["dearest_eur_mwh"]:,.0f}). Over the last four weeks this forecast was off by
  {mae["forecast"]:,.0f} €/MWh per hour on average; the simple rule "same hour as the day before" by
  {mae["same hour, day before"]:,.0f}.</p>
  {published}
  <figure><figcaption>Forecast day-ahead price by hour <span>€/MWh</span></figcaption>{chart}
    <div class="axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div></figure>
  {check}
  <p class="fine">GridAgent's own estimate, not a published price: expected wind and solar (Open-Meteo, issued two days
  before), demand of the same hour a week earlier, and a merit-order curve fitted on the last four weeks. Gas and CO2
  prices, plant outages and neighbouring markets are not in it, so very expensive days come out too low.</p>
</section>"""


def _week(b: dict[str, Any]) -> str:
    week = b.get("week")
    if not week:
        return ""
    days = week["days"]
    top = max([d["wind_gwh"] for d in days] + [week["normal_wind_gwh"], 1])
    cols = []
    for d in days:
        rough = d["reliability"] == "rough indication"
        height = 100 * d["wind_gwh"] / top
        dt = date.fromisoformat(d["date"])
        cols.append(f"""<li class="day{" rough" if rough else ""}">
  <p class="dname">{"Today" if d["days_ahead"] == 0 else f"{dt:%a}"} <span>{dt.day} {dt:%b}</span></p>
  <div class="wbar" role="img" aria-label="About {d["wind_gwh"]:,} GWh of wind"><div style="height:{height:.0f}%"></div></div>
  <p class="wval">{d["wind_gwh"]:,} <small>GWh wind</small></p>
  <p class="fine">{d["solar_gwh"]:,} GWh solar · {d["temperature_c"]:.1f} °C</p>
  <p class="tag">{esc(d["reliability"])}{" · grid operators" if d.get("source", "").startswith("grid") else ""}</p>
</li>""")
    normal = 100 * week["normal_wind_gwh"] / top
    cal = week["calibration"]
    return f"""<section class="wrap section">
  <p class="eyebrow">The week ahead</p>
  <h2>Wind and sun, the next seven days.</h2>
  <p class="lead small">{esc(week_caption(week))}</p>
  <ol class="week" style="--normal:{normal:.0f}%">{"".join(cols)}</ol>
  <p class="fine">Expected energy from Open-Meteo weather forecasts (CC BY 4.0), turned into GWh with a
  curve fitted on the last {cal["days"]} days (wind r = {cal["wind_r"]}, solar r = {cal["solar_r"]}).
  Today and tomorrow use the grid operators' day-ahead forecast where it is published (more accurate).
  Grey line: a normal day of the past four weeks. Measured accuracy: reliable up to four days ahead,
  then a rough indication.</p>
</section>"""


def _four_weeks(b: dict[str, Any]) -> str:
    days = [d for d in b.get("daily", []) if "price_mean" in d]
    if len(days) < 7:
        return ""
    labels = [_day(d["date"]) for d in days]
    price = bars([d["price_mean"] for d in days], labels,
                 [ORANGE if d["date"] == b["day"] else GREY for d in days], height=170,
                 unit="€/MWh", label="Daily average day-ahead price, last four weeks")  # fmt: skip
    wind = bars([d.get("wind_gwh") for d in days], labels,
                [BLUE if d["date"] == b["day"] else GREY for d in days], height=130,
                unit="GWh", label="Daily wind energy, last four weeks")  # fmt: skip
    return f"""<section class="wrap section">
  <p class="eyebrow">The last four weeks</p>
  <h2>Price and wind, day by day.</h2>
  <p class="lead small">{esc(four_weeks_caption(days))}</p>
  <figure><figcaption>Average day-ahead price <span>€/MWh</span></figcaption>{price}</figure>
  <figure><figcaption>Wind energy <span>GWh per day</span></figcaption>{wind}
    <div class="axis"><span>{esc(_day(days[0]["date"]))}</span><span>{esc(_day(days[-1]["date"]))}</span></div>
  </figure>
</section>"""  # noqa: E501


def _events(b: dict[str, Any], ai_story: bool) -> str:
    if not b["events"]:
        items = (
            "<p class='lead small'>No hour or figure stood out against the four weeks before.</p>"
        )
    else:
        rows = []
        for e in b["events"]:
            story = Story(**e)
            why = ""
            if story.explained_by_fundamentals is not None:
                why = ("In line with the residual load (demand minus wind and solar)."
                       if story.explained_by_fundamentals else
                       "More than the residual load explains; the data does not show why.")  # fmt: skip
            explanation = ""
            if ai_story and story.explanation:
                explanation = (f"<details><summary>Investigation (written by AI, figures "
                               f"checked)</summary><p>{esc(story.explanation)}</p></details>")  # fmt: skip
            rows.append(f"""<li><h3>{esc(headline(story))}</h3>
  <p>{esc(story.title)}</p>{f'<p class="fine">{esc(why)}</p>' if why else ""}{explanation}</li>""")
        items = f'<ol class="events">{"".join(rows)}</ol>'
    return f"""<section class="tint"><div class="wrap section">
  <p class="eyebrow">What stood out</p>
  <h2>{"Notable this day." if b["events"] else "A quiet day."}</h2>
  {items}
  <p class="fine">Found by fixed rules: hours far from comparable days (median and MAD), checked
  against the merit order; records, streaks and unusual day-to-day changes over two years;
  forecast misses. Associations, not causes.</p>
</div></section>"""


def _nrw(b: dict[str, Any]) -> str:
    nrw = b.get("nrw")
    if not nrw:
        return ""
    years = nrw["wind_added_per_year"]
    chart = bars([y["mw"] for y in years], [str(y["year"]) for y in years],
                 [BLUE if y["year"] == nrw["year"] else INK for y in years], height=200,
                 unit="MW", label="Onshore wind commissioned in NRW per year")  # fmt: skip
    axis = "".join(f"<span>{y['year']}</span>" for y in years)
    stats = []
    for key, label in (("wind_onshore", "Onshore wind"), ("solar", "Solar"),
                       ("battery", "Batteries (power)"), ("lignite", "Lignite")):  # fmt: skip
        item = nrw.get(key)
        if item:
            stats.append(f"<div><dt>{label} installed</dt><dd>{item['mw'] / 1000:,.2f} "
                         f"<small>GW</small></dd><dd class='fine'>{item['units']:,} units</dd></div>")  # fmt: skip
    districts = "".join(f"<li><span>{esc(d['district'])}</span><strong>{d['mw']:,.1f} MW</strong></li>"
                        for d in nrw["top_wind_districts"])  # fmt: skip
    return f"""<section class="wrap section" id="nrw">
  <p class="eyebrow">North Rhine-Westphalia</p>
  <h2>Onshore wind built in NRW, year by year.</h2>
  <p class="lead small">{esc(nrw_caption(nrw))}</p>
  <div class="split">
    <figure><figcaption>Onshore wind commissioned <span>MW per year</span></figcaption>{chart}
      <div class="axis even">{axis}</div></figure>
    <div><dl class="facts grid">{"".join(stats)}</dl>
      {f'<h3 class="small-title">Most new onshore wind in {nrw["year"]}</h3><ol class="table">{districts}</ol>' if districts else ""}
    </div>
  </div>
</section>"""  # noqa: E501


APP_LINKS = """<a href="/ask">Ask</a>
  <form class="refresh" method="post" action="/refresh"><button type="submit">Refresh briefing</button></form>"""


def page(b: dict[str, Any], ai_story: bool, root: str, days: list[str], app: bool = False) -> str:
    """One briefing as a complete page. `root` is the path back to the site root. `app`: served
    by the local web app, so the menu gets Ask (the chat) and a Refresh button."""
    i = days.index(b["day"]) if b["day"] in days else -1
    newer = days[i - 1] if i > 0 else None
    older = days[i + 1] if 0 <= i < len(days) - 1 else None
    nav = "".join([
        f'<a href="{root}days/{older}.html">‹ {esc(_day(older))}</a>' if older else "<span></span>",
        f'<a href="{root}days/{newer}.html">{esc(_day(newer))} ›</a>' if newer else "<span></span>",
    ])  # fmt: skip
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SITE_NAME} — {esc(_day(b["day"]))}</title>
<meta name="description" content="Daily briefing on the German electricity market: prices, wind, solar and what stood out, from open data.">
<link rel="stylesheet" href="{root}style.css">
</head>
<body>
<header class="top"><nav class="wrap">
  <a class="brand" href="{root}index.html">{SITE_NAME}</a>
  <a href="{root}index.html">Today</a><a href="{root}archive.html">Archive</a><a href="#nrw">NRW</a><a href="{root}about.html">About</a>
  {APP_LINKS if app else ""}
  <span class="fine">Data through {esc(_day(b["day"]))}</span>
</nav></header>
<main>
{_lead(b, ai_story)}
{_kpis(b)}
{_today(b)}
{_price_forecast(b)}
{_week(b)}
{_four_weeks(b)}
{_events(b, ai_story)}
{_nrw(b)}
<nav class="wrap pager" aria-label="Other days">{nav}</nav>
</main>
{footer(root)}
</body>
</html>
"""  # noqa: E501


def footer(root: str) -> str:
    return f"""<footer class="wrap foot">
  <div><p class="title">Sources and licences</p>
  <p>Generation, load, flows and forecasts: Energy-Charts.info (Fraunhofer ISE), CC BY 4.0 ·
  Day-ahead prices: Bundesnetzagentur | SMARD.de, CC BY 4.0 · Power plants:
  Marktstammdatenregister, © Bundesnetzagentur, dl-de/by-2-0 · Weather forecasts:
  Open-Meteo.com, CC BY 4.0.</p></div>
  <div><p class="title">How this page is made</p>
  <p>Figures and events are calculated in code each morning; "normal" means the four weeks
  before. <a href="{root}about.html">Method and AI use</a>.</p></div>
</footer>"""


def about(root: str, legal_notice: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SITE_NAME} — About</title><link rel="stylesheet" href="{root}style.css"></head>
<body><header class="top"><nav class="wrap"><a class="brand" href="{root}index.html">{SITE_NAME}</a>
<a href="{root}index.html">Today</a><a href="{root}archive.html">Archive</a><a href="{root}about.html">About</a></nav></header>
<main class="wrap section prose">
<h1>About this page</h1>
<h2>Method</h2>
<p>Every morning a program loads the previous day's data, calculates the key figures and
compares them with the four weeks before. Events are found by fixed rules: hours whose price
is far from the same hour on comparable days (median and median absolute deviation), each
tested against a merit-order curve fitted on residual load; records and streaks over two
years; unusual day-to-day changes; and misses of the day-ahead forecasts. All captions are
written by code from these numbers. They describe associations, not causes.</p>
<h2>Use of AI</h2>
<p>Where a story or an investigation is marked "written by AI", it was written by a language
model (Anthropic Claude) from the calculated figures only. Before publication every number in
it is checked against those figures, and wording rules are applied (no causal claims, no
hype, no quotes). A text that fails the check is not published; a plain version written by
code is shown instead.</p>
<h2>Data</h2>
<p>Energy-Charts.info (Fraunhofer ISE), CC BY 4.0 · Bundesnetzagentur | SMARD.de, CC BY 4.0 ·
Marktstammdatenregister, © Bundesnetzagentur, dl-de/by-2-0. Each day's figures can be
downloaded as JSON from the archive. Recent values are revised by the publishers; the plant
register's latest months grow as late registrations arrive.</p>
<h2>Privacy</h2>
<p>This site sets no cookies, loads nothing from other servers and uses no tracking.</p>
{f"<h2>Legal notice</h2><p>{esc(legal_notice)}</p>" if legal_notice else ""}
</main>{footer(root)}</body></html>
"""  # noqa: E501


def archive(days: list[str]) -> str:
    rows = "".join(f'<li><a href="days/{d}.html">{esc(_day(d))}</a> · '
                   f'<a class="fine" href="data/{d}.json">data (JSON)</a></li>' for d in days)  # fmt: skip
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SITE_NAME} — Archive</title><link rel="stylesheet" href="style.css"></head>
<body><header class="top"><nav class="wrap"><a class="brand" href="index.html">{SITE_NAME}</a>
<a href="index.html">Today</a><a href="archive.html">Archive</a><a href="about.html">About</a></nav></header>
<main class="wrap section prose"><h1>Archive</h1><ol class="table">{rows}</ol></main>
{footer("")}</body></html>
"""  # noqa: E501


def build_site(
    briefings_dir: Path,
    out_dir: Path,
    ai_story: bool = False,
    legal_notice: str | None = None,
    app: bool = False,
) -> list[str]:
    """Write the whole site; returns the days built, newest first."""
    files = sorted(briefings_dir.glob("????-??-??.json"), reverse=True)
    if not files:
        raise FileNotFoundError(f"No briefings in {briefings_dir}: run `garden brief` first.")
    briefings, skipped = [], []
    for f in files:
        raw = json.loads(f.read_text())
        try:  # briefings written by an older version of the format are left out, not guessed
            Briefing(**raw)
            [Story(**e) for e in raw["events"]]
        except (ValueError, TypeError, KeyError):
            skipped.append(f.name)
            continue
        briefings.append(raw)
    if not briefings:
        raise FileNotFoundError(f"No briefing in the current format in {briefings_dir}.")
    days = [b["day"] for b in briefings]
    for sub in ("days", "data"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(STATIC / "style.css", out_dir / "style.css")
    for b in briefings:
        public = dict(b)
        if not ai_story:  # without AI text in the open data too
            public = {**b, "story": None,
                      "events": [{**e, "explanation": None, "explanation_steps": []}
                                 for e in b["events"]]}  # fmt: skip
        (out_dir / "days" / f"{b['day']}.html").write_text(page(public, ai_story, "../", days, app))
        (out_dir / "data" / f"{b['day']}.json").write_text(
            json.dumps(public, ensure_ascii=False, indent=1)
        )
    latest = briefings[0] if ai_story else {**briefings[0], "story": None}
    (out_dir / "index.html").write_text(page(latest, ai_story, "", days, app))
    (out_dir / "archive.html").write_text(archive(days))
    (out_dir / "about.html").write_text(about("", legal_notice))
    if skipped:
        print(f"Skipped (older format): {', '.join(skipped)}")
    return days
