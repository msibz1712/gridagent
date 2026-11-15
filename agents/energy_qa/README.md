# energy_qa

Answers questions about Germany's electricity system (generation by source, load and
day-ahead prices) using public data from Energy-Charts (Fraunhofer ISE).

- **Owner:** msibz1712
- **Current version:** 0.8.0 (one folder per version; semantic versioning)
- **Model:** set in `agent.yaml`: `claude-haiku-5-5` since 0.2.0 (0.1.0 uses `claude-sonnet-5-5`)

## What it does

The agent fetches data with tools, computes every number with `compute_stats` (never
in the model's head), and answers in one to three sentences, always stating the period
used and the data source.

| Tool | Purpose | Source |
|---|---|---|
| `get_generation` | Generation by source + load, 15-min MW; scope public net or total incl. industry | Energy-Charts |
| `get_day_ahead_price` | Day-ahead price DE-LU, EUR/MWh, 15-min | SMARD via Energy-Charts |
| `get_cross_border_flows` | Physical or commercial flows per neighbour + sum, GW (positive = import) | Energy-Charts |
| `get_regional_generation` | Generation/load per control area (50Hertz, Amprion, TenneT, TransnetBW) | SMARD |
| `get_installed_capacity` | Installed capacity (GW) incl. 2030 targets; yearly or monthly | Energy-Charts |
| `get_reference_statistics` | Official figures with definition, source and status | UBA, BDEW/AGEB, netztransparenz |
| `compute_stats` | sum / mean / min / max / count / energy / share / count_below / count_above / capture_price / capacity_factor | — |

Fetched data stays on the server behind a short `ref` (e.g. `gen_1`); only summaries
and computed results enter the model's context.

## Example questions

- "What share of Germany's electricity came from wind last week?"
- "When was solar generation highest in October 2026?"
- "What was the average day-ahead price yesterday?"
- "Which day last week had the highest average price?"
- "How much electricity did lignite generate in October 2026, in TWh?"
- "What was the market value of solar in October 2026, compared with the official value?" (0.3.0)
- "How much did Germany import on balance last week, physical vs commercial?" (0.3.0)
- "Which control area produced the most onshore wind last week?" (0.3.0)
- "What was the capacity factor of onshore wind in October 2026?" (0.3.0)

## Definitions

- **Share of generation** follows Energy-Charts: public net generation, excluding
  pumped-storage output; 50% of waste counts as renewable. Verified against
  Energy-Charts' own `renewable_share_of_generation` (see `tests/test_sources.py`).
- **Last week** is the previous Monday to Sunday; **yesterday** is the previous calendar
  day; all dates are German local time (Europe/Berlin).
- **Energy** is power integrated over time: sum of 15-minute MW values × 0.25 h.
- **Market value / capture price**: day-ahead price weighted by the technology's generation.
  Reproduces the official October 2026 values within 0.4% (solar), 2.9% (onshore) and 5.3%
  (offshore); the TSOs weight by their own feed-in extrapolation.
- **Capacity factor**: generation ÷ (installed capacity at the end of the previous month ×
  hours); solar uses AC (inverter) capacity.
- **Cross-border energy** is net: positive = net import. Physical flows and scheduled
  commercial exchanges differ (2–8 Nov 2026: 95.7 vs 34.0 GWh net import).
- **Control areas** (SMARD) are grid regions, not federal states; they add up exactly to
  Germany, and SMARD's values equal Energy-Charts' (verified in `tests/test_smard.py`).
- **Official figures** (`src/gridagent/data/reference_data.yaml`) are curated with source,
  definition and status; e.g. renewable share 2025: 57.9% of gross generation (BDEW,
  preliminary) vs 55.1% of gross consumption (UBA).

## Limits

- Germany only; public net generation (no industrial self-supply); DE-LU prices only.
- No forecasts, no investment or trading advice: such questions are declined.
- Recent data is preliminary and may be revised by Energy-Charts.
- At most 93 days per data request; the public API is rate-limited (~2 requests/min),
  so responses are cached locally.
- The model resolves relative dates itself; unusual phrasings ("the week before last
  Easter") can be misread. The answer always states the dates used, so errors are visible.
- Weather is a plain mean over 65 DWD stations (16 in Bavaria), measured ~10 m above
  ground: an indicator of how windy or sunny it was, not plant-weighted or at hub height.
  Last week's station wind correlated r = 0.74 with wind generation; solar radiation
  r = 0.99 with solar generation.
- The check for unbacked numbers is a heuristic: it catches numbers no tool produced
  (e.g. a difference worked out mentally), but not arithmetic in words ("about a third").

## Run it

```bash
uv run garden run energy_qa "What was the average day-ahead price yesterday?"
uv run garden run energy_qa@0.1.0 "..." --fixtures --today 2026-11-14   # pin the Sonnet version
```

## Versions

| Version | Changes |
|---|---|
| 0.4.0 | Conversations: follow-up questions build on earlier turns (same period/source/region unless changed, reuse fetched data, every answer still self-contained). Multi-turn eval cases. |
| 0.5.0 | No mental arithmetic: `compare_values` tool for differences, % changes, ratios and sums (only accepts numbers the tools produced). Every answer is checked for numbers no tool result backs (run log `unsupported_numbers`, eval check `supported`). Comparison eval cases. |
| 0.6.0 | Weather: `get_weather` (DWD wind speed and solar radiation, 65 stations) and `compute_when` (calculate on any data in the hours that meet weather conditions, e.g. price in calm hours, *Dunkelflaute* hours). Rules: state thresholds, station wind is an indicator, association is not cause. |
| 0.7.0 | Neighbouring markets: `get_neighbour_price` and `get_price_spread` (Germany minus neighbour) for the 11 neighbouring zones published under CC BY 4.0; spreads work with compute_stats and compute_when (e.g. imports from France when Germany was dearer). Prompt: comparisons with numbers from earlier answers also go through `compare_values`. |
| 0.8.0 | Congestion management: `get_redispatch` (netztransparenz.de, OAuth; redispatch and countertrading as totals by direction, energy type and TSO, plus 15-minute series for compute_stats / compute_when). Test runs excluded; series spread each measure's energy evenly (stated as approximate). Evals use synthetic redispatch data (licence). |
| 0.11.0 | Price-event investigator: `investigate_price_event` compares hours of one day with the same clock hours on comparable days (weekday/weekend) in the 4 weeks before; a merit-order estimate (price fitted on residual load) separates what residual load explains from the rest; conditions (residual load, wind, solar, demand, weather) are kept apart from system responses (gas, coal, storage, imports). Prompt: four-part answer, associations not causes, scarcity caveat at the top of the merit order. |
| 0.10.0 | Regional plant register: `get_plant_register` on the Marktstammdatenregister (Bundesnetzagentur, dl-de/by-2-0): installed capacity, additions, closures and planned units by state, district and municipality and by year, community wind. `garden mastr sync` reads only the needed files of the 3.3 GB daily export via HTTP range requests; `garden mastr export` writes a municipality x year panel for research. Prompt: German state names, preliminary current year (late registrations). |
| 0.9.0 | Flexibility: `simulate_battery` (day-ahead arbitrage of a battery, each day optimised exactly by dynamic programming, starting and ending empty; profit, cycles, buy/sell prices, schedule as a ref) and `plan_flexible_load` (cheapest intervals for a shiftable consumption vs. running evenly). Works on German and neighbour prices. Prompt: default size 1 MW / 2 MWh, state assumptions, upper bound for day-ahead only; calculating is in scope, investment advice is not. |
| 0.3.0 | Stage 1 data: cross-border flows, control areas (SMARD), installed capacity and targets, total generation incl. industry, official reference figures; capture prices, threshold counts, capacity factors; prompt asks to state definitions and official figures. 13 new eval cases. |
| 0.2.0 | Default model switched to Claude Haiku 5.5 to cut cost (~20x cheaper per token). Prompt and tools unchanged. Quality vs 0.1.0 not yet measured live: run `garden eval energy_qa@0.1.0 --live` and `garden eval energy_qa@0.2.0 --live`, then `garden compare`. |
| 0.1.0 | First version: three tools, Sonnet 5.5 at medium effort. |
