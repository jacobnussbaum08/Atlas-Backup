# Architecture (Phase 1)

Atlas is a **modular monolith with separate background workers** (spec §3.1): one code base and
one deployable backend, with modules separated by package boundaries rather than network hops.

## Module map

| Package (`backend/app/…`) | Responsibility | Phase |
|---|---|---|
| `core.config` | Validated `ATLAS_*` settings; fails fast on invalid/unknown keys | 1 ✅ |
| `core.logging` | structlog JSON logging, UTC timestamps, request context, secret redaction | 1 ✅ |
| `core.security` | Paper-only constant (`TRADING_MODE = PAPER`), redaction helpers | 1 ✅ |
| `core.exceptions` | Exception hierarchy and HTTP error mapping (no internal detail leaks) | 1 ✅ |
| `core.clocks` | Injectable UTC `Clock` (`SystemClock`, `FixedClock` for tests) | 1 ✅ |
| `core.redis_client` | Redis client factory (cache/coordination only) | 1 ✅ |
| `database` | ORM models, repositories, engine/session management | 1 ✅ (foundation tables) |
| `monitoring` | Dependency health checks used by `/ready` | 1 ✅ |
| `api` | `/health`, `/ready`, versioned `/api/v1` router | 1 ✅ |
| `events`, `workers` | Transactional outbox, background task system | later |
| `data_engine` | Provider adapters, normalisation, validation, storage | 2 |
| `asset_discovery`, `market_intelligence`, `feature_engine` | Universe, regimes, features | 3 |
| `strategies` | Strategy proposals (no execution access) | 4 |
| `ai_engine`, `ml_engine`, `news` | Decision coordinator, ML models, async LLM features | 5 |
| `portfolio`, `accounting`, `risk` | Portfolio state, double-entry ledger, deterministic risk | 6 |
| `execution`, `paper_broker` | Order state machine, simulated broker | 7 |
| `backtesting`, `learning` | Backtests, champion/challenger | 8 |
| `notifications` | Dashboard notifications, later Telegram/email | 9+ |

Future modules exist only as packages with a docstring that states their boundary. They contain
no code.

## Request and startup flow

```text
python -m app
  └─ get_settings()            reject unknown ATLAS_* keys, validate values (exit 2 on failure)
  └─ uvicorn → create_app()
       ├─ configure_logging()  JSON, UTC, redaction; uvicorn logs routed through it
       ├─ middleware           X-Request-ID (validated or generated), one access log line per request
       └─ lifespan
            ├─ build_engine()  psycopg 3, pool_pre_ping, timezone=UTC, statement_timeout
            ├─ build_redis()   timeouts, no credentials in URL
            └─ health checks   database · migrations (schema == code head) · redis
```

* `GET /health` is **liveness**. It answers whether the process is up and touches no dependency.
* `GET /ready` is **readiness**. It returns 200 only when PostgreSQL answers, the schema is
  exactly at the migration head shipped with the code, and Redis answers `PING`. Otherwise it
  returns 503 with a per-component report. Failure details are single-line and redacted.

## Data conventions established in Phase 1

* **UTC everywhere.** Every timestamp column is `TIMESTAMPTZ`, and every DB session runs with
  `timezone=UTC`. Application code takes time from a `Clock`, and naive datetimes are rejected.
* **Money is never float.** `Decimal` maps to `NUMERIC`. A metadata test fails if any `Float`
  column appears.
* **Deterministic constraint names** via a SQLAlchemy naming convention, so migrations are
  reproducible. `alembic check` (run in the integration tests) proves models and migrations
  do not drift.
* **Enums as `VARCHAR` + `CHECK`**, not native PostgreSQL enums. They are easier to extend
  through migrations.
* **Append-only audit log.** The `audit_logs` table rejects `UPDATE`, `DELETE` and `TRUNCATE`
  through database triggers, so the guarantee does not depend on application code. The
  trigger function `atlas_reject_modification()` is reusable for later immutable tables
  (journal entries, fills).
* **Portfolio accounts carry no balances.** The ledger (Phase 6) is authoritative, and any
  account summary is derived from it.
* **Paper-only at every layer.**
  * Code has the `TRADING_MODE` constant and no mode setting.
  * Configuration rejects unknown `ATLAS_*` keys such as `ATLAS_LIVE_TRADING`.
  * The database constrains `portfolio_accounts.account_type` with `CHECK (account_type IN ('PAPER'))`.
  * The UI shows an always-visible "Paper trading · Simulated" badge.

## Foundation schema (migration `0001`)

| Table | Purpose | Key integrity rules |
|---|---|---|
| `assets` | Atlas's provider-independent instrument identity | unique (symbol, asset_class, exchange_code) with `NULLS NOT DISTINCT`. Upper-case symbol and currencies. Forex/crypto require a base currency |
| `asset_identifiers` | Provider symbols mapped to Atlas assets, with validity periods | FK `RESTRICT`. `valid_to > valid_from`. At most one *current* mapping per (provider, symbol) |
| `portfolio_accounts` | Identity and future lock target for portfolio state | `account_type = 'PAPER'` only. ISO-4217-style base currency. Unique name |
| `audit_logs` | Immutable audit trail (actor, action, entity, before/after state, reason, request ID) | Identity PK generated always. Append-only triggers. Secrets redacted before insert |

## Frontend

The React dashboard shell has a navigation entry for every section in spec §22:

* **System Health** is live. It polls `/health` and `/ready` every 15 s.
* **The other sections** are explicit "No data yet — Phase N" placeholders. They never show
  invented numbers.
* **Routing.** The Vite dev server proxies `/health`, `/ready` and `/api` to the backend, so the
  browser talks to a single origin.
