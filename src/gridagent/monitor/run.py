"""One briefing run, end to end: the same code for the command line and the web app.

figures and stories -> (optional) model explanations and story -> saved JSON. A run
without the model never replaces what the model already wrote for that day: the figures
are calculated in code, so an earlier story and its explanations still fit.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from gridagent.agent.runlog import RunLog, append_run_log
from gridagent.data.provider import DataProvider
from gridagent.garden.registry import resolve
from gridagent.monitor.daily import Briefing, build_briefing
from gridagent.monitor.explain import explain_events, save_briefing
from gridagent.monitor.writer import NewsStory, template_story, write_story


def run_briefing(
    data: DataProvider,
    reviewed: date,
    today: date,
    *,
    agents_dir: Path,
    reports_dir: Path,
    log_path: Path | None,
    use_model: bool,
    max_cost_usd: float,
    build_llm: Callable,
) -> tuple[Briefing, Path]:
    """Build, (optionally) write and save the briefing for `reviewed`. Returns it and its file."""
    briefing = build_briefing(data, reviewed, today)
    folder = reports_dir / "briefings"

    def log(run: RunLog) -> None:
        if log_path is not None:
            append_run_log(log_path, run)

    if use_model:
        analyst = resolve(agents_dir, "energy_qa")
        explain_events(briefing, analyst, lambda: build_llm(analyst.config), data, today,
                       max_cost_usd, on_run=log)  # fmt: skip
        writer = resolve(agents_dir, "news_writer")
        story = write_story(briefing, writer, build_llm(writer.config))
        briefing.cost_usd = round(briefing.cost_usd + story.cost_usd, 4)
    else:
        story = _keep_earlier_model_text(briefing, folder / f"{reviewed}.json")
    briefing.story = story.model_dump()
    return briefing, save_briefing(folder, briefing)


def _keep_earlier_model_text(briefing: Briefing, earlier_file: Path) -> NewsStory:
    story = template_story(briefing)
    if not earlier_file.is_file():
        return story
    earlier = json.loads(earlier_file.read_text())
    if (earlier.get("story") or {}).get("written_by") != "model":
        return story
    old = {(e["kind"], e.get("from_hour")): e for e in earlier.get("events", [])}
    for event in briefing.events:
        match = old.get((event.kind, event.from_hour))
        if match and match.get("explanation"):
            event.explanation = match["explanation"]
            event.explanation_steps = match.get("explanation_steps", [])
    return NewsStory(**earlier["story"])
