"""Evals for the news_writer agent: does the story say the right thing, not just a valid thing?

The desk check (monitor/writer.py) is a gate: it rejects drafts that break a hard rule
(an unbacked number, causal or hype wording, length). Passing it says nothing about
whether the story is any good. These checks ask what an editor would ask next, all
graded in code from the briefing the story was written from (no model as judge):

  published       the model's story passed the desk (not replaced by the template)
  lead_first      the opening (headline, standfirst, first paragraph) is about the lead
                  story: it carries one of the lead story's numbers or its hours
  price_reading   for a price lead story: it mentions the residual load and does not
                  contradict the merit-order flag ("in line with" when it is not, or
                  "does not show the reason" when it is)
  since_at_least  a record with no earlier value keeps "since at least" (never "ever")
  comparison      the standfirst gives a number with what it is compared with
  certainty       no "will" (forecasts are expectations) and no advice ("should")
  outlook         the story looks ahead when the briefing has a forecast or a price plan,
                  and invents no outlook when it has neither
  in_brief        at most three short items, none on a day with no further stories
  quiet_day       a day without stories is called quiet

A check that does not apply to a case (no price lead story, no record) is None and does
not count. A case passes when every applicable check passes.

Modes: live (the real model; --record keeps its responses), replay (the recorded
responses again, free: catches regressions in the checks and the writer's loop), and
template (the code-only template story through the same checks: the baseline the model
has to beat).
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from gridagent.agent.config import LoadedAgent
from gridagent.evals.offline import (
    CaseRecording,
    RecordingLLM,
    load_recording,
    new_recording,
    recording_path,
    replay_llm,
    save_recording,
)
from gridagent.llm.base import LLM, LLMError
from gridagent.llm.fake import FakeLLM
from gridagent.monitor.anomalies import Story
from gridagent.monitor.daily import Briefing
from gridagent.monitor.writer import NewsStory, template_story, write_story
from gridagent.tools.grounding import _numbers_with_text, is_backed, without_dates

StoryMode = Literal["live", "replay", "template"]
CHECK_NAMES = ("published", "lead_first", "price_reading", "since_at_least", "comparison",
               "certainty", "outlook", "in_brief", "quiet_day")  # fmt: skip
# Fact values that are statistics about the comparison, not news a reader would see.
NOT_ANCHORS = {"robust_z", "gap_to_merit_order_z", "comparable_days", "days_spanned",
               "days_compared", "days_with_a_larger_change"}  # fmt: skip
COMPARISON_WORDS = (
    "against",
    "normal",
    "usual",
    "compared",
    "than",
    "since",
    "average",
    "above",
    "below",
    "forecast",
)  # "7.9 % below its forecast of 811.76"
# Small counts are often written as words ("a third day in a row"). Index = the number.
CARDINALS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
             "ten", "eleven", "twelve")  # fmt: skip
ORDINALS = ("", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
            "ninth", "tenth", "eleventh", "twelfth")  # fmt: skip
READ_AS_EXPLAINED = ("in line with",)
READ_AS_UNEXPLAINED = ("than the residual load explains", "does not show the reason",
                       "not explained by", "more than the residual load",
                       "less than the residual load")  # fmt: skip


class StoryCase(BaseModel):
    id: str
    input: str  # briefing JSON, relative to the eval set file
    tests: str  # what this case is for, in one line


class StoryEvalSet(BaseModel):
    description: str
    cases: list[StoryCase]


class StoryCaseResult(BaseModel):
    id: str
    day: str
    tests: str
    checks: dict[str, bool | None]
    failed: list[str]
    passed: bool
    model_calls: int  # 1 = the first draft passed the desk; 2 = it needed a revision
    desk_problems: list[str]  # why the model's draft was replaced (template stories only)
    cost_usd: float
    latency_s: float
    story: NewsStory


class StoryEvalReport(BaseModel):
    agent: str
    version: str
    model: str
    mode: StoryMode
    eval_set_hash: str
    cases: list[StoryCaseResult]
    pass_rate: float
    published_rate: float  # stories the desk accepted from the model
    first_draft_rate: float  # of those: accepted without a revision
    check_pass_rates: dict[str, float | None]
    total_cost_usd: float


# ---------- the eval set ----------


def load_story_eval_set(path: Path) -> tuple[StoryEvalSet, dict[str, Briefing], str]:
    """Cases, their briefings, and a hash of both (recordings are only valid for it)."""
    raw = path.read_text()
    eval_set = StoryEvalSet.model_validate(yaml.safe_load(raw))
    digest = hashlib.sha256(raw.encode())
    briefings = {}
    for case in eval_set.cases:
        text = (path.parent / case.input).read_text()
        digest.update(text.encode())
        briefings[case.id] = Briefing.model_validate_json(text)
    return eval_set, briefings, digest.hexdigest()[:16]


# ---------- the checks ----------


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(p in text for p in phrases)


def _anchors(lead: Story) -> list[float]:
    numbers = {k: v for k, v in lead.facts.items()
               if isinstance(v, int | float) and not isinstance(v, bool)}  # fmt: skip
    return [float(v) for k, v in numbers.items() if k not in NOT_ANCHORS]


def _mentions_lead(opening: str, lead: Story) -> bool:
    pool = _anchors(lead)
    if any(is_backed(v, w, pool) for v, w in _numbers_with_text(without_dates(opening))):
        return True
    words = set(re.findall(r"[a-z]+", opening.lower()))
    for v in pool:
        if v == int(v) and 0 < v < len(ORDINALS) and {CARDINALS[int(v)], ORDINALS[int(v)]} & words:
            return True
    hours = lead.facts.get("hours")  # "17:00–20:00": the start hour is enough
    return isinstance(hours, str) and hours[:5] in opening


def _day_names(iso_days: set[str]) -> tuple[str, ...]:
    """How a story names a day: "Sunday", "25 October" or "2026-10-25" (the template)."""
    days = [date.fromisoformat(d) for d in iso_days]
    return tuple(n.lower() for d in days for n in (f"{d:%A}", f"{d.day} {d:%B}", d.isoformat()))


def story_checks(
    story: NewsStory, briefing: Briefing, baseline: bool = False
) -> dict[str, bool | None]:
    text = story.text()
    lowered = text.lower()
    lead = briefing.events[0] if briefing.events else None
    opening = "\n".join([story.headline, story.standfirst, *story.paragraphs[:1]])
    checks: dict[str, bool | None] = dict.fromkeys(CHECK_NAMES)
    # The template is the baseline, not a draft the desk rejected: "published" does not apply.
    checks["published"] = None if baseline else story.written_by == "model"
    checks["lead_first"] = _mentions_lead(opening, lead) if lead else None
    if lead and lead.kind.startswith("price_") and lead.explained_by_fundamentals is not None:
        contradicts = (_has(lowered, READ_AS_UNEXPLAINED) if lead.explained_by_fundamentals
                       else _has(lowered, READ_AS_EXPLAINED))  # fmt: skip
        checks["price_reading"] = "residual load" in lowered and not contradicts
    if lead and lead.kind == "record" and lead.facts.get("previous_date") is None:
        checks["since_at_least"] = "since at least" in lowered and not re.search(
            r"\b(ever|all-time)\b", lowered
        )
    standfirst = without_dates(story.standfirst)
    checks["comparison"] = bool(_numbers_with_text(standfirst)) and _has(
        story.standfirst.lower(), COMPARISON_WORDS
    )
    checks["certainty"] = not re.search(r"\b(will|should)\b", lowered)
    # Looking ahead = naming a day the forecast or the price plan covers (not the day itself)
    ahead = {o.day for o in briefing.outlooks} | {p.day for p in briefing.plans}
    has_outlook = bool(ahead - {briefing.day})
    looks_ahead = _has(lowered, _day_names(ahead - {briefing.day}))
    invented = _has(lowered, ("is expected", "are expected"))
    checks["outlook"] = looks_ahead if has_outlook else not invented
    further = max(0, len(briefing.events) - 1)
    checks["in_brief"] = len(story.in_brief) <= min(3, further) if further else not story.in_brief
    checks["quiet_day"] = "quiet" in lowered if not briefing.events else None
    return checks


# ---------- running ----------


def _result(case: StoryCase, briefing: Briefing, story: NewsStory, calls: int,
            latency_s: float) -> StoryCaseResult:  # fmt: skip
    checks = story_checks(story, briefing, baseline=calls == 0)
    failed = [name for name, ok in checks.items() if ok is False]
    return StoryCaseResult(
        id=case.id, day=briefing.day, tests=case.tests, checks=checks, failed=failed,
        passed=not failed, model_calls=calls, desk_problems=story.problems,
        cost_usd=story.cost_usd, latency_s=round(latency_s, 2), story=story,
    )  # fmt: skip


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def run_story_eval(
    agent: LoadedAgent,
    eval_set_path: Path,
    mode: StoryMode,
    *,
    build_llm: Callable[[], LLM] | None = None,
    record: bool = False,
    only: list[str] | None = None,
    progress: Callable[[StoryCaseResult], None] | None = None,
) -> StoryEvalReport:
    eval_set, briefings, eval_hash = load_story_eval_set(eval_set_path)
    cases = [c for c in eval_set.cases if not only or c.id in only]
    model = agent.config.model.name
    rec_path = recording_path(agent.directory, model)
    stored = load_recording(rec_path) if mode == "replay" else None
    if mode == "replay" and (stored is None or stored.eval_set_hash != eval_hash):
        raise FileNotFoundError(f"No recording for this eval set at {rec_path}; "
                                "run with --live --record first")  # fmt: skip
    recording = None
    if mode == "live" and record:
        previous = load_recording(rec_path)
        recording = new_recording(agent.config.name, agent.config.version, model, eval_hash)
        if previous is not None and previous.eval_set_hash == eval_hash:
            recording.cases = dict(previous.cases)  # recording some cases keeps the others

    results = []
    for case in cases:
        briefing = briefings[case.id]
        started = time.monotonic()
        if mode == "template":
            story, calls = template_story(briefing), 0
        else:
            if mode == "live":
                if build_llm is None:
                    raise ValueError("live mode needs build_llm")
                inner: LLM = build_llm()
            else:
                assert stored is not None
                rec = stored.cases.get(case.id)
                missing = FakeLLM([LLMError(f"No recording for case '{case.id}'")])
                inner = replay_llm(rec) if rec is not None else missing
            llm = RecordingLLM(inner)
            story = write_story(briefing, agent, llm)
            calls = len(llm.responses)
            if recording is not None and llm.responses:
                recording.cases[case.id] = CaseRecording(
                    question=f"{case.id} {briefing.day}", responses=llm.responses,
                    latency_s=round(time.monotonic() - started, 2),
                )  # fmt: skip
                save_recording(rec_path, recording)  # after every case: keep what was paid
        latency = time.monotonic() - started
        if stored is not None and case.id in stored.cases:
            latency = stored.cases[case.id].latency_s  # the original live latency
        result = _result(case, briefing, story, calls, latency)
        results.append(result)
        if progress:
            progress(result)

    published = [r for r in results if r.checks["published"]]
    rates = {name: _rate([r.checks[name] is True for r in results if r.checks[name] is not None])
             for name in CHECK_NAMES}  # fmt: skip
    return StoryEvalReport(
        agent=agent.config.name, version=agent.config.version,
        model="template" if mode == "template" else model, mode=mode, eval_set_hash=eval_hash,
        cases=results, pass_rate=_rate([r.passed for r in results]) or 0.0,
        published_rate=_rate([r.checks["published"] is True for r in results]) or 0.0,
        first_draft_rate=_rate([r.model_calls == 1 for r in published]) or 0.0,
        check_pass_rates=rates,
        total_cost_usd=round(sum(r.cost_usd for r in results), 5),
    )  # fmt: skip


def save_story_report(report: StoryEvalReport, reports_dir: Path) -> Path:
    folder = reports_dir / "story_evals"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{report.agent}-{report.version}-{report.mode}-{stamp}.json"
    path.write_text(report.model_dump_json(indent=1) + "\n")
    return path


def summary(report: StoryEvalReport) -> dict[str, Any]:
    return {k: v for k, v in json.loads(report.model_dump_json()).items() if k != "cases"}
