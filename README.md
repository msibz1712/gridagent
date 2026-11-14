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
```

Layout: `agents/<name>/README.md` plus one folder per semantic version,
`agents/<name>/<version>/{agent.yaml, system_prompt.md}`.

## Website

```bash
uv sync --extra web                        # optional extra: FastAPI + uvicorn
set -a && source .env && set +a            # the website needs the API key
uv run --extra web garden ui               # then open http://localhost:8000
```

A clean question-and-answer site for researchers and analysts: ask a question and
get the answer with the period, a chart of the underlying data (CSV download), a
plain-language "How this was calculated" section and linked sources, plus an About
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
