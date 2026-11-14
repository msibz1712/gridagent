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
