# Technical decisions

Decisions taken while implementing Phase 1, including the places where the implementation
deviates from the letter of the spec, and why. New phases append entries. Existing entries are
superseded, never edited away.

---

### D-001 Synchronous SQLAlchemy 2 with psycopg 3

**Decision:** The backend uses synchronous SQLAlchemy sessions and psycopg 3. FastAPI runs
sync endpoints in its thread pool.

**Why:**
* The critical paths ahead (atomic risk reservations with row locks, double-entry postings)
  are transactional and easier to reason about synchronously.
* There is no I/O fan-out yet that needs async.
* Async can be introduced per-module later without a schema change.

### D-002 uv for Python dependency management, `uv.lock` as the lock file

**Decision:** Python dependencies are managed with uv, and `uv.lock` is the lock file.

**Why:**
* It is fast, standards-based (`pyproject.toml`, PEP 735 dependency groups) and cross-platform.
* Docker builds use `uv sync --frozen` with the same uv version (0.13.6) that produced the lock.

### D-003 Root `pyproject.toml`, package under `backend/app`

**Decision:** `pyproject.toml` stays at the repository root (as in spec §5) and builds the
package from `backend/app` with hatchling.

**Detail:** The project is installed in editable mode, also in Docker, so `alembic.ini` and
`backend/alembic/` stay alongside the code. The readiness check reads the migration head from
`backend/alembic/`.

### D-004 Migrations live in `backend/alembic/`, not `app/database/migrations/`

**Decision:** The spec tree (§5) lists both `backend/alembic/` and `app/database/migrations/`.
Only `backend/alembic/` exists.

**Why:** Two migration locations would invite divergence.

### D-005 TimescaleDB not enabled yet

**Decision:** The stack uses plain `postgres:16`.

**Why:** Spec §21 says to adopt TimescaleDB "where its time-series functionality materially
improves performance". Phase 1 has no time-series tables. Revisit in Phase 2, when candle
volume is known.

**Migration path:** The `timescale/timescaledb:*-pg16` image is a drop-in on the same
PostgreSQL major version.

### D-006 Background task system not selected yet

**Decision:** No Celery or other task system is chosen in Phase 1.

**Why:**
* The spec allows "Celery or another appropriately evaluated background-task system".
* Phase 1 has no workers.
* The choice will be made with the outbox design (spec §4.1) once real workloads exist.
* Redis is provisioned and health-checked now.

### D-007 Enums stored as `VARCHAR` + `CHECK` constraints

**Decision:** Enums are stored as `VARCHAR` columns with `CHECK` constraints, not native
PostgreSQL enum types.

**Why:** Adding a value is a constraint swap inside a normal transactional migration. Native
enum alterations are awkward to roll back.

### D-008 No trading-mode setting

**Decision:** There is no trading-mode setting. Paper mode is the code constant
`app.core.security.TRADING_MODE`.

**Why:** Spec §24.1 forbids a "hidden live-trading configuration switch". A setting with only
one allowed value would still be a switch waiting for a second value.

**Guards:**
* Unknown `ATLAS_*` variables (e.g. `ATLAS_LIVE_TRADING`) abort startup.
* The database constrains `portfolio_accounts.account_type` to `PAPER`.

### D-009 Strict configuration with a shared `.env`

**Decision:** The backend ignores `.env` keys outside its `ATLAS_` namespace, but rejects any
unrecognised `ATLAS_*` key, whether from the environment or from `.env`.

**Why:**
* `.env` also feeds Docker Compose (host-port variables), so non-`ATLAS_` keys must be allowed.
* `ATLAS_TEST_*` and `ATLAS_REQUIRE_INTEGRATION` are reserved for the test harness.

### D-010 Readiness requires the schema to be at the code's migration head

**Decision:** `/ready` fails while migrations are pending, or when the database is ahead of
the code.

**Why:** A mismatched schema is a real readiness failure for a system whose correctness
depends on constraints.

### D-011 Audit log immutability enforced in the database

**Decision:** Triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on `audit_logs`.

**Why:** The spec requires that historical records are never silently modified (§16.1, §21).
Enforcing it in the database also covers ad-hoc SQL and future code paths.

### D-012 Frontend toolchain versions

**Decision:** The dashboard uses the current major versions at implementation time:

| Package | Version |
|---|---|
| React | 19 |
| React Router | 8 |
| Vite | 8 |
| TypeScript | 7 |
| Tailwind | 4 |
| Vitest | 5 |

**Version pinning:**
* `pnpm-lock.yaml` pins exact versions.
* `pnpm-workspace.yaml` sets `minimumReleaseAge` to 7 days, so freshly published (possibly
  compromised) versions are never installed.
* Node.js 22 LTS is pinned through `devEngines.runtime`, and pnpm downloads it per-project.

**Not yet added:** ESLint, to keep Phase 1 dependencies minimal. Type checking (`tsc --noEmit`)
and tests gate the frontend for now.

**Not yet added:** TradingView Lightweight Charts. It arrives with the first chart (Phase 2).

### D-013 Starlette TestClient deprecation warning filtered

**Decision:** The pytest configuration filters the warning only for the test client.

**Context:** Current Starlette warns that using `httpx` with `TestClient` is deprecated and
recommends a successor package. Adopting it was deferred until the package has been reviewed.

**Revisit:** When upgrading Starlette.

---

## Phase 2A — provider architecture

### D-014 Market-data variables keep the names from the Phase 2 brief

**Decision:** Market-data settings use `MARKET_DATA_*` and `HISTORICAL_DATA_*`, and the Alpaca
settings use `ALPACA_*`, instead of the Phase 1 `ATLAS_` prefix.

**Why:**
* The Phase 2 brief specifies these names, and the local `.env` already uses them.
* Each group has its own settings class. Provider settings live in the provider's package
  (`data_engine/providers/alpaca/config.py`).
* Unknown keys under each prefix are rejected as typos, the same guarantee D-009 gives
  `ATLAS_*`.

### D-015 Provider contract, two-layer records, capabilities vs entitlements

**Decision:**
* Every provider implements `data_engine/providers/base.MarketDataProvider`. The registry is
  the only place that maps a provider ID to an adapter.
* Adapters emit `Provider*` records keyed by provider symbol. `Market*` records add Atlas's
  asset ID and a quality status.
* `ProviderCapabilities` is what the adapter can deliver. `FeedAccess` is what the configured
  subscription is entitled to. Combinations the docs do not confirm (e.g. delayed SIP on
  Trading API plans) are denied, not assumed.

**Why:**
* Adapters stay ignorant of the database, and the rest of Atlas stays ignorant of providers.
* Plan limits (Basic: no real-time SIP, 15-minute historical SIP limit, 30 stream symbols)
  are explicit errors, never silent downgrades.

### D-016 Schemas validate structure; the quality engine validates semantics

**Decision:** Schemas enforce types, UTC, finite decimals and the meaning of missing values
(`None`, never 0). They do not check OHLC relationships, negative volume or crossed quotes.

**Why:** An impossible bar must be recorded and quarantined with an explanation by the
data-quality engine (Phase 2 Part 8), not lost as a parse failure.

### D-017 `asset_identifiers` is the asset-provider mapping table

**Decision:** The Phase 1 table keeps its name. Migration 0002 adds:
* the provider FK;
* `provider_exchange`;
* `status`, which must be `RETIRED` exactly when `valid_to` is set;
* two GiST exclusion constraints (`btree_gist`) forbidding overlapping validity periods, per
  (provider, symbol) and per (asset, provider).

**Why:**
* Renaming the table would churn Phase 1 code for no functional gain.
* The exclusion constraints guarantee point-in-time resolution is unambiguous when symbols
  are reused.
* They are created in the migration, because autogenerate cannot represent them, and are
  documented on the model.

**Consequence:** Phase 1 tests that used the made-up provider `example` now use the registered
`alpaca`.

### D-018 Configuration errors never print input values

**Decision:** `describe_validation_error` renders pydantic errors without inputs, and the
market-data and Alpaca loaders raise `InvalidSettingsError` with that sanitized text.
`python -m app` prints only sanitized errors.

**Why:** Pydantic's default message includes `input_value=…`, which would print a malformed
credential or a rejected password.

**Corrects:** a Phase 1 comment that claimed the default output contained no secret values.

### D-019 The Alpaca adapter talks to market-data hosts only

**Decision:** The hosts are `https://data.alpaca.markets` and `wss://stream.data.alpaca.markets`.
They are constants, with no base-URL setting.

**Why:** No configuration can point the adapter at a trading or brokerage endpoint.

**Detail:** Historical requests always send `feed` (Alpaca defaults to `sip` otherwise) and
`adjustment=raw`.
