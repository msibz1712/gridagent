# GridAgent

An evaluated AI agent that answers questions about Germany's electricity system
using public Energy-Charts data (Fraunhofer ISE), plus a mini "Agent Garden"
registry with an evaluation harness.

> Work in progress. Full documentation arrives in milestone M6.

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
```

Layout: `agents/<name>/README.md` plus one folder per semantic version,
`agents/<name>/<version>/{agent.yaml, system_prompt.md}`.

## Web app

```bash
uv sync --extra web                        # optional extra: FastAPI + uvicorn
set -a && source .env && set +a            # only needed for live questions
uv run --extra web garden ui               # then open http://localhost:8000
```

A FastAPI backend (`src/gridagent/web/api.py`, API docs at `/api/docs`) serves a
single-page front end in plain HTML/CSS/JS (no build step, no external requests).
Pages: **Ask** (live model, or a free oracle demo with no API cost; answer, metrics,
a chart of the data behind the answer and a step-by-step trace), **Runs** (monitoring
over `logs/runs.jsonl`), **Evaluations** (reports, comparison, free offline eval) and
**Agents** (registry, READMEs, configs). Light and dark themes; works on mobile.
The server listens on 127.0.0.1 only and enforces a spending limit for live calls
(`GARDEN_UI_BUDGET_USD`, default $1.00).

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
