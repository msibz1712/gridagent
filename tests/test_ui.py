"""Streamlit UI smoke tests with Streamlit's AppTest (runs the script headless, no browser).

Skipped when the optional `ui` extra is not installed.
"""

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).parent.parent
APP = ROOT / "src" / "gridagent" / "ui" / "app.py"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppTest:
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("GARDEN_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("GARDEN_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return AppTest.from_file(str(APP), default_timeout=60)


def test_page_loads_without_errors(app: AppTest) -> None:
    app.run()
    assert not app.exception
    assert app.title[0].value == "⚡ GridAgent"
    assert app.sidebar.selectbox[0].value == "energy_qa@0.1.0"


def test_live_ask_is_disabled_without_api_key(app: AppTest) -> None:
    app.run()
    assert app.button(key="ask_button").disabled
    assert any("ANTHROPIC_API_KEY is not set" in w.value for w in app.warning)


def test_free_demo_shows_answer_and_trace(app: AppTest) -> None:
    app.run()
    app.button(key="demo_button").click().run()
    assert not app.exception
    answers = " ".join(m.value for m in app.markdown)
    assert "Source:" in answers
    assert len(app.dataframe) >= 1  # the trace table


def test_offline_eval_from_ui_writes_report(app: AppTest, tmp_path: Path) -> None:
    app.run()
    app.button(key="eval_button").click().run()
    assert not app.exception
    assert list((tmp_path / "reports").glob("*.json"))
    assert any("Report written" in s.value for s in app.success)
