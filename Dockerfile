# GridAgent: CLI + website in one image.
#
#   docker build -t gridagent .
#   docker run --rm --env-file .env -p 127.0.0.1:8000:8000 gridagent          # website
#   docker run --rm gridagent eval energy_qa --oracle                         # any garden command
#
# Two stages: the build stage has uv and creates the virtual environment from
# uv.lock (exact versions); the runtime stage only copies that environment,
# so the final image has no build tools. No secrets are baked in: the API key
# is passed at run time (--env-file .env).

# ---------- build stage ----------
FROM python:3.12-slim AS build

# Same uv version as used locally, so uv.lock is read identically.
COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra web --no-install-project

# Then the project itself, installed as a normal (non-editable) package.
COPY src ./src
RUN uv sync --locked --no-dev --extra web --no-editable

# ---------- runtime stage ----------
FROM python:3.12-slim

# Time zone data for Europe/Berlin (dates are German local time).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user, never as root.
RUN useradd --create-home --uid 1000 garden

WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Registry, scaffold template and recorded data (offline evals, GARDEN_WEB_FIXTURES=1).
COPY agents ./agents
COPY templates ./templates
COPY tests/fixtures ./tests/fixtures
RUN mkdir -p logs reports .cache && chown -R garden:garden logs reports .cache

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER garden
EXPOSE 8000

# Docker marks the container unhealthy if the website stops answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

ENTRYPOINT ["garden"]
# Inside the container listen on all interfaces; publish the port to 127.0.0.1 only.
CMD ["ui", "--host", "0.0.0.0", "--port", "8000"]
