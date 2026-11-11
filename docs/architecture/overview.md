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
| `data_engine` | Provider contract, capabilities, schemas, Alpaca adapter (2A); historical retrieval, quality checks, storage, API, CLI (2B); streaming (2C) | 2 🚧 |
| `asset_discovery`, `market_intelligence`, `feature_engine` | Universe, regimes, features; central feature registry with versioned definitions (4A) | 3 ✅, registry 4A ✅ |
| `news` | News provider contract and Alpaca adapter (REST + stream), append-only articles and revisions, effective-dated asset mapping, versioned keyword classification, relevance, economic-calendar interface (no provider); `workers.news_stream` | 3C ✅ |
| `strategies` | Strategy interface and six strategies (4B logic), immutable versions and configurations, point-in-time feature snapshots and eligibility, idempotent evaluation engine with signal states, standardized proposals and recorded conflicts (no execution access); `workers.strategy_evaluation` | 4A ✅, 4B ✅ |
| `ai_engine`, `ml_engine` | Decision coordinator, ML models, async LLM news features | 5 |
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

## Market Data Engine — provider architecture (Phase 2A)

```text
MarketDataSettings (MARKET_DATA_*, HISTORICAL_DATA_*)
        |
        v
providers/registry.create_provider ──> AlpacaMarketDataProvider (ALPACA_*)
        |                                   | implements
        v                                   v
providers/base.MarketDataProvider  <── contract: feed, capabilities(), feed_access(),
        |                                normalize_symbol(), validate_configuration(),
        |                                check_authentication(), fetch_bars(), create_stream()
        v
schemas: Provider{Bar,Quote,Trade}  ──(asset mapping)──>  Market{Bar,Quote,Trade}
                                         ^
         database/repositories/asset_mapping (point-in-time provider symbol -> asset ID)
```

* **Isolation.** Provider-specific code lives only in `data_engine/providers/<provider>/`.
  Other modules see the contract, the capability model (`capabilities.py`), the enums and
  `schemas.py`.
* **Capabilities vs entitlements.** `ProviderCapabilities` says what an adapter can deliver
  (feeds, coverage, timeframes, data types, page size, documentation, date verified).
  `FeedAccess` says what the configured plan allows (historical/streaming, recency limits,
  stream symbol limit, request rate).
* **Feeds.**
  * The configured feed is the only feed used.
  * IEX is labelled `SINGLE_VENUE` and SIP `CONSOLIDATED`, both in code and in the
    `market_data_feeds` table.
  * Configuration that needs another feed or plan fails with `UnsupportedCapabilityError`.
* **Configuration check at startup.** `validate_configuration` is offline. `python -m app`
  logs its warnings and refuses to start on errors. Errors include half-set credentials,
  streaming without credentials, an unstreamable feed, too many stream symbols, an oversized
  batch, and invalid symbols.
* **Not yet built.** Network operations raise `ProviderOperationNotImplementedError` until 2B
  (historical retrieval, authentication check) and 2C (streaming).

Migration `0002` adds:

| Table | Purpose |
|---|---|
| `market_data_providers` | Registered providers (seeded: `alpaca`) |
| `market_data_feeds` | Feeds per provider with coverage and delay (seeded: `iex`, `sip`, `delayed_sip`). A test checks they equal the adapter's declared feeds |
| `asset_identifiers` (extended) | Provider FK, `provider_exchange`, `status`, and exclusion constraints so a provider symbol identifies one asset at any instant |

## Historical retrieval pipeline (Phase 2B)

```text
POST /api/v1/market-data/historical-fetch      python -m app.data_engine.cli fetch
                 \                                   /
                  HistoricalFetchRequest (Atlas symbols, timeframe, UTC range)
                          |  resolve assets + current provider mapping
                          |  provider.validate_historical_request()  (feed, plan, timeframe,
                          v                                           adjustment; no I/O)
                  ingestion job + per-symbol rows (PENDING)
                          |
            per symbol:   provider.fetch_bars()  -> GET /v2/stocks/bars (explicit feed,
                          |   explicit adjustment, paced, retried, every page_token)
                          v
            per page, one transaction:
                raw response (verbatim, sha256)
                -> reject: malformed, wrong symbol, outside range, unmapped at bar time,
                   currency mismatch, out of order
                -> assess: VALID / INCOMPLETE / QUARANTINED (+ quality events)
                -> dedupe vs latest stored revision (advisory lock per series)
                -> insert new bars / revisions, update counters
                          v
            symbol COMPLETED | NO_DATA | FAILED  ->  job COMPLETED | PARTIALLY_COMPLETED | FAILED
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/market-data/historical-fetch` | Create a job and run it in the background (operator token, D-027) |
| `GET /api/v1/market-data/ingestion-jobs[?status=FAILED]` | Jobs, newest first, with per-symbol results |
| `GET /api/v1/market-data/ingestion-jobs/{job_id}` | One job |
| `GET /api/v1/market-data/candles` | Current revision of bars for one series (`feed`, `adjustment`, `quality`, `known_at`, cursor `after`) |
| `GET /api/v1/market-data/assets` · `/assets/{id}/history` | Mapped assets and stored ranges |
| `GET /api/v1/market-data/quality-events` | Validation findings, failures, capability limitations |
| `GET /api/v1/market-data/providers` | Capabilities and offline configuration report (no secrets) |

Migration `0003` adds the following tables. The last three are append-only:

* `market_data_ingestion_jobs`
* `market_data_ingestion_job_symbols`
* `market_data_raw_events`
* `market_bars`
* `market_data_quality_events`

## Live streaming (Phase 2C)

```text
stream-worker process (python -m app.workers.market_data_stream)      FastAPI process
  advisory lock market_data_stream:{provider}:{feed}  (one worker)       |
  load assets + current mappings for MARKET_DATA_SYMBOLS                 | reads only
  record gap since the previous worker (WORKER_NOT_RUNNING)              v
  loop:                                                       market_data_stream_status
    provider.create_stream()  -> wss://stream.data.alpaca.markets/v2/iex  _sessions, _gaps,
    connect: greeting -> auth -> "authenticated"   (auth_state)          quotes, trades,
    subscribe: full desired set -> "subscription" (ACTIVE | PARTIAL)     market_bars
    close open gaps + RECOVERY event                                     |
    events(): frame -> StreamBatch (bars/quotes/trades/notices/malformed)  GET /api/v1/
        -> bounded queue (full 2 s: drop + DATA_DROPPED gap, never silent)    market-data/stream...
        -> writer: <=500 frames / 200 ms, one transaction:
             raw frame verbatim -> reject (malformed, unsubscribed, unmapped)
             -> bars: assess + dedupe/revision (series lock, same rules as REST)
             -> quotes/trades: assess, insert ON CONFLICT DO NOTHING (duplicates)
             -> notices: trade correction/cancel -> quality events
           DB error: retry same group with backoff
    on retryable failure: CONNECTION_LOST gap, backoff, reconnect (subscription restored)
    on permanent failure: FAILED, stays up, no retry
  SIGTERM: close socket, drain queue, STOPPED, release lock
```

| Endpoint (read-only) | Purpose |
|---|---|
| `GET /api/v1/market-data/stream` | Derived health (`HEALTHY`, `IDLE`, `STALE`, `DEGRADED`, `RECONNECTING`, `STARTING`, `FAILED`, `NOT_RUNNING`, `DISABLED`) with reasons, states, subscription, timestamps, errors, reconnects, counters, latency metrics |
| `GET /api/v1/market-data/stream/sessions` | Connection attempts with their timelines |
| `GET /api/v1/market-data/stream/gaps[?open_only=true]` | Intervals in which live data may be missing (input to 2D) |
| `GET /api/v1/market-data/quotes` · `/trades` | Stored live quotes and exchange trades for one asset (`symbol` or `asset_id`, `start`, `end`, `quality`, cursor `after_id`) |

Migration `0004`: `market_quotes`, `market_trades` (append-only), `market_data_stream_sessions`,
`market_data_stream_status`, `market_data_stream_gaps`; `market_bars.observation_kind`;
`market_data_raw_events.stream_session_id`. Decisions D-029 to D-035.

## Reliability and recovery (Phase 2D)

```text
NYSE calendar x feed hours ──> expected intervals (settled only)
                                   │ minus
coverage evidence ─────────────────┘
  REST_RETRIEVAL: 2B job read every page (failed: up to the last observation received)
  LIVE_STREAM:    stream subscribed, nothing dropped, every frame persisted
                  (proof for bars; for trades it only marks windows to reconcile)
                                   │
                                   v
market_data_gaps (per asset/data type/timeframe; unresolved ones never overlap)
  sources: stream interruptions (2C gaps), completeness scan, operator requests,
           reconciliation (trades the stream lacked), sequence-gap evidence
                                   │
                                   v
recovery worker (one per provider/feed) -> market_data_recovery_jobs
  each attempt = one 2B ingestion job (bars, trades or quotes)
  -> re-evaluate completeness -> COMPLETED / PARTIALLY_COMPLETED / FAILED
  -> gap RECOVERED / PARTIALLY_RECOVERED (+ remainder gap) / UNAVAILABLE / still OPEN
```

* `app.data_engine.calendar`: NYSE holidays, early closes and session labels (D-036).
* `app.data_engine.completeness`: interval arithmetic, expected windows, settling and
  evidence rules (D-037, D-038).
* `app.data_engine.recovery.RecoveryService`: detection, scheduling, execution, evaluation;
  `app.workers.market_data_recovery`: the process (D-039, D-040).
* The stream worker adds per-session `frame_seq` (replays after a lost commit
  acknowledgement are no-ops), live coverage segments, `DATA_NOT_PERSISTED` evidence at
  start-up, database-interruption accounting, and STALE / LATE_ARRIVAL / price-change /
  sequence-gap flags (D-041).
* API: `/market-data/completeness`, `/gaps`, `/recovery-jobs`, `/recovery-requests`
  (operator), `/pipeline-health` (nine dimensions, D-042), `/metrics`.
* Migration `0005`: coverage, gaps, recovery jobs and status tables, `btree_gist` exclusion
  constraint, `frame_seq`, widened quality/status enums.

## Market-data dashboard (Phase 2E)

```text
Alpaca IEX stream ── stream worker (only provider connection) ──> PostgreSQL
                                                                     │
FastAPI ── REST: snapshots, market-status, candles, quotes, trades,  │
           completeness, gaps, recovery/ingestion jobs, health ... <─┤
        └─ SSE /market-data/live <── LiveUpdateHub (1 per API process,│
                                     polls new rows by id, 1 s) <─────┘
                │
Vite proxy (/api, /health, /ready: one origin)
                │
React dashboard ── one shared EventSource per tab (liveUpdates.ts)
```

The dashboard reads only Atlas's API; the browser never contacts a provider and never
receives credentials (status values only). Pages:

| Route | Content | Data |
|---|---|---|
| `/` Overview | NYSE session and next session, pipeline and stream state, summary counts, monitored assets | `market-status`, `stream`, `pipeline-health`, `metrics`, `snapshots` + live trades/quotes |
| `/markets` | Asset table: search, sortable and selectable columns, freshness, quality | `snapshots` + live |
| `/markets/:symbol` | Price chart (1Min–1Day, ranges), quote and last trade, trade tape, coverage, quality events | `candles`, `completeness`, `trades`, `quality-events`, live bars/trades |
| `/data-health` | Nine pipeline stages with last success/update, gaps by status, provider interruptions, quality events, audit trail | `pipeline-health`, `metrics`, `gaps`, `stream/gaps`, `quality-events`, `audit-events` |
| `/ingestion` | Historical retrieval form (backend-validated), job list with filters, recovery jobs | `assets`, `historical-fetch`, `ingestion-jobs`, `recovery-jobs` |
| `/system-health` | API, PostgreSQL, migrations, Redis (Phase 1) | `/health`, `/ready` |
| `/settings` | Effective market-data configuration (read-only), display time zone | `configuration`, `providers` |

Later-phase sections (Strategies, AI Intelligence, Portfolio, Risk Center, Backtesting) are
disabled in the navigation; their URLs show an explicit "not built" page.

Frontend modules (`frontend/src`):

* `types/marketData.ts`: TypeScript mirrors of the backend schemas (decimals stay strings).
* `services/api.ts`, `services/marketData.ts`: typed REST clients and error parsing;
  `services/liveUpdates.ts`: the shared SSE client (reference-counted, reconnects, `resync`).
* `hooks/`: `useApiResource` (loading/success/empty/error, abort + sequence numbers so stale
  responses never win, polling paused in hidden tabs), `useSnapshots`, `useCandles`,
  `useLiveUpdates`, `useNow`, `useSystemStatus`.
* `lib/candles.ts`: chart correctness (ordering, revisions, grid, OHLC, live-bar rules);
  `lib/format.ts`, `lib/time.tsx` (display zone), `lib/snapshots.ts` (live merging).
* `components/`: `ui.tsx` (design-system primitives), `status.tsx` (state badges),
  `market/AssetTable.tsx`, `market/PriceChart.tsx` (Lightweight Charts).
* `layouts/DashboardLayout.tsx` (sidebar, header) and `pages/`.

Refresh strategy: stream status 15 s, market status 60 s, snapshots 15 s plus live merges,
pipeline health 15 s, metrics 30–60 s, job lists 10–15 s; trade tape refetched (25 rows) at
most every 2 s on live trades. Decisions: D-043 to D-049.

## Portfolio and double-entry accounting (Phase 6A)

`app/accounting` (ledger, templates, posting, reconciliation) and `app/portfolio` (accounts,
pricing, valuation checkpoints, figures, read models), migration 0013, read-only routers
`/api/v1/portfolio` and `/api/v1/accounting`, the CLI `python -m app.accounting.cli` and the
dashboard page `/portfolio`. Full conventions: [`accounting.md`](accounting.md); decisions
D-125 to D-137.

```text
CLI (init, post, checkpoint, reconcile)          API (GET only)        Dashboard /portfolio
        |                                              |                        |
        v                                              v                        |
  PostingService --lock account row--> template (pure) -> validate R1/R2 ------+
        |                                  ^                                    |
        |                     LedgerState (lines + quantity ledger)             |
        v                                                                       |
  journal_entries / journal_lines / asset_quantity_ledger (append-only)  <-----+
        |          currency_balances (cache)       fx_rates, instrument_specs
        v
  ValuationService -> VALUATION entry + portfolio_snapshots
  ReconciliationService -> reconciliation_runs / results (HIGH on any discrepancy)
```


## Simulated execution and position management (Phase 7B)

`app/paper_broker/{execution_model,market,fill_model}.py` (versioned assumptions, the
observable stored data, pure fill decisions), `app/execution/{simulation,positions,
protection,monitor,position_queries}.py`, the worker `app.workers.position_monitor`,
migration 0017, GET routes under `/api/v1/paper-positions` and `/api/v1/paper-execution`,
and the Open Positions, Closed Positions and Execution Analytics pages. Working orders are
filled from stored quotes, trades and bars; each fill posts to 6A as SIMULATED_EXECUTION,
consumes its 6C reservation and updates its position in one transaction; the monitor exits
positions on their protective levels. Design: [`execution.md`](execution.md); decisions
D-164 to D-172.

## Paper execution and order management (Phase 7A)

`app/execution` (state machine, validation, service, portfolio contract, reconciliation,
read models, CLI), `app/paper_broker` (the `PaperBroker` interface and the only, simulated,
implementation), `app/events/outbox.py` (transactional outbox), the worker
`app.workers.paper_execution`, migration 0016, GET routes under `/api/v1/paper-orders` and
`/api/v1/paper-broker/status`, and the Paper Orders page. Orders execute approved 6C
reservations; fills post through 6A and consume the reservation in the same transaction.
No brokerage connection exists. Design: [`execution.md`](execution.md); decisions D-156
to D-163.

## Atomic risk reservations (Phase 6C)

`app/risk/reservations` (service, exposure, reconciliation, read models), migration 0015,
GET routes under `/api/v1/risk/reservations`, CLI commands `reserve` and `reservations`, and
the Reservations panel of the Risk Center. Every reservation path locks the portfolio
account row, then the account's reservation state row, then reservation rows; the
assessment is recomputed inside that transaction with active reservations counted against
every limit, and database triggers maintain the projection from append-only events.
Design: [`reservations.md`](reservations.md); decisions D-148 to D-155.

## Deterministic risk engine (Phase 6B)

`app/risk` (configuration, governance, pure sizing and assessment, periods, breakers,
point-in-time data, marked state, service, read models), migration 0014, the router
`/api/v1/risk` (GET, plus the operator-gated kill-switch engage), the CLI
`python -m app.risk.cli` and the dashboard page `/risk`. Conventions:
[`risk.md`](risk.md); decisions D-138 to D-147. The risk engine reads stored rows only and
imports no strategy, ML, LLM or AI code; none of those import it.
