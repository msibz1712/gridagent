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

## Limits

- The checks are rules, not judgment: a story can pass them and still lead with the
  less interesting item. The ranking comes from the anomaly engine's score.
- Rounding a fact-sheet number is allowed (559.63 -> 560); anything else is not.

## Versions

| Version | Changes |
|---|---|
| 0.3.0 | After reviewing 0.2.0's live stories: never mention the inputs (one paragraph said "the fact sheet does not show"); "since at least" is never "the start of the data" (the fact-sheet field was renamed to say what it is); published day-ahead prices are results, not expectations. |
| 0.2.0 | After the first live stories (15 Nov, 9 Nov, 24 Oct): max_tokens 8000 (2000 ran out while thinking, so one draft arrived with only a headline); at most 80 words and five numbers per paragraph (paragraphs had become number lists); "record" banned (a headline said "sets a record" for "highest since at least"). |
| 0.1.0 | First version: Haiku 5.5, style guide, `publish_story`, one revision, template fallback. |
