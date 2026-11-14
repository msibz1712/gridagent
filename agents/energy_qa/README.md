# energy_qa

Answers questions about Germany's electricity system (generation by source, load and
day-ahead prices) using public data from Energy-Charts (Fraunhofer ISE).

- **Owner:** msibz1712
- **Current version:** 0.2.0 (one folder per version; semantic versioning)
- **Model:** set in `agent.yaml`: `claude-haiku-5-5` since 0.2.0 (0.1.0 uses `claude-sonnet-5-5`)

## What it does

The agent fetches data with tools, computes every number with `compute_stats` (never
in the model's head), and answers in one to three sentences, always stating the period
used and the data source.

| Tool | Purpose |
|---|---|
| `get_generation` | Public net generation by source (+ load), 15-minute MW values, max 93 days |
| `get_day_ahead_price` | Day-ahead price, bidding zone DE-LU, EUR/MWh, 15-minute values |
| `compute_stats` | sum / mean / min / max / count / energy (MWh) / share (%), optionally per day |

Fetched data stays on the server behind a short `ref` (e.g. `gen_1`); only summaries
and computed results enter the model's context.

## Example questions

- "What share of Germany's electricity came from wind last week?"
- "When was solar generation highest in October 2026?"
- "What was the average day-ahead price yesterday?"
- "Which day last week had the highest average price?"
- "How much electricity did lignite generate in October 2026, in TWh?"

## Definitions

- **Share of generation** follows Energy-Charts: public net generation, excluding
  pumped-storage output; 50% of waste counts as renewable. Verified against
  Energy-Charts' own `renewable_share_of_generation` (see `tests/test_sources.py`).
- **Last week** is the previous Monday to Sunday; **yesterday** is the previous calendar
  day; all dates are German local time (Europe/Berlin).
- **Energy** is power integrated over time: sum of 15-minute MW values × 0.25 h.

## Limits

- Germany only; public net generation (no industrial self-supply); DE-LU prices only.
- No forecasts, no investment or trading advice: such questions are declined.
- Recent data is preliminary and may be revised by Energy-Charts.
- At most 93 days per data request; the public API is rate-limited (~2 requests/min),
  so responses are cached locally.
- The model resolves relative dates itself; unusual phrasings ("the week before last
  Easter") can be misread. The answer always states the dates used, so errors are visible.

## Run it

```bash
uv run garden run energy_qa "What was the average day-ahead price yesterday?"
uv run garden run energy_qa@0.1.0 "..." --fixtures --today 2026-11-14   # pin the Sonnet version
```

## Versions

| Version | Changes |
|---|---|
| 0.2.0 | Default model switched to Claude Haiku 5.5 to cut cost (~20x cheaper per token). Prompt and tools unchanged. Quality vs 0.1.0 not yet measured live: run `garden eval energy_qa@0.1.0 --live` and `garden eval energy_qa@0.2.0 --live`, then `garden compare`. |
| 0.1.0 | First version: three tools, Sonnet 5.5 at medium effort. |
