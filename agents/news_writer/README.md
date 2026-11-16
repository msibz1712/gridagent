# news_writer

Writes GridAgent's daily briefing as a short news story: headline, standfirst, three
paragraphs and short items. The second agent in the Agent Garden, next to `energy_qa`.

## How it works

- **Input:** a fact sheet built in code from the daily briefing (`monitor/writer.py`):
  key figures with their 4-week normal, the stories the anomaly engine ranked (with
  their facts, and whether the merit order explains a price event), the forecast, and
  the price plan. Every number the story may use is in it.
- **No data tools.** The agent cannot fetch or calculate anything; it writes. It hands
  the story over through its own `publish_story` tool, so the structure is guaranteed.
- **Style guide = system prompt:** inverted pyramid, comparisons with every key number,
  associations not causes, no hype, no quotes, no advice, "since at least" kept.

## The desk check (before anything is published)

Every number backed by the fact sheet (the same grounding check as every answer), no
causal wording, no hype words, no quotation marks, headline at most 80 characters,
standfirst at most 35 words, two to four paragraphs. A failed draft goes back once
with the problems listed; if it fails again, the plain template version from the same
fact sheet is published instead and the rejected draft's problems are recorded.

## Evaluation

`garden eval-stories` runs the 17 briefings in `evals/` (15 real days, 2 edited to tempt a
broken rule) and grades each story in code (`src/gridagent/evals/stories.py`): published
by the model, lead story first, merit-order reading, "since at least" kept, comparison in
the standfirst, no "will" or advice, outlook only when there is one, at most three short
items, a quiet day called quiet. Modes: `--template` (the code-only baseline), `--live
--record` (real model, about $0.04), and replay (default; free; run in CI for the current
version).

| Version | Pass rate | Published | First draft accepted | Finding |
|---|---|---|---|---|
| 0.3.0 | 94.1% (16/17) | 100% | 88% | a quiet day framed as news instead of called quiet |
| 0.4.0 | 94.1% (16/17) | 100% | 82% | quiet day fixed; one story led with the daily average instead of the evening spike ranked first (0.3.0 got that day right: run-to-run variation, not shown to be the new rule) |

## Limits

- The checks are rules, not judgment: a story can pass them and still read badly. Style
  and readability are not graded; the ranking of stories comes from the anomaly engine.
- Rounding a fact-sheet number is allowed (559.63 -> 560); anything else is not.

## Versions

| Version | Changes |
|---|---|
| 0.4.0 | After the first story eval (0.3.0: 16/17): a day without stories is called quiet in the headline or standfirst, and an ordinary difference from normal is not presented as news (0.3.0 headlined a quiet day "Average price falls 8.8% below normal"). |
| 0.3.0 | After reviewing 0.2.0's live stories: never mention the inputs (one paragraph said "the fact sheet does not show"); "since at least" is never "the start of the data" (the fact-sheet field was renamed to say what it is); published day-ahead prices are results, not expectations. |
| 0.2.0 | After the first live stories (15 Nov, 9 Nov, 24 Oct): max_tokens 8000 (2000 ran out while thinking, so one draft arrived with only a headline); at most 80 words and five numbers per paragraph (paragraphs had become number lists); "record" banned (a headline said "sets a record" for "highest since at least"). |
| 0.1.0 | First version: Haiku 5.5, style guide, `publish_story`, one revision, template fallback. |
