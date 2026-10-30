# Phase 1 — Foundation: implementation report

**Date:** 2026-10-29

**Scope:** Spec §27 Phase 1 only. No strategies, AI decisions, portfolio execution, or brokerage
integrations were implemented.

**Verdict: implemented, but not formally complete.** Seven of the nine acceptance criteria are
verified. Two remain open because this machine has no Docker and no Redis server:

* "Repository starts locally" is verified only partly (host run, not `docker compose up`).
* "Redis connects successfully" is not verified.

See [§5](#5-acceptance-criteria) and [§8](#8-unresolved-issues).

---

## 1. Starting state

* **Workspace:** It held only `ATLAS_MASTER_SPEC_V2.md.md`. No git repository and no project
  files.
* **Toolchain present:** Git 2.55, Python 3.13 (plus 3.11), standalone pnpm 12.3.4.
* **Toolchain missing:** Docker, Node.js, PostgreSQL, Redis, uv.

## 2. Phase 1 tasks and dependencies

| # | Task (spec §27) | Depends on | Status |
|---|---|---|---|
| 1 | Initialize Git | — | ✅ `git init -b main` (nothing committed yet) |
| 2 | Repository structure | 1 | ✅ |
| 3 | `.gitignore` | 1 | ✅ (+ `.gitattributes`, `.dockerignore`) |
| 4 | `pyproject.toml` | 2 | ✅ |
| 5 | Python dependency management + lock file | 4 | ✅ uv, `uv.lock` |
| 6 | React/TypeScript frontend | 2 | ✅ Vite + React 19 + TS, `pnpm-lock.yaml` |
| 7 | Docker Compose | 5, 6 | ✅ written · ⚠️ not executed (no Docker) |
| 8 | PostgreSQL | 7 | ✅ compose service · verified against PostgreSQL 16.2 |
| 9 | Redis | 7 | ✅ compose service + client · ⚠️ live connection not verified |
| 10 | Environment validation | 4 | ✅ |
| 11 | Structured JSON logging, UTC | 10 | ✅ |
| 12 | Alembic | 5, 13 | ✅ |
| 13 | SQLAlchemy connectivity | 8, 10 | ✅ |
| 14 | Foundation schemas: assets, portfolio accounts, audit logs | 12, 13 | ✅ migration `0001` |
| 15 | FastAPI skeleton | 10, 11 | ✅ |
| 16 | `/health` and readiness | 13, 9, 15 | ✅ `/health`, `/ready` |
| 17 | Dashboard shell | 6, 16 | ✅ |
| 18 | pytest + frontend test structure | 5, 6 | ✅ |
| 19 | Initial documentation | all | ✅ |

## 3. Files created

All files are new; there was no existing code to modify. The spec file is untouched.

**Root**
* `.env.example`, `.gitignore`, `.gitattributes`, `.dockerignore`
* `pyproject.toml`, `uv.lock`, `alembic.ini`
* `docker-compose.yml`, `README.md`

**docker/**
* `backend.Dockerfile` (runtime + `dev` test stage)
* `frontend.Dockerfile`

**backend/alembic/**
* `env.py`, `script.py.mako`, `versions/0001_foundation_schema.py`

**backend/app/**
* `__init__.py`, `__main__.py` (validated entry point), `main.py` (app factory)
* `api/`: `__init__.py`, `health.py`, `middleware.py`, `v1/__init__.py`
* `core/`
  * `config/`: `__init__.py`, `settings.py`
  * `logging/__init__.py`
  * `security/`: `__init__.py`, `paper_only.py`, `redaction.py`
  * `exceptions/__init__.py`, `clocks/__init__.py`, `redis_client.py`
* `database/`
  * `__init__.py`, `sessions.py`
  * `models/`: `__init__.py`, `base.py`, `asset.py`, `portfolio_account.py`, `audit_log.py`
  * `repositories/`: `__init__.py`, `audit_log.py`
* `monitoring/`: `__init__.py`, `health.py`
* Documented placeholder packages (docstring only): `events`, `workers`, `data_engine`,
  `asset_discovery`, `market_intelligence`, `feature_engine`, `strategies`, `ai_engine`,
  `ml_engine`, `portfolio`, `accounting`, `risk`, `execution`, `paper_broker`, `news`,
  `backtesting`, `learning`, `notifications`
* `tests/conftest.py`
* `tests/unit/`: `test_api.py`, `test_clocks.py`, `test_config.py`, `test_entrypoint.py`,
  `test_health_checks.py`, `test_logging.py`, `test_models.py`, `test_redaction.py`
* `tests/integration/`: `conftest.py`, `test_migrations.py`, `test_schema_integrity.py`,
  `test_audit_log.py`, `test_infrastructure.py`

**frontend/**
* Project files: `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, `tsconfig.json`,
  `vite.config.ts`, `index.html`, `.dockerignore`, `public/atlas.svg`
* `src/main.tsx`, `src/App.tsx`, `src/navigation.ts`
* `src/layouts/DashboardLayout.tsx`
* `src/components/`: `PaperModeBadge.tsx`, `StatusPill.tsx`, `SystemStatusIndicator.tsx`
* `src/pages/`: `SystemHealthPage.tsx`, `SectionPlaceholderPage.tsx`
* `src/hooks/useSystemStatus.ts`, `src/services/api.ts`, `src/types/health.ts`,
  `src/styles/index.css`
* Tests: `src/App.test.tsx`, `src/pages/SystemHealthPage.test.tsx`, `src/test/setup.ts`,
  `src/test/fixtures.ts`

**Other directories**
* `docs/`: `architecture.md`, `decisions.md`, `operations.md`, `phase-1-report.md`
* `config/README.md`, `scripts/check.sh`, `data/.gitkeep`, `logs/.gitkeep`

## 4. Features implemented

**Configuration** (`core/config`)
* Typed `ATLAS_*` settings, validated at startup.
* Startup refuses:
  * missing or invalid values;
  * unknown `ATLAS_*` keys (typos, or e.g. `ATLAS_LIVE_TRADING`) from the environment or `.env`;
  * development passwords or `DEBUG` logging in the `paper` environment;
  * credentials embedded in the Redis URL.
* Secrets are `SecretStr` and masked in `repr`. The database URL masks its password when
  rendered.
* `python -m app` exits with code 2 and a readable error on bad configuration.

**Logging** (`core/logging`)
* One JSON object per line, with ISO-8601 UTC timestamps (`…Z`).
* `service` and `environment` fields on every record.
* Request ID bound to all logs in a request.
* stdlib and uvicorn logs go through the same pipeline.
* Redaction of secret-named keys, URL credentials and `key=value` secrets.
* uvicorn's access log is replaced by a middleware line that omits query strings.

**API**
* `GET /health` is liveness. It touches no dependencies and reports the `PAPER` trading mode.
* `GET /ready` is readiness: PostgreSQL ping, schema exactly at the code's Alembic head, and
  Redis `PING`. It returns 503 with redacted per-component detail on failure.
* Every response carries `X-Request-ID`.
* Unhandled errors return a generic JSON 500.
* CORS applies only to configured origins and allows only `GET`.

**Database**
* SQLAlchemy 2 (sync) + psycopg 3.
* Every session is pinned to `timezone=UTC` and has a statement timeout.
* `session_scope` provides commit/rollback units of work.

**Schema** (migration `0001`)
* `assets` and `asset_identifiers`: provider symbols kept separate from Atlas IDs, with
  validity periods.
* `portfolio_accounts`: PAPER-only at the database level, with no balance columns.
* `audit_logs`: append-only through triggers, with automatic redaction and request IDs.

**Paper-only isolation**
* A code constant, with no mode setting.
* A database constraint on `portfolio_accounts.account_type`.
* An always-visible "Paper trading · Simulated" badge in the UI.

**Dashboard**
* Navigation entries for all 11 spec §22 sections.
* A live System Health page with a 15 s poll.
* Header readiness indicator: Ready, Degraded or API unreachable.
* The other sections are honest "No data yet — Phase N" placeholders. They show no fabricated
  numbers.

## 5. Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Repository starts locally | ⚠️ **Partly** | `python -m app` and `pnpm dev` started on the host and served real traffic. `docker compose up` **not run**: Docker is not installed. |
| Database connects successfully | ✅ | API `/ready` → `database: ok` against PostgreSQL 16.2. Integration suite ran against the same server. |
| Redis connects successfully | ❌ **Not verified** | No Redis server available on this machine. The failure path is verified (`/ready` → 503, `redis: error`). The real round-trip test exists (`test_real_redis_round_trip`) and runs under Compose. |
| Migrations run | ✅ | `alembic upgrade head`, `downgrade base` and `upgrade head` all pass. `alembic check` reports no drift. All are also automated in tests. |
| Health and readiness endpoints function | ✅ | `curl` against the running server: `/health` 200, `/ready` 503 with database and migrations `ok` and redis `error`. Unit-tested for the 200 path. |
| Frontend loads | ✅ | Vite dev server viewed in a browser. System Health showed live component status. No console errors besides the expected 503. |
| Environment configuration validates | ✅ | 25 config tests. `ATLAS_ENVIRONMENT=live python -m app` → exit 2 with a field-level message. |
| Logging operates | ✅ | JSON UTC log lines observed from the running server. Password redacted in the startup log. 7 logging tests. |
| Initial tests pass | ✅ | Backend 99 passed (1 Redis test deselected). Frontend 10 passed. |

## 6. Commands executed (main ones)

**Tooling** (uv and the scratch PostgreSQL live only in the session scratchpad, not in the repo)
* `git init -b main`
* `python -m venv <scratch>/toolenv` and `pip install uv` (uv 0.13.6)
* `py -3.11 -m venv <scratch>/pgenv` and `pip install pgserver`. This provides PostgreSQL
  16.2 binaries.
* `initdb …`, `pg_ctl … start` (port 55432), `psql -c "create database atlas_dev"`
* The bundled build lacked tz data. I copied IANA zoneinfo from Python's `tzdata` package into
  it, then ran `pg_ctl restart`.

**Python and database**
* `uv lock`, `uv sync`, `uv sync --reinstall-package atlas-ai-trader`
* `alembic revision --autogenerate` (draft, then hand-rewritten)
* `alembic upgrade head`, `alembic check`, `alembic downgrade base`, `alembic upgrade head`,
  `alembic current`
* `ruff format`, `ruff check`, `mypy`
* `pytest --cov` with `ATLAS_TEST_DATABASE_URL` set and `ATLAS_REQUIRE_INTEGRATION=1`
* `python -m app`, then `curl /health`, `curl /ready`, `curl /health?token=abc` (log-leak
  check)
* `ATLAS_ENVIRONMENT=live python -m app`

**Frontend**
* `pnpm add …` and `pnpm add -D …`. pnpm also downloaded Node 22.23.4 project-locally via
  `devEngines`.
* `pnpm exec tsc --noEmit`, `pnpm test`, `pnpm build`, `pnpm dev`

## 7. Test results (final run)

| Check | Result |
|---|---|
| `ruff format --check backend` | 65 files formatted |
| `ruff check backend` | All checks passed |
| `mypy` (strict, 63 files) | No issues |
| `pytest` backend | **99 passed**, 1 deselected (Redis), coverage 99 % |
| Frontend `tsc --noEmit` | No errors |
| Frontend `vitest run` | **10 passed** (2 files) |
| Frontend `vite build` | OK (274 kB JS / 88 kB gzip) |

**Backend breakdown**
* **Unit, 76:** config 25, API 10, logging 7, models 15, redaction 8, clocks 5, health checks 5,
  entry point 1.
* **Integration against real PostgreSQL, 23:**
  * migrations: 3
  * schema integrity: 12. Uniqueness incl. `NULLS NOT DISTINCT`, check constraints, PAPER-only
    accounts, current-identifier uniqueness, FK restrict.
  * audit log: 5. UPDATE, DELETE and TRUNCATE are rejected, and redaction is applied.
  * infrastructure: 3. UTC session, statement timeout, rollback, and full `/ready` wiring.

**Bugs found and fixed during verification**
1. Unhandled-exception responses lacked `X-Request-ID`. Errors are now handled inside the
   request middleware.
2. uvicorn's access log duplicated the request logs and could include query strings. It is now
   suppressed.
3. `.env.example` failed validation on a host run, because pydantic-settings rejected the
   non-`ATLAS_` Compose port keys. Fixed with an explicit unknown-`ATLAS_*`-key check
   (decision D-009). A test now loads the shipped `.env.example`.

## 8. Unresolved issues

1. **Docker not installed. Compose, both Dockerfiles and the containerised test run are
   unexecuted.** This is the main gap to Phase 1 sign-off. To close it:
   1. Install Docker Desktop.
   2. Run `cp .env.example .env`.
   3. Run `docker compose up --build`. `/ready` should report all three components `ok`.
   4. Run `docker compose --profile test run --rm backend-tests`. It forces integration tests
      and includes Redis.
2. **Redis connectivity not verified live.** It is covered by step 1.
3. **Database verification used a scratch PostgreSQL 16.2 build (pgserver), not the
   `postgres:16` image.** The engine and SQL are the same major version, but the image itself
   is untested.
4. **uv is not installed system-wide.** I used a scratch copy. Install it per the
   [uv docs](https://docs.astral.sh/uv/) to use the `uv run …` commands and
   `scripts/check.sh`.
5. **Nothing committed.** The git repository is initialised and every file is untracked.
6. **The spec file is named `ATLAS_MASTER_SPEC_V2.md.md` (double extension).** Left as-is.
   Consider renaming it, or moving it to `docs/`.
7. **Starlette `TestClient` deprecation warning is filtered** (D-013). Adding the suggested
   successor package was blocked pending review.
8. **New toolchain majors** (TypeScript 7, React Router 8, Vite 8, Vitest 5) work, but they are
   recent. ESLint is not set up yet.
9. **Authentication for dashboard controls is not implemented.** There are no controls or admin
   endpoints yet. It must land before the first control endpoint (spec §24) at the latest.

## 9. Remaining work before Phase 2

* Run the Docker acceptance steps (8.1) and record the results here.
* Make the initial commit.
* Phase 2 decisions to prepare:
  * first market-data provider (verify licensing, limits, cost per spec §7.1);
  * whether candles justify TimescaleDB (D-005);
  * the background-task system and outbox design (D-006).
