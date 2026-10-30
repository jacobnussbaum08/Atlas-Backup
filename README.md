> # SOURCE-CODE ARCHIVAL BACKUP: NOT A COMPLETE RUNNABLE ATLAS INSTALLATION
>
> This repository is a **sanitized source-code archive** of the Atlas AI Trader project. It is **incomplete**,
> it **will not run as a working installation**, and it **cannot reproduce or verify any research result**.
>
> * Every commit was rewritten. Commit hashes differ from the original project.
> * Removed from every version: provider market data and anything derived from it, strategy results and
>   performance, backtest outputs, benchmark and spread statistics, per-security research tables,
>   screenshots, databases, logs, models, credentials and private settings.
> * Some code and documents are redacted (`REDACTED_FOR_GITHUB`). Hash-pinned files that had to change are stored
>   as `<name>.redacted`.
> * Hashes cited in the files refer to the original files, which are kept offline.
> * See the `backup/manifests` branch for every modification and exclusion.
>
> The project's original README follows, with its default local password replaced by a placeholder. Its
> set-up instructions describe the original project, not this archive.

# Atlas AI Trader

Autonomous, multi-market quantitative trading **research and paper-trading** platform.

> **Paper trading only.** Atlas has no live-broker integration, no real order
> submission code, and no configuration switch that could enable either. All
> execution is simulated and labelled as such (spec §24.1).

The authoritative specification is [`ATLAS_MASTER_SPEC_V2.md`](ATLAS_MASTER_SPEC_V2.md).
It supersedes all earlier specifications.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 — Foundation | Repo, config, logging, DB + migrations, Redis, API health/readiness, dashboard shell, tests | Implemented. See [docs/phase-reports/phase-1-report.md](docs/phase-reports/phase-1-report.md) |
| 2 — Market data | 2A provider architecture ✅. 2B historical retrieval ✅ (live-verified for AAPL and SPY on IEX). 2C live streaming ✅ (stream worker; live-verified: real IEX quotes, trades and bars for AAPL and SPY persisted). 2D reliability and recovery ✅ (completeness evidence, gap detection, recovery worker; live-verified recovery of real outages from Alpaca's IEX history). 2E dashboard pending | In progress. Reports in [docs/phase-reports/](docs/phase-reports/) |
| 3–11 | Asset discovery → autonomous paper trading | Not started |

## Stack

* **Backend:** Python 3.12+/3.13, FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2 (sync, psycopg 3), Alembic, structlog, pytest. Dependencies locked with [uv](https://docs.astral.sh/uv/) (`uv.lock`).
* **Data stores:** PostgreSQL 16 (source of truth), Redis 7 (cache/coordination only).
* **Frontend:** React 19, TypeScript, Vite, Tailwind CSS 4, React Router, Lucide icons, Vitest + Testing Library. Dependencies locked with pnpm (`frontend/pnpm-lock.yaml`).
* **Local infrastructure:** Docker Compose.

## Quick start (Docker Compose, recommended)

Prerequisite: Docker Desktop (or Docker Engine + Compose v2).

```bash
cp .env.example .env
docker compose up --build
```

This starts PostgreSQL and Redis, runs `alembic upgrade head` once (`migrate` service),
then starts the API and the dashboard:

* Dashboard: <http://127.0.0.1:5173>
* API liveness: <http://127.0.0.1:8000/health>
* API readiness: <http://127.0.0.1:8000/ready> (PostgreSQL, migration head, Redis)
* OpenAPI docs: <http://127.0.0.1:8000/docs>

Stop with `Ctrl+C` or `docker compose down`. Data stays in the `postgres-data` volume.
`docker compose down -v` deletes it.

Run the full backend test suite (including PostgreSQL and Redis integration tests) in containers:

```bash
docker compose --profile test run --rm backend-tests
```

## Running on the host (without containers for the app)

Prerequisites: Python 3.12 or 3.13, [uv](https://docs.astral.sh/uv/), [pnpm](https://pnpm.io/) 10+
(pnpm downloads the pinned Node.js runtime itself), and reachable PostgreSQL and Redis. The simplest
way to get those is `docker compose up -d postgres redis`.

```bash
cp .env.example .env              # ATLAS_POSTGRES_HOST=127.0.0.1 etc.
uv sync                           # creates .venv from uv.lock
uv run alembic upgrade head       # apply migrations
uv run python -m app              # API on http://127.0.0.1:8000
```

```bash
cd frontend
pnpm install
pnpm dev                          # dashboard on http://127.0.0.1:5173, proxies /health, /ready, /api
```

## Tests and checks

```bash
scripts/check.sh                  # everything below in one go
```

```bash
uv run pytest                     # backend unit tests (+ integration tests if configured)
uv run ruff check backend         # lint
uv run mypy                       # strict type check
```

```bash
cd frontend && pnpm test          # dashboard tests
```

Integration tests need a real PostgreSQL server on which they may create and drop throw-away
databases:

```bash
export ATLAS_TEST_DATABASE_URL=postgresql+psycopg://atlas:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/postgres
export ATLAS_TEST_REDIS_URL=redis://127.0.0.1:6379/15      # optional
export ATLAS_REQUIRE_INTEGRATION=1                         # fail instead of skip when unset
uv run pytest
```

## Configuration

Every variable is documented in [`.env.example`](.env.example). The backend validates
configuration at startup and refuses to start on missing values, invalid values, unknown
`ATLAS_*` keys (typos), or development passwords in the `paper` environment.

## Repository layout

```text
backend/
  alembic/                Alembic environment and migration versions
  app/
    main.py               FastAPI application factory
    __main__.py           `python -m app` entry point (validates config first)
    api/                  HTTP layer: /health, /ready, versioned /api/v1
    core/                 config, logging, security (paper-only, redaction), exceptions, clocks, redis
    data_engine/          market-data providers (Alpaca), historical and live ingestion, quality,
                          NYSE calendar, completeness and recovery
    database/             ORM models, repositories, engine/session management
    workers/              background processes (market_data_stream: live stream worker;
                          market_data_recovery: gap detection and backfill)
    monitoring/           dependency health checks
    <future modules>/     documented placeholders (data_engine, risk, accounting, …)
    tests/                unit/ (no services) and integration/ (real PostgreSQL/Redis)
frontend/                 React dashboard shell
docker/                   Dockerfiles
config/                   versioned non-secret configuration (empty in Phase 1)
docs/                     architecture, decisions, operations, phase reports
scripts/                  developer scripts
data/, logs/              runtime output (git-ignored)
```

## Documentation

* [docs/architecture/overview.md](docs/architecture/overview.md): module boundaries and Phase 1 conventions
* [docs/architecture/decisions.md](docs/architecture/decisions.md): technical decisions and deviations from the spec
* [docs/operations.md](docs/operations.md): start, stop, migrations, backup and recovery
* [docs/phase-reports/](docs/phase-reports/): phase implementation and preflight reports
* [docs/prompts/](docs/prompts/): phase instructions as given
