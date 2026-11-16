"""The public site: code captions, static pages, AI switch, no external requests."""

import json
import re
from datetime import date

import pytest

from gridagent.data.fixture_source import FixtureDataSource
from gridagent.monitor.anomalies import Story
from gridagent.monitor.captions import four_weeks_caption, headline, plan_caption
from gridagent.monitor.daily import build_briefing
from gridagent.monitor.explain import save_briefing
from gridagent.monitor.writer import template_story
from gridagent.site.build import bars, build_site

from conftest import FIXTURE_DIR


def story(kind: str, **facts) -> Story:
    return Story(kind=kind, title="t", score=0.5, score_parts={}, facts=facts)


@pytest.mark.parametrize(("item", "expected"), [
    (None, "A quiet day on the German power market"),
    (story("price_spike", price_eur_mwh=553.29, hours="16:00–20:00"),
     "Prices reached 553.29 €/MWh, 16:00–20:00"),
    (story("streak_end", run_days=19, figure="average day-ahead price",
           group="highest 10 % of days since 15 November 2024"),
     "A 19-day run of highest average day-ahead price has ended"),
    (story("record", figure="wind energy", direction="highest", previous_date=None,
           compared_back_to="2024-10-24"),
     "Wind energy at its highest since at least 2024"),
    (story("forecast_miss", figure="wind", miss_pct=-10.2, miss_gwh=-126.0),
     "Wind came in 10.2 % below forecast"),
])  # fmt: skip
def test_code_headlines(item, expected: str) -> None:
    assert headline(item) == expected


def test_captions_state_associations_not_causes() -> None:
    daily = [{"date": f"2026-10-{d:02d}", "price_mean": 100.0 + d, "wind_gwh": 500.0 - d}
             for d in range(1, 15)]  # fmt: skip
    text = four_weeks_caption(daily)
    assert "Wednesday 14 October: 114.00 €/MWh with 486.0 GWh" in text
    assert not re.search(r"\b(because|caused|due to)\b", text)
    plan = {"day": "2026-11-16", "cheapest_hours": "02:00–05:00", "cheapest_price": 116.48,
            "dearest_hours": "17:00–20:00", "dearest_price": 263.92}  # fmt: skip
    assert plan_caption(plan).startswith("Published day-ahead prices for Monday 16 November")


def test_bars_handle_negative_prices() -> None:
    svg = bars([10.0, -5.0, None, 20.0], ["a", "b", "c", "d"], ["#000"] * 4, unit="€/MWh")
    assert svg.count("<rect") == 3 and "-5.00 €/MWh" in svg


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A site built from one recorded-data briefing with an (AI-labelled) story."""
    folder = tmp_path_factory.mktemp("briefings")
    b = build_briefing(FixtureDataSource(FIXTURE_DIR), date(2026, 10, 29),
                       today=date(2026, 10, 30), history={})  # fmt: skip
    b.story = template_story(b).model_dump() | {"written_by": "model"}
    b.events[0].explanation = "An investigation."
    save_briefing(folder, b)
    (folder / "2026-10-01.json").write_text(json.dumps({"day": "2026-10-01", "events": [{}]}))
    return folder, tmp_path_factory.mktemp("out")


def test_site_pages_and_open_data(site) -> None:
    briefings, out = site
    assert build_site(briefings, out, ai_story=True) == ["2026-10-29"]  # old format skipped
    for name in ("index.html", "archive.html", "about.html", "style.css",
                 "days/2026-10-29.html", "data/2026-10-29.json"):  # fmt: skip
        assert (out / name).is_file(), name
    page = (out / "index.html").read_text()
    assert "Written by AI from the data" in page and "An investigation." in page
    assert "Marktstammdatenregister" in page and "CC BY 4.0" in page


def test_no_external_requests_cookies_or_scripts(site) -> None:
    briefings, out = site
    build_site(briefings, out, ai_story=True)
    for path in out.rglob("*.html"):
        text = path.read_text()
        assert not re.search(r'(src|href)="https?://', text), path  # GDPR: nothing loaded
        assert "<script" not in text, path


def test_without_ai_no_ai_text_anywhere(site) -> None:
    briefings, out = site
    build_site(briefings, out, ai_story=False)
    page = (out / "index.html").read_text()
    data = json.loads((out / "data" / "2026-10-29.json").read_text())
    assert "Written by AI" not in page and "An investigation." not in page
    assert data["story"] is None and all(e["explanation"] is None for e in data["events"])


def test_redispatch_never_reaches_the_public_site(site) -> None:
    """netztransparenz.de data may not be published without the TSOs' consent."""
    briefings, out = site
    build_site(briefings, out, ai_story=True)
    for path in [*out.rglob("*.html"), *out.rglob("*.json")]:
        assert "redispatch" not in path.read_text().lower(), path
