# GridAgent

An evaluated AI agent that answers questions about Germany's electricity system
using public Energy-Charts data (Fraunhofer ISE), plus a mini "Agent Garden"
registry with an evaluation harness.

> Work in progress. Full documentation arrives in milestone M6.

CI runs on every push (`.github/workflows/ci.yml`): lint, format, offline tests, config
validation, oracle evals for every agent version, and a Docker build with smoke tests.

## Quick start

```bash
uv sync                 # create .venv and install pinned dependencies
uv run garden --help    # CLI
uv run pytest           # tests (offline; add `-m live` for real API tests)
uv run python scripts/demo_data_tools.py   # data tools demo (offline, from fixtures)
uv run ruff check .     # lint
uv run ruff format .    # format
```

Ask the agent a question (calls the Claude API, about $0.01 per question):

```bash
cp .env.example .env    # then put your ANTHROPIC_API_KEY in .env
set -a && source .env && set +a
uv run garden run energy_qa "What was the average day-ahead price yesterday?"
uv run garden run energy_qa "When was solar highest in October 2026?" --fixtures --today 2026-11-14
```

Every run is appended as one JSON line to `logs/runs.jsonl`.

## Agent Garden

```bash
uv run garden list                 # registered agents, versions, model, owner, status
uv run garden validate             # check every agent.yaml (exit code 1 if invalid)
uv run garden tools                # tools agents can use
uv run garden new my_agent         # scaffold agents/my_agent/0.1.0 from the template
uv run garden run energy_qa@0.1.0 "..."   # pin a version
uv run garden runs                 # monitoring: recent runs, ok/error rate, latency, cost
uv run garden chat energy_qa       # conversation in the terminal: follow-ups build on earlier answers
```

Layout: `agents/<name>/README.md` plus one folder per semantic version,
`agents/<name>/<version>/{agent.yaml, system_prompt.md}`.

## Website

```bash
uv sync --extra web                        # optional extra: FastAPI + uvicorn
set -a && source .env && set +a            # the website needs the API key
uv run --extra web garden ui               # then open http://localhost:8000
```

A clean question-and-answer site for researchers and analysts. It is a conversation:
follow-up questions ("And the week before?", "How does that compare with solar?") build
on earlier answers (up to 10 questions; kept in memory for 30 minutes). Each answer states
the period and comes with a chart of the underlying data (CSV download), a plain-language "How this was calculated" section and linked sources, plus an About
page on coverage, method and limits. It deliberately shows no model, agent or
evaluation details; those stay in the CLI (`garden eval`, `garden runs`, ...).

Backend: FastAPI (`src/gridagent/web/api.py`; `/api/health`, `/api/config`,
`/api/ask`). Front end: plain HTML/CSS/JS, no build step, no external requests.
Live Energy-Charts data by default (`GARDEN_WEB_FIXTURES=1` for recorded data);
listens on 127.0.0.1 only; spending limit `GARDEN_UI_BUDGET_USD` (default $1.00).

## Evaluations

```bash
uv run garden eval energy_qa                       # offline: replay if recorded, else oracle (free)
uv run garden eval energy_qa --live --record       # real model, asks before spending; saves for replay
uv run garden eval energy_qa --live --record --model claude-haiku-5-5
uv run garden compare reports/<sonnet>.json reports/<haiku>.json   # model vs model
uv run garden compare energy_qa@0.1.0 energy_qa@0.2.0              # version vs version
```

Eval sets live in `agents/<name>/evals.yaml`; evals always use the fixture data and a
fixed "today", so results are reproducible. Reports (Markdown + JSON) go to `reports/`.

## Docker

```bash
docker compose up --build                 # website on http://localhost:8000 (reads .env if present)

docker build -t gridagent .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 gridagent   # website
docker run --rm gridagent eval energy_qa --oracle                  # any garden command
docker run --rm gridagent list
```

Multi-stage image built from `uv.lock` (exact versions), without dev tools, running as a
non-root user, with a health check on `/api/health`. The API key is never part of the
image: it is passed at run time. The port is published to `127.0.0.1` only, because the
website can spend API credit. Logs and reports are mounted from the host in `compose.yaml`.

## Database (optional)

History can be served from PostgreSQL (Supabase in production) instead of the APIs:
faster, no rate limits, any period length. Anything not synced yet, such as today,
falls back to the live APIs, and so does every request if the database is unreachable.

```bash
uv sync --extra db
# .env: DATABASE_URL=postgresql://...   (owner; Supabase: use the session pooler URL)
uv run garden db init                     # schema `energy` (not exposed by Supabase's web API)
uv run garden db create-reader            # read-only role; writes DATABASE_URL_READER to .env
uv run garden db sync --since 2018-01-01 --regional-since 2026-01-01   # first load
uv run garden db sync                     # later: continue, re-syncing the last 7 days
uv run garden db status                   # rows, MB, covered date ranges
```

- One wide table per endpoint (`public_power`, `total_power`, `day_ahead_price`,
  `cross_border_physical`, `cross_border_trading`, `smard_regional`), 15-minute rows,
  values as fixed-point integers (value x 1000), so they come back exactly as the API
  delivered them (tested value for value); 2018 to today is about 190 MB.
- Reads are packed per day (raw 4-byte numbers instead of one field per value), which
  halves the download, and kept in a disk cache keyed by the latest sync of the period.
- `energy.sync_log` records which days were loaded; a request is served from the
  database only if its whole period is covered. Only complete days are synced.
- The app answers with the read-only role; the owner URL is only for `db` commands and is
  kept out of the website container.
- Tests run against a throwaway PostgreSQL 17 (a service container in CI); locally:
  `TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/gridagent_test uv run pytest`.
