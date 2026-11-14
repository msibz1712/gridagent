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
