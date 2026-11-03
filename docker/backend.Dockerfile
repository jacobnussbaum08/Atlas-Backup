# Atlas backend image (API server, migrations, tests).
# Build context: repository root.

FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# Same uv version used to produce uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

WORKDIR /srv/atlas

# Dependencies first (cached layer), strictly from the lock file.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code. The project is installed in editable mode so the Alembic
# migrations and alembic.ini stay next to the code they belong to.
COPY alembic.ini ./
COPY config ./config
COPY backend ./backend
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --home-dir /srv/atlas atlas     # ML model artifacts (Phase 5A, ML_ARTIFACT_DIR); compose mounts a volume here.
    && mkdir -p /srv/atlas/data/ml/artifacts     && chown -R atlas /srv/atlas/data
USER atlas

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["python", "-m", "app"]

# ---------------------------------------------------------------------------
# Test image: adds the dev dependency group (pytest, ruff, mypy).
FROM base AS dev
USER root
RUN uv sync --frozen
USER atlas
CMD ["pytest"]
