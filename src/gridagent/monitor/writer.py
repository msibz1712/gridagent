"""The news writer: the daily briefing as a short news story, checked before it is published.

1. A fact sheet holds every number the story may use, all calculated in code
   (key figures with their normal, the ranked stories with their facts, outlook, plans).
2. The `news_writer` agent (agents/news_writer) writes headline, standfirst, three
   paragraphs and short items under a newsroom style guide, and hands them over
   through the `publish_story` tool, so the structure is guaranteed.
3. `check_story` applies the rules a desk editor would: every number backed by the
   fact sheet (the same grounding check as every answer), no causal claims, no hype,
   no quotes, length limits. A failed draft goes back once with the problems listed.
4. A story that still fails is never published: a plain template version from the
   same fact sheet takes its place, and the problems are kept in the record.
"""

import json
import re
from typing import Any

from pydantic import BaseModel

from gridagent.agent.config import LoadedAgent
from gridagent.llm.base import LLM, LLMError, TokenUsage
from gridagent.llm.pricing import estimate_cost_usd
from gridagent.monitor.daily import Briefing
from gridagent.tools.grounding import NUMBER, unsupported_numbers, without_dates

HEADLINE_MAX = 80
STANDFIRST_MAX_WORDS = 35
PARAGRAPH_MAX_WORDS = 90  # the guide says 80; a little slack before rejecting
PARAGRAPH_MAX_NUMBERS = 6  # the guide says five
CAUSAL = ("caused", "causing", "because of", "due to", "led to", "drove", "driven by",
          "triggered", "as a result of", "resulted in")  # fmt: skip
# Whole words only ("ever" must not catch "every"), so each form is listed.
HYPE = (
    "massive", "huge", "shocking", "skyrocketed", "skyrocketing", "soared", "soaring",
    "plummeted", "plummeting", "plunged", "plunging", "crashed", "exploded", "unprecedented",
    "dramatic", "dramatically", "historic", "all-time", "ever", "record", "records",
    "record-breaking",
)  # fmt: skip
QUOTES = ('"', "“", "”", "„", "«", "»")
# The reader never sees the inputs, and "since at least" is not where the data starts.
META = ("fact sheet", "the analysis", "data provided", "start of the data", "first data")

PUBLISH_STORY: dict[str, Any] = {
    "name": "publish_story",
    "description": "Publish the finished story. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string", "description": "At most 80 characters, with a verb."},
            "standfirst": {"type": "string", "description": "One sentence, at most 35 words."},
            "paragraphs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Three short paragraphs, most important first.",
            },
            "in_brief": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One sentence per further story; may be empty.",
            },
        },  # fmt: skip
        "required": ["headline", "standfirst", "paragraphs", "in_brief"],
        "additionalProperties": False,
    },
    "strict": True,
}


class NewsStory(BaseModel):
    headline: str
    standfirst: str
    paragraphs: list[str]
    in_brief: list[str] = []
    written_by: str = "model"  # "model" or "template"
    model: str | None = None
    cost_usd: float = 0.0
    problems: list[str] = []  # why a model draft was rejected (template stories only)

    def text(self) -> str:
        return "\n".join([self.headline, self.standfirst, *self.paragraphs, *self.in_brief])


# ---------- 1. the fact sheet ----------


def fact_sheet(briefing: Briefing) -> dict[str, Any]:
    """Everything the story may say, as data. No number in the story may come from elsewhere."""
    return {
        "day": briefing.day,
        "normal_means": "the average of the 4 weeks (28 days) before the day",
        "key_figures": [
            {
                "name": f.name,
                "value": f.value,
                "unit": f.unit,
                "normal": f.normal,
                "difference": f.difference,
                "change_pct": f.change_pct,
                "at": f.at,
            }
            for f in briefing.figures
        ],  # fmt: skip
        "stories": [
            {
                "kind": s.kind,
                "summary": s.title,
                "facts": s.facts,
                "explained_by_fundamentals": s.explained_by_fundamentals,
                "explanation_by_the_analysis_agent": s.explanation,
            }
            for s in briefing.events
        ],  # fmt: skip
        "forecast": [o.model_dump() for o in briefing.outlooks],
        "price_plan": [p.model_dump() for p in briefing.plans],
    }


# ---------- 3. the desk editor's checks ----------


def language_problems(text: str) -> list[str]:
    """Wording rules every published text follows (story and explanations alike)."""
    problems = []
    lowered = text.lower()
    causal = [w for w in CAUSAL if re.search(rf"\b{re.escape(w)}\b", lowered)]
    if causal:
        problems.append(f"Causal wording ({', '.join(causal)}): write 'coincided with' or "
                        "'alongside' instead.")  # fmt: skip
    hype = [w for w in HYPE if re.search(rf"\b{re.escape(w)}\b", lowered)]
    if hype:
        problems.append(f"Hype words ({', '.join(hype)}): use plain words.")
    if any(q in text for q in QUOTES):
        problems.append("Quotation marks: the story must not quote anyone.")
    meta = [w for w in META if w in lowered]
    if meta:
        problems.append(f"Wording about the inputs ({', '.join(meta)}): write about the market.")
    return problems


def check_story(story: NewsStory, sheet_text: str) -> list[str]:
    """Every rule the story breaks, as sentences the writer can act on."""
    text = story.text()
    problems = []
    unbacked = unsupported_numbers(text, [sheet_text], [])
    if unbacked:
        problems.append(f"Numbers not in the fact sheet: {', '.join(unbacked)}. Use only "
                        "fact-sheet numbers; do not calculate.")  # fmt: skip
    problems += language_problems(text)
    for i, paragraph in enumerate(story.paragraphs, 1):
        words = len(paragraph.split())
        numbers = len(NUMBER.findall(without_dates(paragraph)))
        if words > PARAGRAPH_MAX_WORDS:
            problems.append(f"Paragraph {i} has {words} words; at most 80.")
        if numbers > PARAGRAPH_MAX_NUMBERS:
            problems.append(f"Paragraph {i} has {numbers} numbers; keep the five that matter.")
    if len(story.headline) > HEADLINE_MAX:
        problems.append(f"Headline is {len(story.headline)} characters; at most {HEADLINE_MAX}.")
    if len(story.standfirst.split()) > STANDFIRST_MAX_WORDS:
        problems.append(f"Standfirst has more than {STANDFIRST_MAX_WORDS} words.")
    if not 2 <= len(story.paragraphs) <= 4:
        problems.append(f"{len(story.paragraphs)} paragraphs; write three.")
    return problems


# ---------- 2. writing, with one revision ----------


def write_story(briefing: Briefing, agent: LoadedAgent, llm: LLM) -> NewsStory:
    """A checked story from the model, or the template version if the model's fails."""
    sheet_text = json.dumps(fact_sheet(briefing), ensure_ascii=False, indent=1)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Fact sheet for {briefing.day}:\n{sheet_text}"}
    ]
    usage, problems = TokenUsage(), ["the model did not publish a story"]
    for _ in range(max(1, agent.config.limits.max_steps)):
        try:
            response = llm.create(agent.system_prompt, [PUBLISH_STORY], messages)
        except LLMError as exc:
            problems = [f"model error: {exc}"]
            break
        usage = usage + response.usage
        if response.stop_reason == "max_tokens":
            problems = ["the model ran out of tokens before finishing (max_tokens)"]
            break  # a cut-off tool input is never published, not even repaired
        calls = response.tool_uses()
        if not calls:
            problems = ["the model answered without publishing a story"]
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": "Publish the story with publish_story."},
            ]
            continue
        try:
            story = NewsStory(**calls[0]["input"])
        except (TypeError, ValueError):
            problems = ["the story did not have the required fields"]
            break
        problems = check_story(story, sheet_text)
        if not problems:
            story.model = llm.model_name
            story.cost_usd = round(estimate_cost_usd(llm.model_name, usage) or 0.0, 5)
            return story
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": calls[0]["id"], "is_error": True,
                "content": "Rejected by the desk: " + " ".join(problems) +
                           " Revise and publish again.",
            }]},
        ]  # fmt: skip
    fallback = template_story(briefing)
    fallback.problems = problems
    fallback.cost_usd = round(estimate_cost_usd(llm.model_name, usage) or 0.0, 5)
    return fallback


# ---------- 4. the template: always correct, never exciting ----------


def template_story(briefing: Briefing) -> NewsStory:
    """A plain story from the fact sheet alone: what is published when the model's is not."""
    figures = {f.name: f for f in briefing.figures}
    lead = briefing.events[0] if briefing.events else None
    price = figures.get("Average day-ahead price")
    from gridagent.monitor.captions import headline as short_headline

    headline = short_headline(lead)  # a short, complete headline, never a cut-off sentence
    if len(headline) > HEADLINE_MAX:
        headline = headline[: HEADLINE_MAX - 1].rsplit(" ", 1)[0] + "…"
    standfirst = (f"The average day-ahead price was {price.value:.2f} EUR/MWh, against "
                  f"{price.normal:.2f} on a normal day of the 4 weeks before."
                  if price and price.value is not None and price.normal is not None
                  else f"Key figures for {briefing.day}.")  # fmt: skip
    paragraphs = [lead.title + "." if lead else "No unusual events were found."]
    wind, share = (
        figures.get("Wind energy"),
        figures.get("Renewable share of public net generation"),
    )
    if wind and share and wind.value is not None and share.value is not None:
        paragraphs.append(f"Wind produced {wind.value:.2f} GWh (normal {wind.normal}) and "
                          f"renewables had a share of {share.value:.2f} %.")  # fmt: skip
    if briefing.outlooks:
        paragraphs.append(
            f"Forecast for {briefing.outlooks[0].day}: {briefing.outlooks[0].headline}."
        )
    if briefing.plans:
        plan = briefing.plans[0]
        paragraphs.append(f"On {plan.day} the cheapest hours are {plan.cheapest_hours} "
                          f"({plan.cheapest_price:.2f} EUR/MWh).")  # fmt: skip
    others = [s.title for s in briefing.events[1:4]]
    return NewsStory(headline=headline, standfirst=standfirst, paragraphs=paragraphs[:4],
                     in_brief=others, written_by="template")  # fmt: skip
