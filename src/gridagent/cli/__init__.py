"""Command-line interface for the Agent Garden (`garden`).

One Typer app, its commands grouped by module:
  agents.py   list, tools, validate, new, run, chat, runs, ui, version
  evals.py    eval (energy_qa), eval-stories (news_writer), compare
  monitor.py  brief, today, anomalies, site build, forecast-skill, price-backtest
  data.py     db init|create-reader|status|sync, mastr sync|upload|export
  common.py   the app, the console, paths, the API-key and database helpers

Paths are relative to the current directory and can be overridden with
environment variables (useful in Docker):
  GARDEN_AGENTS_DIR (default: agents), GARDEN_TEMPLATE_DIR (default:
  templates/agent_template), GARDEN_LOG_DIR (default: logs),
  GARDEN_CACHE_DIR (default: .cache)
Database (optional, `db` extra): DATABASE_URL (owner: schema + sync),
  DATABASE_URL_READER (read-only role used for answering, if set).
"""

# Importing the command modules registers their commands on `app`.
from gridagent.cli import agents, data, evals, monitor  # noqa: F401
from gridagent.cli.common import app

__all__ = ["app"]
