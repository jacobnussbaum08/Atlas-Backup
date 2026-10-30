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


---

## Phase 2B — historical market data

### D-020 Historical prices are RAW (unadjusted) by default

**Decision:** Historical bars are requested with `adjustment=raw`, set by
`HISTORICAL_DATA_ADJUSTMENT=RAW`. Alpaca also offers `split`, `dividend`, `spin-off` and
`all`. The adjustment is stored on every job and every bar, and is part of a bar's identity.

**Why:**
* Raw prices are the prices that actually traded, which is what execution simulation and fill
  realism need.
* Adjusted series are re-computed by the provider after each corporate action. Mixing them
  silently would make history non-reproducible.

**Limitations of raw data:**
* After a split, pre-split prices and volumes are discontinuous: there is a price jump and a
  volume scale change.
* On ex-dividend dates, prices drop by the dividend amount.
* Return or indicator calculations across these dates are wrong unless they are
  corporate-action aware.

**Later:** No corporate-action engine exists yet. An adjusted series can be ingested
side-by-side later, as a different `adjustment`, without touching raw rows.

### D-021 `asof` is not sent

**Decision:** Atlas leaves Alpaca's default `asof`, so symbols are mapped as of today
(renamed tickers include their predecessors' history).

**Atlas's own safeguard:** Atlas still checks every returned bar against its own point-in-time
mapping (`asset_identifiers` validity periods). A bar outside the mapping period is rejected as
`UNEXPECTED_SYMBOL`.

**Caveat:** Alpaca's FAQ says rename mapping becomes available the day *after* a rename.

### D-022 Backfilled data never claims an original availability time

**Decision:**
* `original_available_at` is NULL for every `HISTORICAL_REST` bar. This is enforced by a model
  validator and by a database CHECK.
* `received_at` and `available_at` record when Atlas obtained the bar, which is the retrieval
  time.
* Backtests over backfilled data must use the versioned policy
  `bar-end-plus-settlement-v1` (`data_engine/availability.py`):

  > usable from = interval end + feed delay + 60 s settlement for intraday bars

  For daily bars, the interval end is midnight New York.

**Why:** The 60 s settlement margin exists because Alpaca can update minute bars at the
following half-minute when late trades arrive.

### D-023 Bars are append-only with revisions

**Decision:**
* **Identity.** A bar is identified by
  `(asset, timeframe, provider, feed, adjustment, event_time, revision)`.
* **Repeats.** Re-fetching identical values stores nothing (it is counted as a duplicate).
* **Changes.** A changed observation (a correction, or an incomplete bar becoming complete) is
  inserted as `revision + 1`, with a `CORRECTION` quality event.
* **Immutability.** Triggers make `market_bars`, `market_data_raw_events` and
  `market_data_quality_events` append-only.
* **Concurrency.** Writers of one series are serialized with `pg_advisory_xact_lock`, so
  overlapping jobs cannot race to a revision number.
* **Reads.** Reads take the latest revision first and *then* filter by quality, so a
  quarantined correction hides a bar rather than resurrecting an older revision.
  `known_at` (on `ingested_at`) gives point-in-time views.

### D-024 Retrieval runs per symbol, sequentially, one transaction per page

**Decision:**
* Each symbol is paged independently, so its success, failure and empty result are attributable.
* A page commits atomically: raw response, bars, quality events and counters together. An
  interruption leaves only complete pages, and re-running the range is idempotent.

**How jobs run:**
* With no task system yet (D-006), API-started jobs run in-process via FastAPI background tasks
  (worker thread, own event loop).
* The CLI runs them in the foreground.
* At API startup, jobs left PENDING or RUNNING are marked FAILED ("interrupted").

**Concurrency** is bounded at one request in flight per job, paced to the plan's documented
rate (Basic: 200/min).

### D-025 Exact storage

**Decision:**
* Prices, volume and VWAP are unconstrained `NUMERIC`. A fixed-scale `NUMERIC(p,s)` would
  silently round on insert.
* Responses are parsed with `parse_float=Decimal`. Values that are not exactly representable
  are rejected, never rounded; this includes sub-microsecond timestamps.
* Every response body is stored verbatim (`payload_text`) with a SHA-256 hash.

### D-026 Instrument reference data comes from an operator-maintained file until Phase 3

**Decision:** `config/market_data/reference_assets.toml` defines asset class, primary-listing
MIC, currency, and each provider symbol with its `valid_from` (2016-01-01, the start of Alpaca's
stock history). `register-assets` is idempotent and audit-logged, and it never re-maps a symbol
silently.

**Why:** Alpaca's asset directory is on the trading API, which Atlas does not call (D-019).

### D-027 Operational endpoints need an operator token

**Decision:** `POST /api/v1/market-data/historical-fetch` requires
`X-Atlas-Operator-Token` when `ATLAS_OPERATOR_TOKEN` is set, compared in constant time. Without
the setting, the endpoint is allowed only in `development`/`test` and refused in `paper`. Read
endpoints stay open on the localhost-only API.

**Why:** It is the minimum protection required by spec §24 until dashboard authentication
exists.

### D-028 Polars not introduced yet

**Decision:** Polars is not used in 2B.

**Why:** Ingestion is I/O-bound and row-oriented: validation per bar, insert per page.
[REDACTED_FOR_GITHUB] Polars will arrive with the feature pipeline (Phase 3), where columnar
computation pays off.

## Phase 2C — live streaming

### D-029 The stream contract yields decoded frames (`StreamBatch`)

**Decision:** `MarketDataStream.events()` yields `StreamBatch` objects instead of bare
`MarketDataEvent`s. A batch holds the verbatim frame (`raw_event`) plus everything decoded from
it: `bars`, `quotes`, `trades`, `notices` (trade corrections/cancels, unhandled types) and
`malformed` items. The protocol also exposes `auth_state` and `subscription_state` beside
`state`.

**Why:** Adapters own provider parsing (D-015); the worker and the persistence service stay
provider-independent. Every item lands in exactly one bucket, so nothing is dropped silently.
An open socket, a confirmed authentication and a confirmed subscription are separate facts.

### D-030 One worker process per provider/feed, outside FastAPI

**Decision:** Streaming runs in `app.workers.market_data_stream` (Compose service
`stream-worker`), never in the API process. A session-level PostgreSQL advisory lock
(`market_data_stream:{provider}:{feed}`) guarantees one worker per provider/feed. The worker
writes its state to `market_data_stream_status` (heartbeat every 5 s); the API only reads it.

**Why:** The API stays responsive and opening a page can never open a provider connection.
Alpaca allows one connection per endpoint. No task system is needed for one long-lived process
(D-006 stays open). PostgreSQL rather than Redis holds the status so health survives a Redis
outage and stays queryable with the sessions and gaps (spec §3.2).

### D-031 Live persistence: one transaction per group of frames

**Decision:** Received frames go into a bounded `asyncio.Queue`. A writer drains up to 500
frames (or 200 ms) and persists them in one transaction (raw frames, observations, quality
events). On a database error the same group is retried with bounded backoff; nothing of a
failed attempt is committed. If the queue stays full for 2 s, the frame is discarded, counted,
and recorded as a `DATA_DROPPED` gap and a CRITICAL quality event; health turns `DEGRADED`.

**Why:** Batching keeps writes efficient; the retry keeps temporary database failures from
losing data; the bounded queue keeps memory finite. Blocking the socket indefinitely would make
Alpaca disconnect the client ("slow client", 407) and lose data anyway, so the loss is made
explicit instead.

### D-032 Live bars share `market_bars` and its revision rules

**Decision:** Live bars are stored in `market_bars` with `source = LIVE_STREAM` and a new
`observation_kind`: `LIVE_INITIAL` (Alpaca `b`), `LIVE_UPDATED` (`u`, late trades),
`LIVE_RUNNING` (`d`, the forming daily bar); REST rows are `HISTORICAL` (CHECK constraint ties
kind to source). Same series identity and revision logic as 2B (D-023): an identical
re-observation is a duplicate, a changed one is `revision + 1` with a `CORRECTION` event
(INFO when expected — an update, a running bar, or a previously INCOMPLETE bar; WARNING
otherwise, e.g. live disagreeing with REST). `original_available_at` is the delivery time.
Live bars are RAW (the stream reports trades as they happened).

**Why:** One normalized model for history and live; a correction never rewrites what Atlas had
at an earlier decision time (`known_at` reads). `LIVE_INITIAL` is explicitly not final: Alpaca
may re-emit the bar at the next half-minute.

### D-033 Quotes and trades: exact time, content identity, append-only

**Decision:** New append-only tables `market_quotes` and `market_trades`. Alpaca timestamps
have nanoseconds; `event_time_ns` (BIGINT, ns since epoch) keeps them exactly and `event_time`
holds them truncated to microseconds for indexing. Identity is
`(asset, provider, feed, event_time_ns, content_sha256)`; an identical repeat is a counted
duplicate (`ON CONFLICT DO NOTHING`). Trade corrections and cancels are recorded as
`CORRECTION` / `TRADE_CANCEL` quality events; stored trades are never modified. Quote sizes are
stored in Alpaca's units (documented as round lots). Quote/trade findings are aggregated per
transaction (one event with a count), not one event per tick.

**Rules:** quarantined — negative prices or sizes, a size quoted at price 0, a crossed quote,
a trade price ≤ 0; warnings — missing fields (kept NULL), a locked quote. A side with price 0
and size 0 means an empty book side and is kept as reported.

### D-034 Connection failures and gaps

**Decision:**
* Retryable (bounded exponential backoff, 1 s doubling to `MARKET_DATA_RECONNECT_MAX_DELAY`,
  ±20 % jitter): network errors, unexpected closes, missing pongs, reply timeouts, Alpaca
  401/404/406/407/500.
* Permanent (state `FAILED`, no retry, process stays up reporting it): 402 auth failed,
  405 symbol limit, 409 insufficient subscription, 410 invalid subscribe action, 400 invalid
  syntax, missing credentials or an unentitled feed.
* Every reconnect re-authenticates and re-sends the full desired subscription, and compares
  Alpaca's confirmation with it (`PARTIAL` is reported, never assumed complete).
* Heartbeat: Alpaca documents no application heartbeat, so WebSocket ping/pong is used (ping
  every `MARKET_DATA_HEARTBEAT_TIMEOUT/2`, closed without a pong within the timeout).
* Data may be missing over three kinds of interval, recorded in `market_data_stream_gaps`:
  `CONNECTION_LOST` (from the disconnect until the subscription is confirmed again),
  `WORKER_NOT_RUNNING` (from the previous worker's last activity until the next subscription;
  sessions left open by a crash are closed as interrupted) and `DATA_DROPPED`. Each carries the
  last message before it, the symbols and channels, and `recovery_status = PENDING`. Backfill
  is Deliverable 2D.

### D-035 Health is derived, with session-aware staleness

**Decision:** `GET /api/v1/market-data/stream` derives `health` from the status row, the time
and the US equity session: no heartbeat for 30 s → `NOT_RUNNING`; `HEALTHY` only when an
observation was persisted within `MARKET_DATA_STREAM_STALE_AFTER`; quiet in the regular
session → `STALE`; quiet outside it → `IDLE`; partial subscription, recent drops, failing writes
or received-but-unpersisted data → `DEGRADED`.

**Limitation:** sessions are weekday windows (04:00/09:30/16:00/20:00 New York) without an
exchange holiday calendar (Phase 3). On a holiday a quiet stream can read `STALE`; the response
says so in `calendar`.

## Phase 2D — reliability, recovery and completeness

### D-036 A rules-based NYSE calendar, verified, in the data engine

**Context:** gap detection must tell a missing candle from a weekend, a holiday or an early
close. D-035 deferred holidays to Phase 3; 2D needs them now. Alpaca's calendar endpoint belongs
to its trading API, which Atlas does not integrate (paper only, no broker integration).

**Decision:** `app.data_engine.calendar` generates NYSE holidays and early closes from NYSE's
published rules (observance on weekends, Good Friday from Easter, early closes on the day after
Thanksgiving, Dec 24 and Jul 3 when on Monday-Thursday). Unscheduled closures are listed with
their source (`SPECIAL_CLOSURES`, e.g. 2025-01-09). Unit tests pin every 2025-2027 date to
NYSE's published calendar; outside those years `verified` is False. Stream health (D-035) now
uses it, closing that limitation.

### D-037 Completeness is judged on evidence, not on the presence of each bar

**Context:** providers publish a minute bar only when the feed traded; IEX is sparse. A missing
bar is usually "no activity", sometimes a halt, sometimes lost data. Bar presence cannot tell.

**Decision:** `gaps = expected - coverage evidence`.
* Expected: the feed's publishing hours (`FeedAccess.session_hours`; IEX 08:00-17:00 New York,
  SIP 04:00-20:00) on trading days, restricted to *settled* intervals
  (`MARKET_DATA_RECOVERY_SETTLE_SECONDS` after the interval ends). Windows err wide: a check
  that finds nothing costs one request; a narrow window would hide gaps.
* Evidence (`market_data_coverage`): a 2B retrieval that read every page of a range
  (`REST_RETRIEVAL`, settled part only; a failed retrieval proves only up to the newest
  observation received), or an uninterrupted, fully persisted live stream (`LIVE_STREAM`).
* Inside covered time a minute without a bar had no eligible activity (thin trading, a halt).
  It is not missing and nothing is fabricated for it.

### D-038 A live stream is evidence for bars, not for trades or quotes

[REDACTED_FOR_GITHUB] Atlas was connected, healthy and recorded no loss. Bars are aggregated by
the provider from all trades, so a missed trade frame does not make a minute bar incomplete.

**Decision:** live coverage proves completeness for minute bars only. For trades it marks an
interval for **reconciliation**: once a 30-minute window (`MARKET_DATA_TRADE_RECONCILIATION_
MINUTES`) has settled, the provider's trade history for it is retrieved; trades the stream
already stored are identical (same `event_time_ns` and content hash) and counted as duplicates,
anything else is stored as backfill and reported as a `STREAM_MISSED_DATA` gap. Where trade ids
form a dense sequence (AAPL on IEX), skipped ids also raise `SEQUENCE_GAP` evidence at once.
Quotes have neither ids nor a cheap reconciliation; their gaps come from interruptions only.

### D-039 Recovery reuses 2B ingestion and is finished only by evidence

**Decision:** gaps (`market_data_gaps`) are recovered by `market_data_recovery_jobs`, each
attempt running one ordinary 2B ingestion job (2B now handles trades and quotes too, through the
same paged adapter path). After the attempt the interval's completeness is re-evaluated:
* nothing left -> job `COMPLETED`, gap `RECOVERED`;
* evidence grew but part is left -> `PARTIALLY_COMPLETED`, gap `PARTIALLY_RECOVERED`, the
  remainder becomes a new gap;
* the provider refuses permanently (403, 4xx, unsupported) -> job `FAILED`, gap `UNAVAILABLE`
  plus `UNRECOVERABLE_INTERVAL`;
* transient failure -> retried with backoff (1 min doubling to 1 h); after
  `MARKET_DATA_RECOVERY_MAX_ATTEMPTS` the job is `FAILED` and the gap stays `OPEN` (still
  missing; retried after 6 h);
* records rejected as invalid -> never `COMPLETED` (those minutes have no valid observation).

Recovered observations are `HISTORICAL_REST` with `available_at` = the recovery time, so
point-in-time reads never see them before Atlas had them.

### D-040 Durable, single-worker recovery with database-enforced deduplication

**Decision:** a separate process (`app.workers.market_data_recovery`, Compose service
`recovery-worker`, off by default like streaming) runs cycles every
`MARKET_DATA_RECOVERY_INTERVAL`. One worker per provider/feed (advisory lock); jobs are claimed
`FOR UPDATE SKIP LOCKED` and set `RUNNING` in the claiming transaction; at start-up a worker
returns jobs a dead worker left `RUNNING` to `PENDING`. Overlapping detections are serialized by
a per-series advisory lock and only the uncovered remainder is recorded; PostgreSQL enforces it
(`EXCLUDE USING gist` on unresolved gaps of a series, which needs the `btree_gist` extension)
together with a partial unique index (one active job per gap).

### D-041 No disk spool: bounded queue, exact loss accounting, provider-side recovery

**Context:** Part 5 allows a local spool if justified. [REDACTED_FOR_GITHUB]

**Decision:** no spool. During a database outage frames wait in the bounded queue (writes are
retried); if it overflows, frames are dropped and recorded (`DATA_DROPPED`, 2C). Frames carry a
per-session `frame_seq` (unique per session in the database), which (a) makes a write retried
after a lost commit acknowledgement a no-op, and (b) lets the next worker count exactly how many
frames a dead worker received but never wrote (`DATA_NOT_PERSISTED`). Every such interval
becomes a recovery gap. Bars and trades are recoverable from the provider's history (verified
for IEX on this subscription); quotes are too, but only when `quotes` is listed in
`MARKET_DATA_RECOVERY_DATA_TYPES` (high volume), otherwise their gaps stay reported as missing.
Revisit if a feed without history is added.

### D-042 Pipeline health is per stage; connectivity never implies health

**Decision:** `GET /api/v1/market-data/pipeline-health` reports nine dimensions (provider
connection, authentication, subscription, message reception, processing, validation, database
persistence, historical completeness, backfill activity), each `OK | WARNING | FAILING |
INACTIVE | UNKNOWN`. The pipeline is `HEALTHY` only if every essential (live) stage is OK and
nothing is outstanding; any failing essential stage makes it `FAILING` (e.g. connected and
authenticated while writes fail). Metrics come from the workers' persisted state
(`GET /api/v1/market-data/metrics`); no monitoring platform is added. Recovery events are written
to the append-only audit log with `details.priority`: `HIGH` only for genuine integrity problems
(unrecoverable intervals, failed backfills, missed stream data, outages over 5 minutes in
expected hours, database interruptions over a minute); ordinary quiet markets produce nothing.
