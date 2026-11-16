"""Model-written explanations for the biggest price events of a briefing, and storage.

The explanation is an ordinary question to the evaluated `energy_qa` agent ("Why was
the price so high on ... between ... and ...?"), so it goes through the same tools,
grounding check and run log as any chat answer. A cost limit stops further questions.
"""

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from gridagent.agent.config import LoadedAgent
from gridagent.agent.loop import Agent
from gridagent.agent.runlog import RunLog
from gridagent.data.provider import DataProvider
from gridagent.llm.base import LLM
from gridagent.monitor.daily import EXPLAIN_EVENTS, Briefing, Event, as_dict

PRICE_EVENTS = ("price_spike", "price_dip")


def question_for(event: Event, day: date) -> str:
    word = "high" if event.kind == "price_spike" else "low"
    return (f"Why was the electricity price so {word} on {day.day} {day:%B %Y} between "
            f"{event.from_hour:02d}:00 and {event.to_hour:02d}:00?")  # fmt: skip


def explain_events(
    briefing: Briefing,
    agent: LoadedAgent,
    llm_factory: Callable[[], LLM],
    data: DataProvider,
    today: date,
    max_cost_usd: float,
    on_run: Callable[[RunLog], None] = lambda log: None,
) -> None:
    """Ask the agent about the biggest price events (at most EXPLAIN_EVENTS) within budget."""
    from gridagent.services import method_steps  # services imports a lot; only needed here

    day = date.fromisoformat(briefing.day)
    spent = 0.0
    for event in [e for e in briefing.events if e.kind in PRICE_EVENTS][:EXPLAIN_EVENTS]:
        if spent >= max_cost_usd:
            break
        log = Agent(agent, llm_factory(), data, today=lambda: today).run(question_for(event, day))
        on_run(log)
        spent += log.cost_usd or 0.0
        if log.status == "ok" and log.final_answer:
            event.explanation = log.final_answer
            event.explanation_steps = method_steps(log)
    briefing.cost_usd = round(spent, 4)


# ---------- storage: one JSON file per reviewed day ----------


def save_briefing(directory: Path, briefing: Briefing) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{briefing.day}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(as_dict(briefing), ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path


def list_briefings(directory: Path) -> list[str]:
    """Reviewed days with a briefing, newest first."""
    return sorted((p.stem for p in directory.glob("????-??-??.json")), reverse=True)


def load_briefing(directory: Path, day: str) -> dict:
    return json.loads((directory / f"{day}.json").read_text())
