# Phase 2A — Provider architecture: implementation report

**Date:** 2026-10-29

**Scope:** Deliverable 2A only:
* provider interface
* Alpaca adapter foundation
* capability model
* standardized schemas
* asset-provider mappings
* database models and migration
* configuration and credential validation
* tests

Out of scope, and not done: historical retrieval, streaming, network calls to Alpaca, and the
quality engine. Strategies, ML, discovery, portfolio and execution are untouched.

**Baseline:** `d7801bf` (Phase 1) and `14ff1b8` (preflight docs) are present and unchanged. All
2A work is uncommitted at the time of writing.

## Result

| Check | Result |
|---|---|
| `ruff format --check` / `ruff check` | 88 files formatted / all checks passed |
| `mypy` (strict, 85 files) | no issues |
| Backend `pytest` against Docker PostgreSQL 16.15 + Redis (`ATLAS_REQUIRE_INTEGRATION=1`) | **236 passed, 0 skipped**, coverage 98 % |
| Frontend `vitest run` (unchanged) | **10 passed** |
| Migration round trip on a scratch DB | `upgrade head` → `alembic check` (no drift) → `downgrade 0001` → `upgrade head` all OK |
| Dev database | the `migrate` container applied `0001 → 0002`, exit 0 |
| Running stack after rebuilding `migrate` + `backend` | `/ready` 200: database, migrations (`at head ['0002']`) and Redis all ok. Volume intact |
| Startup validation inside the container, with the real local `.env` | passed. Logged `single_venue_feed` (warning) and `stream_disabled` (info); no credential value logged |
| Credential scan of all changed and new files | no secret values |

Alpaca's servers were **not** contacted. Authentication has not been verified with the
provider; that is Deliverable 2B.

## Provider architecture

* **Contract.** `data_engine/providers/base.py`, the `MarketDataProvider` ABC, covers:
  * provider identity and the configured feed;
  * `capabilities()` and `feed_access()`;
  * `normalize_symbol()`;
  * `credential_status()` and `validate_configuration()`, both offline;
  * `check_authentication()`, `fetch_bars()` (async page iterator) and `create_stream()`,
    which returns the `MarketDataStream` protocol: connect, subscribe, unsubscribe, events,
    close and state;
  * `aclose()`.

  The contract also defines `HistoricalBarsRequest`, `BarPage`, `StreamSubscription` and
  `ProviderConfigurationReport`.
* **Registry.** `providers/registry.py` is the only provider-ID → adapter mapping.
* **Capabilities vs entitlements.** `capabilities.py` holds `ProviderCapabilities` (feeds with
  coverage, timeframe rules, data types, page size, documentation, `verified_on`) and
  `FeedAccess` (per-plan historical/streaming permission, recency limit, stream symbol limit,
  request rate). Unverified combinations are denied.
* **Schemas.** `schemas.py` has two layers:
  * `Provider{Bar,Quote,Trade}`, keyed by provider symbol and emitted by adapters;
  * `Market{Bar,Quote,Trade}`, which add the asset ID and a quality status;
  * plus `MarketAsset`, `MarketDataEvent`, `ProviderStatus`, `DataQualityEvent` and
    `DataIngestionJob`.

  All timestamps are UTC (naive ones are rejected), numbers are finite `Decimal`s, and a
  missing value is `None`, never 0. Timestamps distinguish `event_time`, `received_at` and
  `available_at` (≥ received); persistence time comes from the database.
* **Timeframes.** `timeframes.py` provides a validated `Timeframe` value object. The initial
  set is 1Min, 5Min, 15Min, 1Hour and 1Day, and adding one needs no code change.
  `fixed_duration` is `None` for session-based bars.
* **Alpaca package** (`providers/alpaca/`, the only Alpaca-aware code):
  * `config.py`: `ALPACA_*` settings. Credentials are optional but must come as a pair, are
    format-checked, and are held in `SecretStr`.
  * `capabilities.py`: facts verified against the official docs on 2026-10-29, and per-plan
    entitlements.
  * `adapter.py`:
    * symbol normalization and offline validation;
    * auth headers `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`;
    * `GET /v2/stocks/bars` query construction, always with an explicit `feed`,
      `adjustment=raw`, `sort=asc`, `limit` ≤ 10000, and a refusal of SIP data newer than
      15 minutes on Basic;
    * stream URL selection;
    * network methods raise `ProviderOperationNotImplementedError` naming 2B or 2C.
* **Mappings.** `database/repositories/asset_mapping.py` provides register, retire,
  point-in-time `resolve` and `resolve_many`, current mapping per asset, and history.

## Database (migration `0002`)

| Object | Change |
|---|---|
| `market_data_providers` | new. Seeded `alpaca` |
| `market_data_feeds` | new. PK (provider, code), coverage `SINGLE_VENUE`/`CONSOLIDATED`, delay. Seeded `iex`, `sip`, `delayed_sip` (900 s) |
| `asset_identifiers` | + FK to `market_data_providers`. + `provider_exchange`, + `status` (`ACTIVE`/`SUSPENDED`/`RETIRED`) with a `RETIRED ⇔ valid_to IS NOT NULL` check. + `btree_gist` exclusion constraints: no overlapping periods per (provider, symbol) or per (asset, provider) |

The downgrade reverses everything except the `btree_gist` extension, which other objects may
use.

## Configuration

| Variable | Default | Validation |
|---|---|---|
| `MARKET_DATA_PROVIDER` | `alpaca` | lower-case ID; must be registered |
| `MARKET_DATA_SYMBOLS` | empty | comma list, upper-cased, symbol grammar, no duplicates |
| `MARKET_DATA_STREAM_ENABLED` | `false` | enabling it requires credentials, symbols, a streamable feed, and ≤ the plan's symbol limit |
| `MARKET_DATA_RECONNECT_MAX_DELAY` | 60 s | (0, 3600] |
| `MARKET_DATA_HEARTBEAT_TIMEOUT` | 30 s | (0, 600] |
| `HISTORICAL_DATA_BATCH_SIZE` | 1000 | ≥ 1; ≤ provider page size (Alpaca 10000) |
| `HISTORICAL_DATA_MAX_RETRIES` | 5 | 0–20 |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | empty | both or neither; no whitespace; printable ASCII; plausible length |
| `ALPACA_DATA_FEED` | `iex` | `iex` \| `sip` \| `delayed_sip` |
| `ALPACA_DATA_PLAN` (new) | `basic` | `basic` \| `algo_trader_plus` |
| `ALPACA_REQUEST_TIMEOUT_SECONDS` (new) | 15 | (0, 120] |

Unknown keys under `MARKET_DATA_`, `HISTORICAL_DATA_` and `ALPACA_` abort startup. Validation
errors never print input values (D-018).

## Files

**Created**

* Migration: `backend/alembic/versions/0002_market_data_providers.py`
* Config: `backend/app/core/config/market_data.py`
* `backend/app/data_engine/`:
  * `enums.py`, `errors.py`, `timeframes.py`, `capabilities.py`, `schemas.py`
  * `providers/__init__.py`, `providers/base.py`, `providers/registry.py`
  * `providers/alpaca/__init__.py`, `config.py`, `capabilities.py`, `adapter.py`
* Database:
  * `backend/app/database/models/market_data.py`
  * `backend/app/database/repositories/asset_mapping.py`
* Unit tests (`backend/app/tests/unit/`): `test_market_data_settings.py`,
  `test_alpaca_config.py`, `test_timeframes.py`, `test_market_data_schemas.py`,
  `test_capabilities.py`, `test_alpaca_adapter.py`
* Integration test: `backend/app/tests/integration/test_market_data_reference.py`
* `docs/phase-reports/phase-2a-provider-architecture-report.md`

**Modified**

* `.env.example`: documented market-data section, plus `ALPACA_DATA_PLAN` and
  `ALPACA_REQUEST_TIMEOUT_SECONDS`
* `backend/app/__main__.py`: validates market-data and provider configuration at startup, and
  prints only sanitized errors
* `backend/app/core/config/settings.py`: generic unknown-key check, `InvalidSettingsError`,
  `describe_validation_error`
* `backend/app/core/config/__init__.py`, `backend/app/database/models/__init__.py`,
  `backend/app/database/repositories/__init__.py`: exports
* `backend/app/database/models/asset.py`: `MappingStatus`, provider FK, `provider_exchange`,
  `status`
* Tests:
  * `backend/app/tests/conftest.py`: isolates the new variables and clears the new caches
  * `test_entrypoint.py`: 4 new startup tests
  * `test_models.py`: the table set now includes the 2 new tables
  * `test_health_checks.py`: the expected head is now `0002`
  * `integration/test_schema_integrity.py`: uses the registered provider `alpaca` instead of
    `example`, and marks the ended mapping `RETIRED` as the new constraint requires
* `docs/architecture/decisions.md`: D-014 to D-019
* `docs/architecture/overview.md`: Market Data Engine section

No Phase 1 test was removed or weakened. The changes above follow schema additions that the
tests deliberately pin.

## Remaining issues / notes for 2B

1. **No live verification.** Alpaca authentication and entitlements are unverified against the
   provider. Basic-plan IEX historical recency and delayed-SIP availability are modeled from
   the docs, conservatively, and must be confirmed in 2B with the opt-in live smoke test.
2. **The universe is not seeded.** `MARKET_DATA_SYMBOLS` only lists symbols. Creating `assets`
   rows needs an asset class (EQUITY vs ETF) and an exchange. Alpaca's asset directory is on
   the trading API, which Atlas does not call (D-019). 2B needs a decision: an operator-
   maintained reference file, or explicit registration.
3. **New variables are not in your local `.env`.** It lacks `MARKET_DATA_SYMBOLS`, so the
   configured universe is empty until you add it. The other new variables have safe defaults.
4. **HTTP client dependency.** `httpx` is currently a dev-only dependency. 2B will promote an
   async HTTP client to a runtime dependency.
5. **Running services.** The `backend` and `migrate` images were rebuilt and the dev database
   migrated to `0002`. The frontend container was not rebuilt (no frontend changes).
6. **Redis healthcheck warning** (preflight addendum): still unfixed.
7. **Alpaca key rotation:** still recommended.
