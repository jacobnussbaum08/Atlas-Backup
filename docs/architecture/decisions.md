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

## Deliverable 2E — market-data dashboard

### D-043 Live updates: Server-Sent Events from stored data, one hub per API process

**Context:** the dashboard needs live candles, trades and quotes. The stream worker is the
only provider connection (2C) and writes to PostgreSQL. Options were WebSockets, Server-Sent
Events, or polling; Redis pub/sub could carry notifications.

**Decision:** `GET /api/v1/market-data/live` is an SSE stream. Each API process has one
`LiveUpdateHub` that polls PostgreSQL once a second while at least one client is connected
and fans the rows out to all clients: every new bar row (a new revision is a new row), and per
asset only the newest trade and quote with an arrival count. Updates flow one way, so SSE is
enough (no WebSocket protocol, automatic browser reconnection, works through the Vite proxy).
Polling by primary-key cursor keeps PostgreSQL the source of truth and adds no coupling
between the worker and the API; Redis is not used (it would only shorten latency below the
1 s poll, which a monitoring view does not need). Late-committing transactions can hold an id
below the cursor, so bar ids are re-checked over a trailing window of 2,000 ids. A client
that falls behind receives `resync` and reloads from REST; a client that reconnects does the
same, because missed events are not replayed. At most 50 clients per API process.

### D-044 Snapshot semantics: last price, previous close and freshness are derived, never filled

[REDACTED_FOR_GITHUB] Without a valid daily bar there is no change and the response says why.
Market status (`/market-status`) comes from the 2D calendar; the frontend does no session
arithmetic.

### D-045 Charts show stored bars only; nothing is synthesised

**Decision:** charts load `/candles` (VALID, INCOMPLETE and QUARANTINED, newest revision per
bar) for a bounded range per timeframe (at most 20,000 candles). The browser validates order,
uniqueness, timeframe grid and OHLC before drawing and reports, not repairs, any failure.
Live bars follow Alpaca's semantics: 1-minute bars are appended when published (LIVE_INITIAL)
and replaced by higher revisions (LIVE_UPDATED); the daily bar is replaced while it forms
(LIVE_RUNNING, drawn faded). The provider streams only 1Min and 1Day bars, so 5Min, 15Min and
1Hour charts change only when history is retrieved; they are never aggregated from trades or
1-minute bars in the browser. QUARANTINED bars leave an empty, marked slot. Missing volume is
not drawn as zero. Decision-time snapshots are unaffected: the chart only reads, and the bar
table stays append-only (`known_at` reads remain available).

### D-046 Exchange time (ET) is the default display zone

**Decision:** all data stays UTC. The dashboard shows times in one labelled zone, by default
America/New_York ("ET", the zone of the NYSE calendar and IEX hours), switchable to UTC or the
browser's local zone in Settings (stored per browser). The header also shows local time.

### D-047 Read endpoints answer 503 when the database is unreachable

**Decision:** SQLAlchemy `OperationalError`/`InterfaceError` escaping a request now maps to
`503 dependency_unavailable` ("The database is unavailable.") instead of a generic 500, so
the dashboard can tell an outage from a bug. Internal details stay in the log.

### D-048 Pipeline health: recorded auth/subscription states are history when disconnected

**Context:** with the stream worker stopped, `/pipeline-health` still reported
authentication and subscription `OK` from the last recorded states (2D), while `/stream`
already treated the subscription as inactive. The dashboard made the inconsistency visible.

**Decision:** while the stream is not connected, authentication is `UNKNOWN` ("not connected
(last known auth state …)") and the subscription `FAILING`; a rejected authentication stays
`FAILING`. Connected behaviour is unchanged. Dimensions now also carry `last_success_at` and
`updated_at`.

### D-049 Charting library: TradingView Lightweight Charts 5

**Decision:** `lightweight-charts` ^5.2.1 (Apache-2.0, named in spec §6). The TradingView
attribution logo stays enabled, which satisfies the licence's attribution requirement. The
asset page (and with it the library) is loaded on demand, so the initial bundle is 367 kB
(110 kB gzipped) instead of 561 kB.

## Phase 3A — dynamic asset discovery and universe management

### D-050 Read-only asset directory on Alpaca's paper Trading API (partly supersedes D-019, D-026)

**Context:** Phase 3A requires instrument discovery through the existing Alpaca adapter.
Alpaca publishes its asset directory only on the Trading API (`GET /v2/assets`); the
market-data hosts have none. D-019 kept the adapter on market-data hosts only, and D-026
kept instrument identity in an operator file for that reason. The user approved this
exception on 2026-11-01.

**Decision:** `providers/alpaca/reference.py` reads `GET /v2/assets` and
`GET /v2/assets/{id}` from `https://paper-api.alpaca.markets`, and nothing else:
* the host is a constant (the live Trading API host appears nowhere in the code; a unit test
  scans the code base for it) and there is no setting that could change it;
* `check_reference_request` refuses every method but GET and every path but the two asset
  paths, both before the request and again in an httpx request hook, so a future code change
  cannot route an order, account or position request through this client;
* the client has its own request pacer (the Trading API's 200/min budget);
* the opt-in live test records every HTTP request and asserts the fence held.

**Unchanged:** market data still comes only from `data.alpaca.markets` and the stream host.
Atlas has no order, account or position code. The keys must be paper-account keys (the live
host is never contacted).

### D-051 Instrument identity: provider IDs, and symbol uniqueness among active assets only

[REDACTED_FOR_GITHUB] The Phase 1 constraint `UNIQUE (symbol, asset_class, exchange_code)` on
`assets` cannot represent that.

**Decision:**
* `provider_assets` maps a provider's permanent instrument ID (Alpaca's asset UUID) one-to-one
  to an Atlas asset; the link never changes. A renamed ticker stays the same asset; a reused
  ticker is a different asset.
* The `assets` uniqueness now applies to `ACTIVE` rows only (partial unique index
  `uq_assets_active_listing`). Downgrading 0006 fails if two assets share a symbol.
* On first sight, an instrument active under a symbol an operator already registered (D-026)
  is **adopted** (same asset, the operator mapping kept), never duplicated.
* `assets.exchange_code` now references the new `exchanges` table (MIC). Alpaca's exchange
  codes are mapped through `provider_exchange_codes`; `TXSE` has no verified MIC and stays
  unmapped (the asset keeps the provider code; qualification rejects the venue).

### D-052 Versioned reference data and observed lifecycle events

**Decision:** every distinct provider record is stored verbatim (`raw_json`, canonical key
order, exact numbers) and normalized, as a new row of the append-only
`asset_reference_versions`. Identical re-observations only refresh `last_seen_at`. Changes
become append-only `asset_lifecycle_events`: deactivation, delisting (Alpaca's `_DELISTED`
symbol suffix: an observed, undocumented convention, recorded as such), reactivation, symbol,
exchange and name changes, tradability changes, other metadata changes, and absence from or
reappearance in a complete listing. Alpaca reports no effective dates, so `effective_at` is
Atlas's observation time (`effective_at_basis = OBSERVED`). Absence from a listing is never
interpreted as a delisting: the status changes only when the provider reports it.

The `assets` row is the current state only (materialised from the newest version); history
lives in the versions and events.

### D-053 Mapping start dates say what they are based on

**Decision:** `asset_identifiers.valid_from_basis`:
* `OPERATOR_REFERENCE`: the reference file (existing rows);
* `PROVIDER_HISTORY_ASSUMED`: instruments active in the *initial* catalog whose symbol no
  other known instrument uses start at `ASSET_DISCOVERY_ASSUMED_HISTORY_START` (2016-01-01,
  as D-026), because Alpaca resolves history to today's instrument (D-021);
* `OBSERVED`: everything else (new listings, reactivations, symbol changes, shared symbols)
  starts when Atlas saw it.

Inactive instruments found in the initial catalog get no mapping (their validity period is
unknown). The 2B check that rejects bars outside a mapping's period (D-021) is unchanged, so
history cannot be attributed to an `OBSERVED` mapping before its start; history preparation
skips such instruments.

### D-054 Universe membership as an event chain, reconstructable at any time

**Decision:** `universe_membership_events` is append-only: ADDED/REMOVED per asset and layer
(discovery pool, qualified universe, active watchlist), with `effective_at`, `recorded_at`,
reason code and text, and the evaluation or score behind it. Each event names its predecessor;
a unique constraint on `(asset, layer, previous_event_id)` (NULLs not distinct) makes two
concurrent transitions from one state impossible. `members_at(T, known_at)` returns the newest
event effective at or before T *and recorded by* `known_at` (default T): today's membership
never leaks into the past, and a delisted asset is still a member of every period it belonged
to. Back-dating before the previous event is refused. Historical qualification runs
(`--as-of`) never change membership.

### D-055 Feed-specific, versioned qualification rule sets; missing data is not failure

**Decision:** rule sets (`qualification_rule_sets`) and scoring versions (`scoring_versions`)
are immutable; a change is a new version, written to the audit log (also when created through
`POST /api/v1/discovery/config/*`, operator token required). A rule set names the feed its
volumes are calibrated for (`us-equity-iex`, `us-equity-sip`) and is refused for another feed:
IEX volume is one venue's share of trading, so consolidated thresholds would be wrong.
Mandatory rules decide qualification; a mandatory rule without input is `UNAVAILABLE`
(outcome `INSUFFICIENT_DATA`), never `FAIL`. Spread, quote quality and a volatility cap are
optional and apply only where their data exists. The defaults are starting points, not
validated thresholds (see the Phase 3A report).

### D-056 Polars for qualification measurements (supersedes D-028 for this module)

**Decision:** daily-bar measurements are computed in Polars over one frame per evaluation
(assets x expected sessions). Expected sessions come from the venue calendar (closures never
count as missing), and each is classified first (traded, zero volume, missing volume,
quarantined, no reported activity, provider gap, missing), so that zero volume, missing volume
and missing data are never confused. Floats are used inside Polars; persisted values are
`NUMERIC` rounded to stated places, with window, as-of time, source range and
`feature_version` (`daily-liquidity-v1`). Only bars ingested and available by `as_of` are read.
Polars is pinned to the 1.x line (`>=1.40,<2`; 1.44.2 locked).

### D-057 Watchlist ranking is explained analytical priority, never a signal

**Decision:** factor scores are cross-sectional percentiles, or a band score for volatility so
that higher volatility is not "better"; momentum and relative strength use magnitudes, with
direction ignored. Missing factors are excluded and the weights renormalized; below 60%
computable weight an asset is unranked. Every score row stores its factors, version and an
explanation ending in a fixed disclaimer. Selection: top N, plus current members within a
retention buffer. Nothing consumes it as a trade input.

### D-058 Durable jobs without a task system (D-006 still open)

**Decision:** `discovery_jobs` is the queue: one PENDING/RUNNING job per kind (partial unique
index; repeated requests return the active job), claims with `FOR UPDATE SKIP LOCKED`, leases
renewed by heartbeat, expired leases returned to PENDING (or FAILED after
`ASSET_DISCOVERY_JOB_MAX_ATTEMPTS`), transient failures retried with backoff, permanent ones
(credentials, capability) failed at once. API requests run in a background thread (as in 2B);
the optional `discovery-worker` schedules and runs jobs. Directory chunks are one transaction
each under a provider-wide advisory lock; first sightings are inserted in batches (one INSERT
per table per chunk, with a per-record fallback if a constraint trips); a qualification job's
measurements and evaluations commit in one transaction.

[REDACTED_FOR_GITHUB]

### D-059 Exchange-aware sessions built on the 2D calendar (extends D-036)

**Decision:** `app.asset_discovery.sessions` gives each venue (`exchanges.calendar_code`) its
own calendar object with its time zone and hours. The five supported US venues currently use
NYSE's published holiday and early-close rules (verified 2025-2027) and say so explicitly; a
diverging venue gets its own entry. Extended hours are the generic 04:00-20:00 ET window;
halts are not known (no halt feed) and overnight sessions are not modelled. OTC and unmapped
venues have no calendar and are unsupported for qualification.

### D-060 Instrument type is UNCLASSIFIED unless a source says otherwise

**Decision:** Alpaca's `us_equity` class covers stocks, ETFs, preferreds, warrants, units and
rights alike. Discovered instruments are `asset_class = EQUITY`, `instrument_type =
UNCLASSIFIED`, `classification_source = NONE`; Atlas never infers the type from names or
ticker suffixes. Assets the operator file lists as ETF are `ETF` (migration 0006). The default
rule sets accept UNCLASSIFIED; the price and liquidity rules exclude most warrants and rights,
which is a side effect, not a classification.

### D-061 History for qualification is prepared in bounded batches

**Decision:** a qualification job retrieves daily bars (through the unchanged 2B path) for at
most `ASSET_DISCOVERY_HISTORY_PREP_MAX_ASSETS` (default 200) instruments that pass the metadata
rules but lack history: the benchmark first, then instruments with listed options, then a
stable hash of the asset ID (not alphabetical, which would bias the early universe). The rest
stay `INSUFFICIENT_DATA` until later runs. The ~13,000 eligible instruments therefore need many
runs (or a larger bound) before the qualified universe reflects the whole pool.

### D-062 Observed: IEX daily bars with VWAP 0 are quarantined by 2B validation

[REDACTED_FOR_GITHUB] Such instruments fail the invalid-observation and coverage rules.

**Decision:** unchanged. The validator is not weakened for qualification; the effect is
reported in the Phase 3A report.

### D-063 Qualification measures split-adjusted daily bars (audit P1-1; amends D-020 for 3A)

**Context:** D-020 keeps stored history RAW and warns that return calculations across
corporate actions are wrong. Phase 3A nevertheless computed volatility, ATR, 20-session
momentum and relative strength from RAW daily bars: a 4:1 split read as a -75% move, which
inflated volatility, could disqualify the asset and pushed it up the momentum ranking.

**Decision:** qualification reads and retrieves the daily-bar series of
`ASSET_DISCOVERY_PRICE_ADJUSTMENT` (default `SPLIT`). The RAW default of
`HISTORICAL_DATA_ADJUSTMENT` is unchanged for every other path; `HistoricalFetchRequest`
gained an optional `adjustment`, and the adjusted bars are a separate series (D-020).

* `SPLIT`, not `ALL`: it removes the large discontinuities while prices stay close to as
  traded. Dividend adjustment rewrites every earlier price after each dividend and would
  churn revisions for little gain over a 20-60 session window.
* An adjusted series is only consistent within one retrieval (the provider re-adjusts after
  each split). History preparation always re-retrieves the whole window in one request, and
  reads take the newest revision known at `as_of`, so a window never mixes adjustments.
* The metric version names its input: `daily-liquidity-v2-split` (v1 was RAW).

**Limitations:** spin-offs and special dividends still appear as discontinuities. Existing
RAW history is not used by qualification; the first runs after this change re-prepare
history (bounded by D-061), so assets without split-adjusted history are
`INSUFFICIENT_DATA` until prepared.

### D-064 Ingestion jobs carry a heartbeat; only stale jobs are reclaimed (audit P1-2)

**Context:** the API (at startup) and the recovery worker marked *every* PENDING/RUNNING
ingestion job FAILED as "interrupted". The recovery and discovery workers run ingestion jobs
too, so restarting one process failed another process's live job, whose final status then
overwrote the failure.

**Decision:** a job's `heartbeat_at` is set at creation and renewed every 30 s while it runs
(and per page). Only jobs whose heartbeat is older than 5 minutes are reclaimed, by the API at
startup and then every 60 s, and by the recovery worker at startup. A running job whose row is
no longer RUNNING stops itself, and a finished run never overwrites a reclaimed status. No
migration: `heartbeat_at` already existed.

### D-065 Discovery work runs off the event loop; a lost lease stops the job (audit P1-3)

**Context:** directory application, qualification, scoring and membership updates were
blocking calls on the event loop that also renews the job lease, so a long step starved the
heartbeat; a lost lease was only logged, and the job ran on beside the worker that reclaimed it.

**Decision:** those steps run in a worker thread (`asyncio.to_thread`). When a renewal finds the
job owned by someone else, the runner cancels the job and records nothing. Every write
transaction of qualification, scoring and membership first checks, under the job's row lock,
that this runner still holds the lease, so a thread that finishes after losing it commits
nothing.

### D-066 Phase 3B analytics in plain Python; no NumPy dependency yet

**Context:** the Phase 3B prompt suggests Polars and NumPy "where beneficial / appropriate".
NumPy is not a project dependency, and `uv` (needed to update `uv.lock`, which the Docker
build installs with `--frozen`) is not available in the development shell used for 3B.

**Decision:** indicators, classifiers and correlations are pure Python over float lists
(`app.feature_engine.indicators`, `app.market_intelligence.*`). Every per-instrument window
is at most a few hundred values and the correlation matrix at most 40 x 40 (power
iteration for its top eigenvalue), so vectorization would not change results or run time
materially, while plain loops keep each definition readable and checkable against its
reference. Polars stays the tool for the 3A cross-sectional measurements. NumPy can replace
these loops later (with a lock update) without changing any stored definition.

### D-067 Market intelligence defaults are interpretable starting points

**Decision:** every window, weight and cut point lives in the versioned
`intelligence_configs` table (default `market-intelligence-default` v1); each result names
the configuration and algorithm version it used. Initial values and their rationale:

* Daily windows: 20/50-session averages (~1 month / 1 quarter), ADX and ATR 14 (Wilder's
  defaults), Bollinger 20/2 (Bollinger's defaults), 20-session realized volatility ranked
  against 120 sessions of its own history, 5-session change look-back. Weekly: 10/30-week
  averages, 13-week momentum and volatility, 26-week volatility history.
* Trend: ADX logistic midpoint 20 (Wilder's commonly cited 20-25 trending threshold),
  direction components weighted 0.20/0.20/0.20/0.15/0.25, confidence floor 0.40.
* Volatility: own-history percentile cut points 0.25 / 0.75 / 0.95 (quartiles, and a
  1-in-20 extreme), soft boundaries (width 0.03); range expansion at ATR(5)/ATR(14) >= 1.25.
* Stability: 10 % drawdown = full drawdown stress; average correlation 0.3-0.7 mapped to
  0-1; participation below 50 % above the slow average; STABLE below 0.35, STRESSED above
  0.60 stress score.
* Correlation: 60 daily returns compared with the preceding 60, at least 45 shared
  observations per pair, 95 % Fisher-z intervals, at most 40 instruments.

None of these values were fitted to returns or validated against outcomes. They were chosen
so that each output can be explained from its inputs; changing them is a new version.

### D-068 Knowledge basis: RECORDED versus RECONSTRUCTED

**Context:** look-ahead prevention requires "only observations available at the decision
time". Atlas began storing data recently, and REST backfills carry `available_at` = their
retrieval time, so a strictly point-in-time history of past periods would be empty.

**Decision:** every result states its basis.

* RECORDED (live runs): bars with `ingested_at` and `available_at` <= the decision time,
  sessions completed (close + settle) by then, the newest revision known then, VALID only.
  This is what a strategy could have used at that moment.
* RECONSTRUCTED (reconstruction runs): bars retrieved later whose session had completed and
  settled by the decision time. They may include corrections published afterwards, and are
  always labelled; the API's "current view" never uses them.
* In both, universe membership is what Atlas had recorded by the decision time, so periods
  before Phase 3A have no universe (no participation): never today's universe applied to
  the past (survivorship bias).

Features are scale-free (ratios, percentages) so the provider's re-adjustment of
split-adjusted history after a split does not change a recomputed value. Results are
append-only; recomputing a decision time adds rows, and readers take the newest available
by their query time.

### D-069 Classification probabilities are evidence weights, not forecasts

**Decision:** trend, volatility and stability outputs are distributions over labels
(including UNCERTAIN) from transparent formulas (logistic transforms of configured
evidence). They are normalized evidence shares, not calibrated probabilities; they predict
nothing and authorize nothing. Missing inputs move mass to UNCERTAIN; insufficient data
gives UNCERTAIN with probability 1; a label below the confidence floor is reported as
UNCERTAIN. Calibration against outcomes belongs to later validation work (spec 19-20).

### D-070 Participation is universe participation, not market breadth

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

### D-071 Market intelligence runs: one at a time, heartbeat, ownership checks

**Decision:** `intelligence_runs` allows one RUNNING run per provider and feed (partial
unique index). A run renews `heartbeat_at` every 30 s; a RUNNING run with a heartbeat older
than 5 minutes is failed as interrupted when the next run is created. Each decision time is
written in one transaction that first checks, under a row lock, that the run is still
RUNNING, so a run reclaimed meanwhile commits nothing (as D-064/D-065). Runs start from the
CLI or the operator-gated API (in a background thread of the API process, D-006 still
open); there is no scheduled worker yet.

---

## Phase 3C — news and event intelligence

### D-072 News is a separate subsystem with its own provider contract

**Decision:** `app/news/` holds a provider-independent engine: `providers/base.NewsProvider`
(historical retrieval with pagination, a live `NewsStream`, authentication check,
capabilities) and `economic_calendar.EconomicCalendarProvider`. `providers/registry.py` is
the only place mapping a provider ID to an adapter (`NEWS_PROVIDER`, default `alpaca`).
Adapters return sanitized `ProviderNewsArticle` observations; storage, mapping,
classification and relevance never see provider payloads.

**Alpaca adapter** (`news/providers/alpaca/`), verified against the documentation and real
responses on 2026-11-02: `GET https://data.alpaca.markets/v1beta1/news` (`start`/`end`
inclusive, `symbols`, `limit` <= 50, `sort`, `include_content`, `page_token` ->
`next_page_token`) and `wss://stream.data.alpaca.markets/v1beta1/news` (same protocol as
the stock stream; `{"action":"subscribe","news":["*"]}`). It reuses the market-data
credentials, HTTP transport (pacing, retries, redaction) and, after a refactor that moved
connect/auth/control handling into `AlpacaStreamConnection`, the WebSocket protocol code;
the stock stream's behaviour is unchanged (its tests pass unmodified).

### D-073 Article identity, revisions and deduplication

**Decision:** `news_articles` is identity plus current state (as `assets`, D-052);
`news_article_revisions` is append-only. An observation is a duplicate when a stored
revision has the same stored fields (`content_sha256`: headline, summary, author, source,
URL, sorted symbols), the same provider `updated_at`, and the same body hash where both
carry a body. Otherwise it is a new revision: `PROVIDER_UPDATE`, `CONTENT_CHANGED` or
`LATE_EARLIER_VERSION` (stored but never made current). Writers take a per-article advisory
transaction lock in sorted order, so the stream worker and a backfill job never race.

**Why the body is compared only when both sides have one:** REST retrievals without content
must not contradict a stream observation that carried a body.

### D-074 Article bodies are not retained by default

**Context:** Alpaca relays partner content (observed sources: `benzinga`, `fiscal.ai`). Its
documentation states no retention or redistribution terms for article bodies, and the
Phase 3C prompt forbids retaining full content unless permitted.

**Decision:** `NEWS_STORE_CONTENT=false` (default): REST requests send
`include_content=false`; stream bodies are dropped after hashing (`body_sha256`, so a body
correction is still detected); `provider_payload` never contains the body. Headline,
summary, URL, author, symbols and images (URLs only) are kept. Atlas never fetches linked
pages. An operator who confirms the licence terms may enable retention; stored bodies are
plain text (HTML removed).

### D-075 Publication, receipt and availability are different timestamps

**Decision:** per revision: `published_at` and `provider_updated_at` (provider's),
`received_at` (Atlas obtained the observation), `processed_at` = `available_at` (stored),
and `original_available_at` = receipt time for stream observations only. A CHECK enforces
`original_available_at IS NULL` for every `HISTORICAL_REST` revision (as D-022 for bars):
a backfill never claims Atlas knew the article at its publication time. Articles record
`first_received_at`, `first_available_at`, `first_origin` and `first_live_received_at`.

Reads offer a point-in-time view (`known_at`): the newest revision and newest event stored by
then. An article published at T but retrieved at T+3 days does not exist for a decision at
T+1 day. Strategies and backtests must use this view.

### D-076 Deterministic, versioned event taxonomy; labels are unverified estimates

**Decision:** taxonomy `news-taxonomy-v1` (17 categories, including `OTHER`) and keyword
rules in `news/classification.py`: named regular expressions per category, headline match
2 points, summary match 1, assigned at >= 1; several categories per article; primary =
provider label where known (Alpaca supplies none), else highest score, ties by taxonomy
order. Crypto pairs among the symbols add `CRYPTOCURRENCY` by symbol rule. Confidence is
evidence strength (`MEDIUM`/`LOW`/`NONE`), never a probability. Atlas labels are always
`UNVERIFIED` (database CHECK); provider labels are `PROVIDER_ASSERTED`. Each event stores
taxonomy, classifier version and the rules' SHA-256; reclassification under a new version
adds events available from the time they were computed (`python -m app.news.cli
reclassify`), so earlier decisions keep seeing the labels that existed then.

**Version history:** `keyword-rules-v1` was run on 3 days of real articles on 2026-11-02;
reviewing samples showed misses ("Reports Fiscal Year ... Financial Results", "Executive
Departure at ...", trading halts, "Agrees To Sell Its ... Division", staff cuts, share
issuance). `keyword-rules-v2` adds those rules; the stored v1 events remain.

Classification says what an article is about. It does not rate importance, surprise,
sentiment or impact.

### D-077 No economic calendar is claimed

**Decision:** schema `economic_events` / `economic_event_revisions` (name, country, currency,
scheduled time, previous/forecast/actual as number + unit + verbatim text, the source's own
importance label, revisions with availability) and the `EconomicCalendarProvider` interface
exist; the registry is empty and `GET /api/v1/news/economic-calendar` reports
`NOT_CONFIGURED`. Alpaca's news feed is not a calendar and is never presented as one.
Choosing a calendar provider (and any subscription) is a separate decision.

### D-078 Relevance is an explained ordering heuristic

**Decision:** `relevance-v1`: static relevance per article-asset link = 0.40 (direct provider
tag) + 0.30 x 1/(tagged symbols) + 0.30 x (headline names the symbol or issuer name), stored
with its factors; recency = 0.5^(age / `NEWS_RELEVANCE_HALF_LIFE_HOURS`) computed at query
time (never stored). Source reliability is recorded as `NOT_ASSESSED` with weight 0. The
weights are documented starting points, not fitted; relevance is not sentiment or impact.

### D-079 Symbol mapping is effective-dated at publication and never forced

**Decision:** method `effective-dated-symbol-v1` (`news/mapping.py`) resolves each article
symbol in its provider's symbol namespace (`alpaca`) with `asset_identifiers` valid at the
article's publication time: `MAPPED` (also for now-inactive assets, status recorded),
`AMBIGUOUS` (the symbol changed hands within `NEWS_MAPPING_AMBIGUITY_WINDOW_HOURS`),
`NOT_EFFECTIVE` (mappings exist but none covered publication, e.g. only observed later,
D-053), `UNKNOWN_SYMBOL`, `INVALID_SYMBOL` (not a symbol form of the namespace; observed:
`TSX:CPX`, `PCG$A`). Each outcome is stored with reason and candidates; unmapped articles
are kept with scope `MARKET` (macro categories) or `UNLINKED`.

### D-080 One news stream worker; recovery by overlapping REST retrieval

**Decision:** `python -m app.workers.news_stream` (compose `news-worker`, off unless
`NEWS_STREAM_ENABLED=true`) holds the only news connection (advisory lock per provider),
separate from the stock stream worker. It persists each frame inline and idempotently;
while PostgreSQL is down it stops reading (the provider eventually drops the connection).
After every (re)subscription it runs a `STREAM_RECOVERY` job over
`[interruption start - NEWS_STREAM_RECOVERY_OVERLAP_SECONDS, now]` (first start: the
previous session's end; capped at `NEWS_STREAM_RECOVERY_MAX_HOURS`, with an error recorded
when capped). Duplicates from the overlap are recognised (D-073). Permanent failures
(rejected credentials, entitlement) report `FAILED` and are not retried.

**Observed (2026-11-02):** Alpaca accepts one news stream connection per account; a second is
refused with `406 connection limit exceeded` during authentication. The worker treats 406
as recoverable (backoff, as the stock stream) and the advisory lock keeps Atlas itself to
one connection; the news and stock streams are separate endpoints and coexist.

**Limitation:** Alpaca does not document whether `start`/`end` filter on creation or update
time; an article updated during an outage but created before the window may be missed until
it is seen again.

### D-081 Untrusted text is sanitized on ingestion and rendered only as text

**Decision:** adapters strip HTML with a parser (script/style bodies dropped), decode
entities once, remove control and bidirectional-format characters, cap lengths, and keep
URLs only when absolute http(s) without credentials (`javascript:`/`data:` rejected). The
dashboard renders every field as React text and links out only to http(s) URLs with
`rel="noopener noreferrer nofollow"`. Missing fields stay `null`.

## Phase 4A — strategy laboratory foundation

### D-082 Strategies are self-describing modules; Phase 4A families have no logic

**Decision:** a strategy is a `Strategy` subclass (`app/strategies/base.py`) with declarative
metadata (`strategy_id`, family, name, description, `logic_version`, supported asset classes
and timeframes, evaluation schedule, a frozen pydantic `Parameters` class) and two methods:
`required_features(params, timeframe)` and `evaluate(ctx, params) -> StrategyOutcome`. Each
family is one module in `app/strategies/families/` exposing `STRATEGY`; the registry
discovers them (`pkgutil`), so adding a strategy touches no other strategy module. A strategy
is validated when registered: id format, metadata, default parameters, data-backed
timeframes, and every required feature resolving in the feature registry.

The six families (momentum, trend following, mean reversion, breakout, volatility, event
driven) ship as `InterfaceOnlyStrategy` (`implemented = False`): parameter schemas and
feature dependencies, no trading logic (Phase 4B). The engine never plans them and
enabling them is refused (409). The engine is exercised by TEST FIXTURE strategies only.

**Why:** the prompt reserves strategy logic for Phase 4B; an "enabled" strategy that cannot
evaluate would be a misleading state.

### D-083 Versions are immutable (logic version + configuration); status is an event log

**Decision:** `strategy_configurations` are content-addressed parameter sets (every field
explicit, decimals as strings, sha256 per strategy). `strategy_versions` number the pairs
(logic version, configuration) together with the metadata the code declared then
(description, timeframes, schedule, resolved feature keys, registry version); the newest is
current. A new version is created when an operator posts new parameters (partial updates
merge onto the current version's values; identical parameters return the current version) or
when a catalog sync finds the code's logic version or metadata changed (same configuration).
If the stored configuration no longer validates against the code's schema, sync reports a
problem and creates nothing: parameters are never changed silently. Enable/disable is
`strategy_status_events` (append-only, newest wins, no event = disabled). All of these tables
are append-only (migration 0009 triggers); changes are audited (`audit_logs`).

### D-084 One feature registry over the existing indicators; fixed windows

**Decision:** `app/feature_engine/registry.py` describes every feature (name, version, source,
method, typed parameters with bounds, timeframes, required data, availability rule, value
kind, window rule) and gives each resolved feature a canonical key, e.g.
`ema.v1(length=20)@1Day`. Bar features call `app/feature_engine/indicators.py`: the Phase 3B
functions (SMA, EMA, Wilder ATR/DMI/ADX, ROC, realized volatility, Bollinger width,
percentile rank) plus new RSI (Wilder), MACD (Appel), Bollinger bands, volume ratio and prior
extremes. Phase 3B's private volume-ratio helper now delegates to `indicators.volume_ratio`
(identical formula; `features-v1` values unchanged, tested). Each value uses exactly the
newest `window(params)` completed periods (EMA/Wilder seeded inside a window that is a
multiple of the period), so more stored history cannot change it. Definitions are registered
in `feature_definitions` with a sha256; a changed definition under an existing version is
refused at sync.

### D-085 Strategy inputs are point in time; completed periods only

**Decision:** an evaluation has a data cutoff and a knowledge time. Periods are those whose
regular close plus `STRATEGY_SETTLE_SECONDS` passed by the cutoff (`completed_sessions`; weekly
periods are complete ISO weeks with every session's bar); bars are the newest revision stored
by the knowledge time (`reads.daily_bars`, as Phase 3B), VALID only. `RECORDED` (live and
strict historical) uses `known_at = cutoff`; `RECONSTRUCTED` re-evaluates a past cutoff with
data retrieved later and is labelled. A missing period inside a window is `MISSING_INPUTS`,
too little history `INSUFFICIENT_DATA`, a zero denominator `UNDEFINED`; nothing is filled. The
values, statuses, reasons and availability times are stored in a content-addressed
`strategy_feature_snapshots` row (CHECKs: `RECORDED` inputs available by the cutoff, no period
after it). The engine calls a strategy only when every required feature is VALID; otherwise
the evaluation is `SKIPPED` with the reasons.

### D-086 News and regime features never invent information

**Decision:** news features (`news_event_count`, `news_hours_since_latest`) count articles
directly related to the asset (Phase 3C relationships, newest event per article known at the
knowledge time, optional category and minimum relevance) only when completed retrieval jobs
or subscribed stream sessions covered the whole look-back window as known then; otherwise
they are `UNAVAILABLE` ("absence of articles is unknown"), never zero. Regime features read
the newest stored Phase 3B classification with `as_of <= cutoff` (RECORDED evaluations use
only RECORDED classifications) and are `UNAVAILABLE` beyond `max_age_hours`. The event-driven
family requires its news counts (not optional), so missing coverage blocks it.

**Limitation:** coverage is inferred from job ranges and stream sessions; a stream session
still running counts up to the knowledge time only while its status heartbeat is fresh.

### D-087 The standardized proposal is a hypothesis

**Decision:** `strategy_proposals` holds the spec 11.7 / prompt fields: id, strategy and
version, asset, market (asset class), direction (LONG/SHORT), timeframe, signal time, data
cutoff, feature snapshot ids, reference entry, optional invalidation and target (CHECKed to
the correct side), holding horizon in periods, signal strength in [0, 1] with
`strength_kind = UNCALIBRATED_SCORE`, evidence, reasoning, expiry. No quantity, order type or
approval state exists. Every evidence item must name a VALID feature of the evaluation's
snapshot (the engine copies its value and availability). Expiry is the close of the
`signal_ttl_periods`-th period ending after the signal on the exchange calendar. Status is
derived at read time (ACTIVE, EXPIRED, SUPERSEDED); `strategy_proposal_events` records expiry
(when Atlas noticed) and supersession by a newer LIVE proposal of the same strategy, asset,
timeframe and direction. Expected transaction costs (spec 11.7) are left to the risk and
execution phases, which own cost models.

### D-088 Evaluation engine: idempotent claims, leases, outcome written once

**Decision:** an evaluation's idempotency key hashes (version, asset, timeframe, mode, basis,
trigger, trigger reference: the completed period or news event). A claim runs under an
advisory lock on the key; a partial unique index allows one non-failed evaluation per key.
Duplicates and in-progress claims return without work; a RUNNING evaluation whose lease
expired is failed as `INTERRUPTED` and retried; after `STRATEGY_MAX_ATTEMPTS` failures the
update is given up. The outcome is written in one transaction only if the evaluation is still
RUNNING and owned by this worker; a trigger (`atlas_strategy_evaluation_guard`) allows the
single RUNNING -> outcome transition and nothing else. Failures are recorded by stage
(INPUTS, STRATEGY, VALIDATION, TIMEOUT, PERSISTENCE, INTERRUPTED; messages redacted).
`evaluate_many` bounds concurrency (`STRATEGY_MAX_CONCURRENCY`) and time
(`STRATEGY_EVALUATION_TIMEOUT_SECONDS`); a timed-out strategy thread cannot be killed, so its
late result is discarded by the ownership check.

Updates are detected by polling (`app/strategies/triggers.py`): the newest completed session
with a VALID stored bar per qualified-universe member (and the week when it ends one), and
news events since the previous check. Re-detection is harmless because of the keys, so no
cursor needs to be durable.

**Why polling:** PostgreSQL is the source of truth; LISTEN/NOTIFY or Redis streams would add
a second delivery path for little gain at daily granularity.

### D-089 The Strategy Laboratory page is read-only and shows no performance

**Decision:** the dashboard shows the catalog, versions, parameters, required features,
status history, proposals and evaluation history, and the feature registry. It has no
enable, parameter or execution controls (those are operator API calls) and shows no
performance statistics: none exist until strategies trade or are backtested (Phases 7-8).

## Phase 4B — six trading strategies

### D-090 Strategies compute only from registry features and record every condition

**Decision:** the six strategies read features through `EvaluationContext` and nothing else;
every indicator comes from the feature registry (Phase 3B `indicators` plus additions). What
strategies share (`app/strategies/signals.py`) is bookkeeping: an `Assessment` per direction
evaluates *every* condition (no short-circuit) and records it as a check (passed, failed or
informational); passed checks become the proposal's evidence; `make_levels` rejects invalid
price relationships (non-positive, wrong side, collapsed by rounding); `strength` is the mean
of components clamped to [0, 1]. A strategy that finds inconsistent but VALID inputs (e.g. a
non-positive ATR) returns `invalid_data` instead of "no signal".

**Why:** an evaluation without a proposal must still say exactly why, and the same
indicator must never exist twice with diverging maths.

### D-091 New features are new names, not new versions of existing ones

**Decision:** Phase 4B added `close_location`, `true_range_ratio`, `log_slope`,
`log_slope_tstat`, `realized_volatility_percentile`, `prior_bollinger_width_percentile`,
`failed_breakout_count`, `relative_return` and `period_news_count`, all at v1. No existing
definition changed. `relative_return` introduces the `BENCHMARK` feature source: the
benchmark's bars are loaded for exactly the same completed periods and knowledge time; a
missing period in either series is `MISSING_INPUTS`, an unknown or ambiguous benchmark
`UNAVAILABLE` (migration 0010 widens the `feature_source` CHECK).

**Why:** a v2 of an existing feature would change the resolved keys of Phase 4A strategies
and stored snapshots; new names keep every earlier key and hash valid.

### D-092 Point-in-time asset eligibility; SHORT needs shortable and easy to borrow

**Decision:** before any input is computed, the engine loads the newest Phase 3A provider
reference version observed by the evaluation's knowledge time. No version, a non-ACTIVE
status or `tradable` not true makes the asset ineligible (`INELIGIBLE_ASSET`, no snapshot).
A SHORT hypothesis additionally needs `shortable` and `easy_to_borrow` both reported true;
unknown counts as no. If no configured direction is eligible the evaluation is
`INELIGIBLE_ASSET`; otherwise the blocked direction is a failed `eligibility` check and its
drafts are dropped by validation.

**Why:** flags known later must not leak into historical evaluations, and a short
hypothesis on an unborrowable asset is not a hypothesis Atlas could ever act on. This gates
analysis only; it authorizes nothing (risk control is later).

### D-093 Evaluations record a signal state

**Decision:** `strategy_evaluations.signal_state` distinguishes SIGNAL, NO_SIGNAL,
INSUFFICIENT_DATA, MISSING_DATA (gap or stale), UNAVAILABLE_DATA (source not covering the
window), INVALID_DATA, INELIGIBLE_ASSET, DUPLICATE and FAILED, with `signal_reason`, the
checks and the eligibility used. A blocked evaluation's state follows its worst feature
status in that order. Expired is a property of a proposal (derived status), not of an
evaluation. CHECKs tie the state to the status (RUNNING has none; PROPOSED is SIGNAL);
pre-4B rows were backfilled from status and skip reasons.

### D-094 One proposal per strategy, asset, timeframe, direction, mode, basis and event

**Decision:** a proposal stores its `evaluation_event` (`<trigger>:<trigger_ref>`), unique
together with strategy, asset, timeframe, direction, mode and basis. The engine checks under
a per-asset advisory lock and records a repeat as DUPLICATE (with the existing proposal's
id); the unique constraint is the backstop. The key is per strategy, not per version, so a
new parameter version re-evaluating the same completed period cannot propose it again.

### D-095 Opposing proposals are recorded as conflicts, never resolved

**Decision:** when a proposal is stored, every proposal for the same asset, mode and basis in
the opposite direction whose active period overlaps (and that was not superseded by then)
is recorded in `strategy_proposal_conflicts` (append-only; ordered pair, so once). Both
proposals stay as they were. Conflicts include different timeframes and the same strategy
(flagged). The per-asset advisory lock serialises proposal writes so concurrent strategies
see each other's proposals.

**Why:** different strategies may legitimately disagree; choosing between them is the
Phase 5 coordinator's job, which needs the disagreement on record.

### D-096 Event-driven: events must precede the reaction; direction comes from price

**Decision:** event-driven counts articles with `period_news_count`, whose window runs from
the close of the session `event_sessions` before the newest completed one to the newest
one's close (exchange calendar, so weekends and holidays are covered), with only events
available by the knowledge time. Direction comes only from the price reaction (in ATR),
confirmed by volume and close location; Atlas stores no sentiment and article wording is
never an input. Missing coverage blocks (UNAVAILABLE), never counts as no news.
Macroeconomic and central-bank events are not evaluated: the economic calendar is
NOT_CONFIGURED (D-077), and every evaluation records this.

### D-097 Mean reversion: opposing trends by regression slope and regimes, not ADX

**Decision:** "do not trade blindly against strong trends" is implemented as: the long-term
SMA filter; no proposal when the `trend_slope_length` (50) regression slope opposes the
trade with a t-statistic of at least `max_opposing_slope_tstat` (3); a BEARISH (for LONG) or
BULLISH (for SHORT) Phase 3B market trend regime; and blocked market volatility regimes
(EXTREME). ADX is recorded as context only.

**Why:** testing showed ADX(14) is driven by the very selloff that creates the stretch, so
an ADX guard rejected nearly every genuine setup; a longer regression measures an
*established* trend.

### D-098 Catalog sync writes new parameter fields' defaults into a new configuration

**Decision:** when the code adds parameter fields, sync creates the new version with a new
configuration that keeps every stored value and writes the new fields' defaults explicitly;
the version's reason names the added fields. Renamed or removed fields still make sync
report a problem instead of guessing. All six families kept every Phase 4A parameter name.

## Phase 5A — machine-learning infrastructure

### D-099 scikit-learn, XGBoost (CPU build), NumPy and joblib are dependencies

**Decision:** Phase 5A adds `numpy`, `scikit-learn`, `xgboost-cpu` and `joblib` (SciPy comes
with scikit-learn). `xgboost-cpu` is the CPU-only XGBoost wheel: the default `xgboost` wheel
pulls in CUDA libraries the image does not need. `uv.lock` was regenerated with the pinned uv
0.13.6 inside the backend image. Polars (D-056) holds dataset frames; models receive NumPy
arrays. Phase 3B analytics stay in plain Python (D-066 unchanged). No neural-network library
is added (spec 12.2: complexity must earn its place).

### D-100 The ML engine reuses the strategy feature pipeline; datasets are RECONSTRUCTED

**Decision:** dataset features are computed by `app.strategies.inputs.compute_features`, the
code path strategies use, at each historical decision time (the close of a completed period
plus the settle delay). A dataset fixes one `data_known_at`: bars and stored features are
read as Atlas knew them then (RECONSTRUCTED basis, D-068), so rebuilding the same
specification reproduces the same content hash.

`compute_features` gained an optional `bar_source` (default: the database query). The builder
passes a preloaded source that returns exactly the rows the query would, for one knowledge
time only. `last_input_period_at <= decision_at` is a database constraint, and an
integration test checks the builder's values against direct `compute_features` calls.

### D-101 Observations: period closes, strategy replay and recorded proposals

**Decision:** three observation sources.

* **PERIOD_CLOSE:** every completed period of every listed asset, in each requested
  direction.
* **STRATEGY_REPLAY:** the Phase 4B strategies evaluated in process at every completed
  period. Nothing is stored as a proposal. An observation is a proposal a strategy would
  have made, with its version (`id@logic#parameters-hash`) and levels. Strategy parameters
  are the newest version registered by `data_known_at`, else the code defaults (recorded).
* **STRATEGY_PROPOSALS:** proposals the laboratory recorded.

Rows whose required features are not VALID are rejected at build time and counted by reason.

### D-102 Targets are net of a stated cost assumption and direction-adjusted

**Decision:** every target is computed from the realized path after the decision:

* entry at the next period's open (default) or the decision close;
* exit at the horizon's close or a barrier;
* direction-adjusted (SHORT gains when price falls);
* minus an assumed round-trip cost (default 10 bps). Atlas has no execution cost model
  before Phase 7, and borrow fees are not modelled.

Kinds:

* FORWARD_RETURN;
* DIRECTION: net return above a threshold. This means "would have made money after costs",
  not "price went up";
* VOL_ADJUSTED_RETURN and VOL_ADJUSTED_DIRECTION, in units of the ATR known at the decision;
* BARRIER: profit barrier before stop, set by ATR multiples or the strategy's own levels.
  When one bar crosses both, the stop counts first (spec 18.1); gaps exit at the open.

Each definition and its documentation are stored with the dataset. A label whose horizon had
not settled by `data_known_at` is IMMATURE; a label with a missing bar on its path is
UNDEFINED. Neither is ever trained on or evaluated.

### D-103 Splits are chronological, purged and embargoed; the test period is evaluated once

**Decision:**

* Splits are defined on decision times (train / validation / test fractions of the distinct
  times, default 60/20/20) and are never shuffled.
* Earlier observations whose labels end after a later segment begins are purged.
* The first `embargo` decision times of each later segment are skipped (default: the longest
  label horizon).
* Hyperparameters are selected by expanding-window walk-forward folds on the development
  period only. A grid is capped at 12 candidates and the number tried is recorded.
* Purged k-fold and CPCV run on the development period as supplementary robustness.
* Every split is re-checked by `assert_disjoint` before use.
* Runs on a dataset version record how many earlier runs evaluated the same test period
  (`test_period_reuse`).

### D-104 Preprocessing is fitted on training rows and stored as data

**Decision:** the following are all fitted on the training period:

* feature selection: missing share, constant, near-duplicate correlation, and an optional
  mutual-information top-k;
* clipping;
* median imputation with missing indicators;
* standard scaling (linear models);
* one-hot encoding of categorical features and context.

The fitted state is stored as plain JSON with the run. Evaluation and inference rows are
transformed, never refitted. Row rejection judges each row alone.

### D-105 Direction interactions and normalized uniqueness weights

**Decision:**

* When training holds both LONG and SHORT observations, preprocessing adds each numeric input
  multiplied by the direction's sign.
* Overlapping labels are down-weighted by 1 / label concurrency, normalized to mean 1 within
  each training set.

**Why:** found on the planted-signal test fixture.

* The same move means opposite outcomes for the two directions. With only a direction
  indicator, models learned the pooled effect and were *anti*-predictive for SHORT (test
  AUC 0.34).
* Unnormalized weights (mean about 0.2) made XGBoost's `min_child_weight` five times
  stricter than intended.

With both fixes the fixture's test AUC is 0.79, and pure-noise fixtures are still never
validated.

### D-106 Probabilities need calibration evidence; the test period can only withdraw the claim

**Decision:** classifier outputs are UNCALIBRATED_SCOREs unless a calibration fitted on
validation data passes on a held-out half of the validation period. Passing means:

* at least 100 observations;
* ECE at most 0.05;
* Brier no worse than the base rate.

Sigmoid and isotonic are compared there. The test period never fits or selects a calibration,
but a calibration whose test ECE exceeds 0.05 is not exposed as a probability.
[REDACTED_FOR_GITHUB] Reliability tables are stored.

### D-107 The evidence verdict is recorded; promotion is always an explicit decision

**Decision:** each run records a `validation_status`. PASSED requires all of the following on
the untouched test period (at least 200 observations):

* the model beats its constant baseline (log loss or RMSE);
* AUC (or directional accuracy) is above 0.5;
* the observations it selects have a positive mean net return, above that of taking every
  observation.

In addition, at least half of the walk-forward folds must beat their baselines.

The verdict is evidence, not a promotion. Every version starts as CANDIDATE and changes
status only through the operator API or the CLI.

### D-108 Registry lifecycle and immutability

**Decision:**

* The lifecycle is CANDIDATE → VALIDATED → SHADOW → APPROVED; REJECTED and RETIRED are final.
* VALIDATED and APPROVED need a PASSED verdict, and APPROVED needs SHADOW first.
* A second APPROVED version of the same model name is refused until the operator retires the
  first. Nothing is replaced implicitly.
* Status is an append-only event chain (one successor per event). Transitions are serialized
  per model name and audited.
* Model versions, artifacts, datasets, observations, evaluations, calibration results and
  predictions are append-only. A training run's outcome is written once.

### D-109 Artifacts are joblib files verified by sha256 before loading

**Decision:** an artifact holds the fitted model, the preprocessing description, the
calibrator and the input schema. Files are:

* created exclusively (never overwritten) under `ML_ARTIFACT_DIR`;
* recorded with their sha256;
* loaded only from inside that directory, and only after the hash matches, because joblib
  files are pickles that can execute code.

Library versions are recorded with each artifact. The compose backend mounts a named volume
at the default directory.

### D-110 Inference validates schemas strictly and records every request

**Decision:**

* APPROVED-mode inference serves only the APPROVED version; SHADOW mode serves only SHADOW
  versions.
* Features are matched by full canonical key (version, parameters, timeframe). Missing,
  incompatible, non-finite or later-than-decision inputs reject the request.
* Every request, valid or rejected, is stored with its inputs, their hash, the artifact hash
  and the model status.
* `replay` recomputes a stored prediction.
* The ML engine imports nothing from execution, risk, portfolio or accounting, and a test
  enforces this.

### D-111 Analytical values are NUMERIC columns

**Decision:** labels, Brier and ECE values are stored as `NUMERIC` (written from exact
decimal representations, read as floats). This keeps the schema rule that no column is a
floating-point type (`test_no_floating_point_columns`).

### D-112 Market-context features and evaluation segments

**Decision:** two benchmark features were added under new names (D-091): `benchmark_return`
and `benchmark_realized_volatility`.

Evaluation segments are descriptive and point in time:

* market trend: the sign of the benchmark's 60-period return;
* volatility: the asset's realized-volatility percentile, in thirds.

They are not Phase 3B regime classifications. Those are mostly unavailable historically: the
stored `regime_label` feature was dropped by preprocessing for having one category in
training.

## Phase 5B — central AI decision engine and LLM integration

### D-113 The coordinator is a pure function of stored inputs and a versioned policy

**Decision:** `app/ai_engine/coordinator/` splits a decision run into three steps:

* `gather`: point-in-time reads into an `InputBundle` (proposals, recorded conflicts, market
  and asset regimes, Phase 3B asset features, Phase 3A liquidity, pair correlations, Phase 3C
  news events, stored LLM analyses, recorded ML predictions, strategy track records);
* `evaluate`: a pure function of the bundle and the policy (no database, clock or LLM);
* `service`: claims the run, stores every bundle item in `decision_inputs` with its exact
  payload and sha256, and writes the decisions, components, evidence and conflicts and the
  run's outcome in one transaction.

`replay(run_id)` rebuilds the bundle from `decision_inputs` alone and re-evaluates it with the
stored policy parameters; identical decision hashes are the reproducibility check. No data is
re-read and no provider is called.

**Why:** a decision must be reconstructable from what Atlas knew then, not from a fresh query
that may see corrected or later data, and not from a new LLM call that may answer differently.

### D-114 Decision policies are data, content-addressed and versioned

**Decision:** every weight, threshold and rule is a field of `DecisionPolicy` (strict pydantic,
`extra="forbid"`), stored in `decision_policy_versions` by sha256 (a new version only when the
parameters change) and named by every run. `AI_DECISION_POLICY_FILE` or `--policy FILE` selects
a JSON policy; the default is documented in code.

**The defaults are not optimal and were not fitted.** There are no traded or backtested outcomes
to fit them on (Phases 7-8), and the Phase 5A models showed no out-of-sample value. Weights:
signal quality 0.15, ML support 0.15, agreement 0.10, track record 0.10, regime suitability
0.10, reward/risk 0.10, trend alignment 0.08, volatility 0.05, liquidity 0.05, cost efficiency
0.05, data reliability 0.05, news 0.02 (sum 1). News is lowest because LLM labels are
unverified; ML and signal quality are highest because they are the only direct assessments of
the opportunity, and ML only counts when an APPROVED model exists.

### D-115 Scores are heuristics; probabilities only from approved, calibrated models

**Decision:** each component is a documented [0, 1] heuristic, or UNAVAILABLE / EXCLUDED with
its reason; nothing missing is filled with a neutral value. The **heuristic score** is the
weighted mean over available components (weights renormalized; coverage = available weight /
total weight). The **ranking score** discounts it by an uncertainty index (coverage gap,
component dispersion, ML disagreement, reconstructed inputs) and a conflict penalty. Decision
records keep apart the raw model score, the calibrated probability (only from an APPROVED model
with CALIBRATED_PROBABILITY output, labelled with its target), the heuristic score and the
ranking score, and every record carries the note that the scores are not probabilities.

### D-116 Outcomes, rule order and conflicts

**Decision:** proposals are grouped per asset and direction. One strategy counts once (its
strongest proposal leads); different strategies with entries within 1% are recorded as
DUPLICATE_PROPOSALS and counted once for capacity. Rules apply in order; the first decides and
every rule that fires is recorded as a reason:

1. INSUFFICIENT_DATA: coverage below 50 %, or data reliability below 0.5;
2. REJECT: net reward/risk below 1.0 after costs; a supported expected value <= 0; a market
   trend regime opposing the strategy family (policy table);
3. DEFER: inputs older than the timeframe's maximum age; a deferring market volatility regime
   (EXTREME); uncertainty above 0.6; disagreeing approved models;
4. opposite-direction opportunities on one asset within 0.15 ranking score of each other both
   DEFER; otherwise the weaker is DEPRIORITIZED and the stronger keeps a conflict penalty;
5. PRIORITIZE_FOR_REVIEW at ranking score >= 0.55, unless a higher-ranked opportunity with the
   same exposure is correlated at |rho| >= 0.8 (CORRELATED_CAPACITY, DEPRIORITIZE) or five
   are already prioritized; otherwise DEPRIORITIZE.

A run with nothing prioritized also records a NO_OPPORTUNITY decision. Conflicts (opposing
directions, duplicates, ML disagreement, regime conflict, news contradicting price action,
correlated capacity) are stored in `decision_conflicts` with how they were handled; consensus
is never forced. PRIORITIZE_FOR_REVIEW is a candidate for the Phase 6 risk engine, never an
approval; the records hold no quantity, order or approval field.

### D-117 Expected value only where a validated probability means the right thing

**Decision:** potential reward and adverse move are the distances to the lead proposal's own
target and invalidation, net of the assumed 10 bps round trip (ASSUMPTION; borrow fees not
modelled). An **expected value** is SUPPORTED only from an APPROVED model with a calibrated
probability on a BARRIER target with STRATEGY_LEVELS barriers for the same strategy
(`p * reward - (1 - p) * adverse`, with a range from the test ECE), or from an approved
FORWARD_RETURN regression on the same horizon (its estimate, +/- test RMSE). Everything else
is NOT_SUPPORTED. A direction probability is never turned into a profit estimate.

### D-118 Point-in-time rules for decision inputs

**Decision:** RECORDED runs (`knowledge_at = decision_at`, the only basis of LIVE runs) read a
row only if it was stored by the decision time: proposals by `created_at` and RECORDED basis;
supersessions by `recorded_at`; regimes, asset features and correlations by `available_at` and
RECORDED basis; liquidity by `created_at`; LLM analyses by `available_at`; ML predictions by
`predicted_at`, model status from events stored by then, and never a model created after the
decision time. RECONSTRUCTED runs admit rows stored later about periods completed by the
decision time and are labelled. Context older than `max_context_age_hours` (120) is not used.

### D-119 ML authority rules

**Decision:** only predictions whose model was APPROVED at the decision time, with validation
PASSED and a test metric not worse than its baseline, contribute. SHADOW predictions are
recorded as context and never scored; other statuses are excluded with the reason.
Uncalibrated scores and regression estimates carry half the authority of a calibrated
probability (`uncalibrated_authority = 0.5`). LIVE runs ask the inference engine for
point-in-time predictions of APPROVED (and SHADOW) models before fixing the decision time.
No model is APPROVED today (Phase 5A), so the ML component is UNAVAILABLE in every run.

### D-120 LLM providers over httpx; no SDK dependency

**Decision:** `ai_engine/llm/providers.py` implements Anthropic's Messages API (one tool whose
`input_schema` is the output schema, forced with `tool_choice`) and OpenAI-compatible chat
completions (`response_format: json_schema`, strict) over the existing `httpx` dependency. A
FIXTURE provider exists for tests and cannot be configured. The default is
`AI_LLM_PROVIDER=none`: nothing is sent and analyses are UNAVAILABLE. The configured model is
the only model called (no silent fallback). Schemas sent to providers omit keywords some
structured-output modes reject (lengths, patterns); the full strict pydantic models validate
every answer locally.

**Why no SDK:** two small, documented HTTP calls do not justify two vendor dependencies.

### D-121 LLM outputs are stored features; untrusted text is data

**Decision:** LLMs run only in asynchronous jobs (CLI, `app.workers.ai_engine`); the
coordinator imports nothing from `ai_engine.llm` (an AST test enforces it) and reads only
stored, usable `llm_analyses` available by its decision time. Article text travels as escaped
JSON inside a delimited data block that the versioned system prompt declares untrusted;
instruction-like patterns are flagged before sending and the model is asked to report embedded
instructions; flagged analyses are stored with `usable = false` and never read. Symbols the
model adds are dropped and flagged. Schemas have no price, size, order or configuration field.
Labels are `UNVERIFIED_LLM_ESTIMATE`. Conflict explanations are written after a run is final
and are reading material, never evidence.

### D-122 Spending is bounded, priced and recorded before anything is sent

**Decision:** every call goes through `LLMService`:

1. content-hash cache (task, prompt hash, schema hash, provider, model, input hash);
2. cool-down after consecutive failures;
3. admission under an advisory lock (daily and monthly USD budgets including running
   reservations, a daily request limit, an input-token bound), in the same transaction that
   records the RUNNING request and its worst-case reservation;
4. bounded concurrency, a timeout, and retries of rate limits, timeouts and unavailability only
   (honouring `retry-after`);
5. strict validation;
6. response, token-usage and cost records.

Without configured prices every call is refused (UNPRICED). Revisions whose content hash was
already analysed reuse that analysis; revisions with two invalid answers are skipped. Defaults:
$0.50 per day, $5 per month, 200 requests per day, batches of 5 articles.

### D-123 Baseline comparison on realized outcomes

**Decision:** `evaluate` compares opportunities the coordinator prioritized with take-all,
strongest-signal-per-run and not-prioritized groups on realized net outcomes (next open to the
lead proposal's horizon close, Phase 5A label code, cost assumption), each lead proposal once.
Below 30 matured coordinator outcomes the verdict is INSUFFICIENT_EVIDENCE; above it, a Welch
t of at least 2 against take-all (with a positive mean) is reported as
INCREMENTAL_VALUE_OBSERVED: an observation on overlapping, unsized returns, not proof of an
edge.

## Fix — discovery mapping `created_at` from the injected clock

### D-124 Discovery writes a mapping's `created_at` from its clock

*(Made on a branch alongside Phase 5A/5B, hence numbered after D-099–D-123; merged in
before Phase 6A.)*

**Decision:** when directory discovery creates an `asset_identifiers` row it sets `created_at`
to the same `recorded_at` (`max(observed_at, clock.now())`) it writes on the lifecycle events
and reference versions of that run, instead of leaving it to the database's `now()`.
`mappings_at` filters `created_at <= as_of` ("mappings Atlas had recorded by then"), so the
mapping's recording time must come from the same clock as every other point-in-time
timestamp the service writes. In production the injected clock is the real clock, so the
stored value is unchanged in practice; under a fixed or replay clock the database time made
every mapping look recorded after the decision time once the wall clock passed it, and
qualification rejected each asset on `provider_mapping`. Operator-registered mappings
(`AssetMappingRepository.register`) keep the database default.

## Phase 6A — portfolio management and double-entry accounting

The accounting conventions themselves are in [`accounting.md`](accounting.md).

### D-125 Phase 6A builds on Phase 5A/5B; two loose ends fixed first

**Decision:** Phase 6A starts from the Phase 5A/5B commit (`abf1bca`), with the discovery
`created_at` fix (D-124, a sibling branch) merged and the backend Dockerfile repaired: its
Phase 5A `RUN useradd ... && mkdir ...` lost its line continuations (an `&&` began a new
Dockerfile instruction), so `docker compose up --build` could not build the image. The
uncommitted fix in the main checkout was reproduced exactly.

### D-126 Money is Decimal; functional values in the reporting currency's minor units

**Decision:** every amount is a `Decimal` (floats refused at every input), computed in a
60-digit trapping context and rounded half-even only at documented points: currency amounts
to the currency's minor units, functional values to the functional currency's minor units
(cents), inverted rates to 12 places. Inputs are validated, never rounded.

**Why cents for functional values:** an earlier draft carried functional values to 8 places.
The seeded random-sequence test found that a conversion's realized FX then has sub-cent
digits that cannot be posted as an ordinary USD line. Keeping the ledger in the reporting
currency's own precision is the usual convention; rounding differences land in FX lines.

### D-127 One system chart; balances per account, currency and asset

**Decision:** a fixed chart of 19 ledger accounts (code-defined, seeded by migration 0013,
append-only). Restricted short proceeds (1010) are separate from cash (1000) so they are never
available cash. Valuation adjustments (1150, 2150) are adjuncts beside cost (1100, 2100),
so cost basis and unrealized valuation stay separate. FX translation (1910/4300) uses
functional-only lines in the foreign currency they relate to.

### D-128 R1 (per currency) and R2 (functional) enforced twice

**Decision:** every entry balances per currency and in functional value, checked in Python and
by a deferred constraint trigger at COMMIT, together with: lines only in the entry's own
transaction (`posting_txid`), a gapless per-account sequence, chart line rules and currency
precision in `atlas_journal_line_guard`, and append-only triggers on every ledger table.
Reconciliation re-checks it all after the fact.

### D-129 Postings serialize on the portfolio account row; idempotent by event ID

**Decision:** a posting locks its `portfolio_accounts` row `FOR UPDATE`, so balances,
positions and sequence numbers are read and written consistently (spec 14.3's locking pattern;
the same row will be the lock target of Phase 6B reservations). `(account, event_id)` is
unique; a repeat with the same request returns the original entry, a repeat with different
inputs is refused (`IdempotencyConflictError`). The resulting state must introduce no
invariant violation (no negative cash, no position/ledger mismatch, no orphan valuation)
before anything is written. Opening funding is idempotent on the account's funding entry
whatever the time it is re-run.

### D-130 Templates are pure, versioned and recorded on first use

**Decision:** posting templates are pure functions of (state, inputs, rates, instrument
specs). A template's code, version, rules and parameter schema are hashed; the row in
`posting_templates` is written on first use and a stored row whose hash differs from the code
is refused: a template's rules change only with a new version.

### D-131 Multi-currency: historical carrying value, FX clearing, posted translation

**Decision:** conversions balance each currency against 1900; currencies are relieved at their
average historical carrying value; realized FX is functional value received minus carrying
value given up; unrealized translation of each foreign net balance-sheet position is posted at
checkpoints (1910/4300) and released pro rata when the currency is converted. Valuation and
postings use only PROVIDER/OPERATOR rates (plus TEST_FIXTURE on test accounts); executed
CONVERSION rates are documentation only. **Why:** the original amount and currency of every
line are preserved, no currency is ever added to another without an explicit rate, and the
clearing account's functional value is always zero (a reconciliation check).

### D-132 Lot sizes are never guessed

**Decision:** a trade needs an effective-dated `instrument_specs` row (increment, minimum,
multiplier). The CLI can derive one from the provider reference data (`min_trade_increment`,
or whole units for non-fractionable assets) or register it explicitly; a fractionable asset
without a reported increment is refused. Multipliers other than 1 are refused until
derivatives exist.

### D-133 Valuation is posted at checkpoints; stale inputs are flagged, never substituted

**Decision:** unrealized valuation is posted (so the ledger is authoritative for it), only at
explicit checkpoints, only from fresh unadjusted prices and fresh rates; each checkpoint writes
an immutable snapshot recording the inputs and their provenance. Stale or missing inputs leave
the position or currency at its last posted valuation and mark the snapshot STALE or
INCOMPLETE. A checkpoint is idempotent per `as_of` and cannot predate the latest one.

### D-134 Primary and CONTROLLED_TEST accounts

**Decision:** `portfolio_accounts.purpose` is PRIMARY (the $100,000 paper account) or
CONTROLLED_TEST. Phase 6A has no execution source, so trades, costs and conversions are posted
only as CONTROLLED_TEST transactions on CONTROLLED_TEST accounts; the service and the database
(`atlas_journal_entry_guard`) refuse them on a PRIMARY account. Account type, base currency
and purpose are immutable; accounts are never deleted.

### D-135 Reconciliation reports, never corrects

**Decision:** reconciliation runs every check under a share lock and stores every result. A
discrepancy is a FAIL with severity HIGH, an error log line and an audit record; the dashboard
shows it as an accounting error. Nothing is adjusted: the books stay as they are until an
operator posts a correcting reversal. `currency_balances` is a cache maintained in the posting
transaction and is itself reconciled against the lines.

### D-136 Read-only API; the "account" path guard gets an explicit GET-only exception

**Decision:** `/api/v1/portfolio/*` and `/api/v1/accounting/*` are GET only; postings,
funding, checkpoints and reconciliation are CLI jobs. The OpenAPI safety test forbids
"account" in paths to keep brokerage-account endpoints out; the ledger ("accounting") and the
list of simulated paper accounts are allowed explicitly and asserted to expose GET only, like
the read-only market-data trades path.

### D-137 Display formatting never goes through floating point

**Decision:** the dashboard formats the API's decimal strings with BigInt half-even rounding
(accepting Python's exponent form, e.g. `0E-8`), so displayed money is exactly the ledger's.

## Phase 6B — deterministic risk engine

The risk conventions themselves are in [`risk.md`](risk.md).

### D-138 Phase 6B builds on 6A; an assessment is a pure function of stored inputs

**Decision:** this branch started at `main` (Phase 4B) and was fast-forwarded to the Phase 6A
commit (`3e95e37`, which contains 5A/5B); 6A was verified first (1589 passed, 1 skipped,
the same as its report). The assessment is `app.risk.assessment.assess(inputs)`: no
database, clock or randomness. Its inputs (proposal, instrument, market data, the marked
portfolio, correlations, configuration) are stored as JSON with every assessment, so a
stored decision replays to the same content hash (`RiskService.replay`, CLI `replay`).
**Why:** reproducible decisions (spec 3.4) and deterministic vectors that test the real
code, not a copy of it.

### D-139 Hard limits and preferences are immutable, operator-only versions in the database

**Decision:** one configuration per portfolio account; every change is a new gapless
version with previous and new values, field diff, actor, reason and time, written in one
transaction under the account lock and mirrored to the audit log. Hard limits (risk per
trade, positions, loss limits, drawdown, leverage, gross/net/asset/class/sector/correlated/
short/margin limits, stop and short policies) are separate from preferences (cost and
slippage assumptions, correlation threshold and ages, assessment TTL), which never relax a
hard limit. The actor must match `operator:<name>`: the service refuses any other and the
database CHECK refuses it again. Limits cannot come from environment variables. The
starting values are spec 14.1 plus conservative framework values; none is described or
tuned as optimal. `max_leverage` is capped at 1 because the 6A chart has no margin loan.

### D-140 Strategies and AI contribute levels only

**Decision:** `Proposal` has no score, strength, probability, expected value or text field
(extra fields are refused). Strategy proposals and AI decisions are converted by reading
database rows (`app.risk.proposals`): entry, invalidation level (stop), target, horizon,
expiry, ids. Only `PRIORITIZE_FOR_REVIEW` decisions are accepted. Static tests assert that
`app.ai_engine`, `app.strategies`, `app.ml_engine`, `app.news` and `app.workers` import
nothing from `app.risk`, and that `app.risk` imports none of them. **Why:** absolute
separation of recommendation and authority (spec 3.3, 3.6, 12.6).

### D-141 Circuit breakers are an append-only event stream with fixed recovery rules

**Decision:** state = latest event per (account, breaker) (`risk_circuit_breaker_states`
view); no mutable state row. Events are numbered without gaps and must start from the
previous state (trigger). Loss, drawdown, exposure, reconciliation, execution-safety and
kill-switch breakers need an operator reset, refused while the condition is still breached
or cannot be measured (the refusal is recorded); only DATA_QUALITY recovers automatically,
recorded as a SYSTEM reset (the database refuses a system reset of anything else). A trip
blocks new entries (RISK_HALTED); nothing is liquidated; monitoring continues; the event
records that risk-reducing operations stay permitted for the execution phases.

### D-142 Sizing never lets rounding add risk

**Decision:** spec formula first, then: entry moved adversely and stop away from entry on
the tick grid (provider increment, else the SEC Rule 612 increment for US equities; crypto
needs a reported increment); loss per unit includes half spread and slippage at entry, half
spread and stop slippage at exit, fees on both legs, FX conversion cost, and borrow over the
holding period for shorts; commissions per unit with a per-leg minimum; quantity solved in
closed form, floored to the lot, then verified with losses rounded up (currency, then
functional) against the budget rounded down, stepping down a lot while it exceeds. Every
capacity limit (capital, gross, leverage, net, single asset, asset class, sector, correlated
cluster, short exposure, margin, liquidity) is a quantity; the smallest binds and is named.
Shorts get `short_risk_budget_fraction` (0.5) of the budget: a stop does not cap a gap.
Forex is refused (no contract specification; 6A holds currencies as balances).

### D-143 Loss periods follow the NYSE calendar; baselines are fixed once

**Decision:** one portfolio-wide calendar, XNYS (as 6A freshness). A risk day ends at the
regular close (early closes included); a risk week is the ISO week of the risk day.
Start-of-period equity is, in order: the latest COMPLETE equity observation during the
session that closed at the boundary, else the earliest one after it and before the next
open; the opening funding if funded during the period; else the first observation in the
period (flagged FIRST_OBSERVATION_IN_PERIOD: earlier losses are not captured). It is stored
once per period. Loss = fall in investment return (equity − contributed capital), so it
includes realized and unrealized P/L, FX and costs and never counts funding. Peak equity =
highest investment return observed (zero at funding) + capital.

### D-144 Exposure is the ledger marked in memory; nothing is netted or invented

**Decision:** the risk state applies the 6A valuation template to a copy of the ledger
state with fresh prices and rates, without posting, so equity and exposure follow the
accounting equation. Long = 1100+1150, short = 2100+2150 per asset. Any stale or missing
input makes the mark STALE/INCOMPLETE (DATA_QUALITY). Gross adds long and short; sector,
class and correlated exposures are gross. A held asset counts as correlated with the
candidate if it is the same asset, shares an operator cluster tag, or |corr| ≥ threshold
(newest OK Phase 3B PAIR row ≤ 7 days); an unmeasured pair counts under ASSUME_CORRELATED
(default). Unclassified assets share one UNCLASSIFIED sector bucket (default POOL). Pending
exposure is 0 with the reason (no reservations before 6C).

### D-145 Short safeguards reject on missing data by default

**Decision:** a short needs shorts enabled, a supported instrument, a provider reference
that says shortable and easy-to-borrow (unknown is a rejection), and a short margin
requirement unless policy says USE_CONFIGURED_MINIMUM (then recorded as an assumption).
Provider margin percentages are read as collateral beyond the restricted proceeds, the
conservative reading; the larger of that and `min_short_margin_rate` (Reg T 50%) is used.
Borrow cost is an ASSUMED annual rate (no borrow-rate source exists), recorded with each
assessment. Short exposure and margin usage are limits.

### D-146 The API reads; its only write can only halt

**Decision:** `/api/v1/risk/*` is GET, plus `POST /risk/kill-switch/engage` (operator
token, actor `operator:api`). Releasing the switch, resetting breakers, changing limits,
classifying assets and recording assessments are CLI actions (`python -m app.risk.cli`)
with `--actor operator:<name>` and a reason. **Why:** without `ATLAS_OPERATOR_TOKEN` the
development API is unauthenticated; nothing reachable that way may loosen risk.

### D-147 Database guards, and what they do not guarantee

**Decision:** `atlas_risk_assessment_guard` refuses a PASS_FOR_RESERVATION row whose
configuration is not the latest, whose budget exceeds equity × the configured risk per
trade, or while any breaker of the account is tripped; CHECKs require a pass to be sized
within its budget; `atlas_risk_assessment_consistent` (deferred) requires the stored
result to follow from the stored checks; checks can only be written in their assessment's
transaction; every risk table is append-only. Services take the portfolio account lock.
**Not guaranteed:** a pass reserves nothing, so two passes may together exceed a limit;
the guards read committed state only and cannot recompute a sizing. Phase 6C must re-check
under the lock and persist the reservation in the same transaction.

## Phase 6C — atomic risk reservations

### D-148 One lock protocol per account; READ COMMITTED; a database backstop

**Decision:** every reservation command is one PostgreSQL transaction at READ COMMITTED
that locks, in this order: the `portfolio_accounts` row (`FOR UPDATE`, the lock every 6A
posting and 6B assessment/monitor/breaker action already takes), then the account's
`risk_reservation_states` row, then reservation rows in id order. One lock per account
serializes all financial writers of that account, and the fixed order rules out deadlocks.
Under READ COMMITTED each statement after the lock sees everything committed before it was
granted. The state row carries a gapless event counter read *before* any capacity input; the
event trigger refuses an event whose number is not the next one with SQLSTATE 40001, so a
path that skipped the lock fails instead of committing a decision made on stale data.
**Why not SERIALIZABLE:** it would add retries without adding safety over the explicit lock,
and 6A/6B already rely on the same row lock. **Why not Redis:** spec 3.2; Redis is not used
for reservations at all.

### D-149 A reservation is capacity, not money

**Decision:** reservations hold risk budget (potential loss at the stop), gross exposure,
available capital and short margin in their own tables. Nothing is posted to the ledger:
the 6A ledger changes only when a fill is posted (Phase 7). The risk view adds active
remainders to actual positions for every limit; a consumption must reference a ledger fill
of the same asset, direction and quantity, so the consumed part moves from "reserved" to
"held" without being counted twice or disappearing.

### D-150 The assessment is recomputed inside the reservation transaction

**Decision:** `reserve` runs the 6B evaluation and the pure assessment inside its own
transaction under the lock, with the active reservations in the state, and approves exactly
the permitted quantity. The database refuses an approval unless a PASS_FOR_RESERVATION
assessment recorded *in the same transaction* permitted exactly that quantity, loss,
notional, capital and margin, no breaker is tripped and the latest reservation
reconciliation did not fail. Earlier assessments by a worker are never trusted. Proposed and
approved quantities are stored separately; every reduction (RESTRICT checks) and rejection
(FAIL/UNKNOWN/HALT checks, identity refusals) is stored with the reservation.

### D-151 Reserved capacity in the assessment

**Decision:** reserved risk reduces the loss allowance (the room before the daily, weekly or
drawdown limit); reserved exposure counts toward gross, leverage, single-asset, asset-class,
sector, correlated-cluster and liquidity limits, reserved short exposure and margin toward
the short and margin limits, reserved capital against available capital, reserved assets
toward the position count. For the net limit only same-direction reservations count (worst
case: they fill, the others do not); an opposite reservation on the same asset is refused.
Foreign reservations are re-converted at the current rate when that is higher, never lower;
without a fresh rate new approvals are INSUFFICIENT_DATA. Assessment engine version 6C.1.
**Limitation (inherited from 6B):** open-position risk to the stop is not tracked, so a
reservation's risk stops counting against the allowance once it is consumed.

### D-152 Lifecycle and expiry

**Decision:** states PENDING → APPROVED | REJECTED; APPROVED → PARTIALLY_CONSUMED → CONSUMED;
APPROVED / PARTIALLY_CONSUMED → RELEASED | EXPIRED | FAILED. PENDING never commits (a
deferred trigger refuses it), so an interrupted transaction leaves nothing. A reservation
expires at its assessment's validity (300 s by default) or the proposal's expiry, whichever
is first, and only if no order was ever attached and nothing consumed. Once an order is
attached (Phase 7 interface) or the execution state is flagged uncertain, it never expires
or releases on its own: it is marked EXPIRY_DEFERRED once and held until the execution state
is resolved (consume, release with a confirmed resolution, fail, or an operator release).
A release gives back only the unconsumed remainder; fills are never reversed by it.

### D-153 Idempotency

**Decision:** every command (reserve, attach, consume, release, fail, flag) carries an
idempotency key, unique in `risk_reservation_requests` with the request's SHA-256. The key is
looked up after the lock: a retry with the same request returns the original decision and
the current state, a different request under the same key is a 409. A partial unique index
allows one live reservation per strategy proposal or AI decision. A lost uniqueness race is
retried once and then returns the committed winner; serialization failures and deadlocks are
retried.

### D-154 Reconciliation blocks approvals; nothing is repaired automatically

**Decision:** `ReservationReconciler` replays every event independently of the trigger and
compares the projection, the per-account totals, gapless numbering, approvals against their
assessments, idempotency records and every consumption against the asset-quantity ledger.
Any mismatch FAILS the run, writes a CRITICAL `system_health_events` row and a
RESERVATION_MISMATCH risk event, and blocks new approvals (service and database) until a
later run passes. Expiry due, held past expiry and uncertain execution are warnings.

### D-155 Read-only API; execution interfaces for Phase 7

**Decision:** `/api/v1/risk/reservations*` is GET only. Reserving is a service call (and an
operator CLI command); attach/consume/release/fail/flag are service interfaces for the
execution engine (`consume_in` runs inside the transaction that posts the fill). In 6C they
are exercised only by controlled test events.

## Phase 7A — paper execution engine and order management

### D-156 Orders are an event-sourced projection with a database-enforced state machine

**Decision:** `orders` is a current-state projection that only the `order_events` insert
trigger (`atlas_order_event_apply`) may change; events are append-only and gapless per
order. The legal transitions live in one Python table (`state_machine.TRANSITIONS`) and are
frozen in migration 0016 as `atlas_order_transition_allowed`; tests compare the two.
ACCEPTED/FILL/CANCEL_CONFIRMED/CANCEL_REJECTED must reference a paper broker event; the
engine cannot finish an order the broker is working. PROPOSED never commits.
**Why:** the same pattern 6C uses for reservations: application bugs and direct writes
cannot produce an illegal or silently overwritten order history.
**Spec deviation (documented):** spec 15.1 names PENDING_RISK and APPROVED_RESERVED; the
7A prompt names PROPOSED and APPROVED. 7A uses the prompt's names: the risk decision and
the reservation both happen before an order exists (6C), so PROPOSED covers "pending risk"
within the submission transaction and APPROVED means "approved, reservation attached".

### D-157 One ENTRY order per reservation; exits link to their parent

**Decision:** an ENTRY order executes exactly one APPROVED 6C reservation (partial unique
index, insert guard, one `ORDER_ATTACHED`), never above the approved quantity and never
priced beyond the approval's worst entry price (`protection_price`). EXIT orders (take
profit, protective stops, closing orders) reduce the filled position of an ENTRY order:
they reference the parent's reservation for traceability only and consume nothing.
**Why:** an exit reduces risk and cannot open exposure (quantity bounded by the parent's
fills and the ledger position); requiring a fresh reservation for it would block closing a
position while breakers are tripped, contrary to spec 24.2. Exits are therefore accepted
while new entries are halted.

### D-158 Who may submit

**Decision:** only `system:execution[-...]` (the execution coordinator) and
`operator:<name>` may submit or cancel; any system actor naming a strategy, AI, LLM, ML,
model or decision component is refused, as is anything else. Operator-sourced
reservations can only be executed by an operator. Refused requests are recorded
(`execution_requests`, REFUSED, with reasons) and touch no reservation. A static test
checks that strategies, the AI and ML engines, intelligence and news import nothing from
`app.execution`, `app.paper_broker` or `app.events`.

### D-159 Transactional outbox in PostgreSQL; handlers run inside the delivery transaction

**Decision:** `outbox_events` (spec 4.1) with `dedupe_key` UNIQUE; one dispatcher claim =
one transaction: `FOR UPDATE SKIP LOCKED`, handler in a savepoint, DELIVERED in the same
commit; failures recorded with exponential backoff; DEAD after 5 attempts with a
per-topic recovery. Redis is not used.
**Why:** the paper broker's book is in the same database, so delivering and acting can be
one atomic step: a crash leaves the event pending and nothing half-done; retries cannot
duplicate an order (UNIQUE `client_order_id` at the broker, state checks under the order
lock).

### D-160 Fills post as CONTROLLED_TEST in Phase 7A

**Decision:** the fill -> 6A contract (`PositionLedger`) posts through the existing
templates with source CONTROLLED_TEST, which 6A accepts only on CONTROLLED_TEST accounts.
`AccountingPositionLedger` refuses any other source. `order_fills` and
`paper_broker_events` CHECK constraints accept FILL only from CONTROLLED_TEST.
**Why:** 7A has no fill model; nothing may present a fill as anything but a controlled test
event. Phase 7B adds a simulated-execution source (6A `EntrySource`, the CHECKs and this
class) as an explicit, migrated change.

### D-161 Broker events are applied in broker order, once

**Decision:** each book row numbers its events without gaps; `orders.
applied_broker_sequence` must advance by exactly one per applied event (trigger). A
delivery applies all earlier unapplied events first; a repeat is a no-op.
**Why:** a cancel confirmation applied before an earlier fill would release capacity the
fill then needs; ordering makes the outcome independent of delivery order and concurrency.

### D-162 Cancellation and expiry release only confirmed, unfilled exposure

**Decision:** cancellation is a request (CANCEL_PENDING); the reservation's remainder is
released only when the broker's CANCEL_CONFIRMED is applied. An order the broker never
received is cancelled with a broker tombstone (so a late submission is answered with the
cancellation). Expiry of a working order is decided by the broker (EXPIRED event); of an
order it never received, by the engine. A dead cancellation holds the capacity, flags the
reservation uncertain and trips EXECUTION_SAFETY. Order lifetime defaults to 1 day, at
most 7 days (constants, no environment variable).

### D-163 Post-fill 6A reconciliation through the outbox

**Decision:** every fill enqueues `accounting.reconcile`; its handler runs the 6A
reconciliation (`trigger = POST_FILL`) unless a run already covers the latest entry.
**Why:** 6B requires a current reconciliation for an assessment (the 6C report's open
limitation); without it the first fill would make every later reservation
INSUFFICIENT_DATA until an operator ran the reconciliation.

## Phase 7B — simulated execution and automated position management

### D-164 Fills are simulated by the paper broker from stored observations only

**Decision:** the execution simulator (`app.execution.simulation`) evaluates each working
order against the stored `market_quotes`, `market_trades` and `market_bars` rows that were
*available* (`available_at`) and had *occurred* (`event_time`) at the evaluation time, with
quality VALID, after the order reached the simulator plus an execution delay (1 s). The pure
fill model (`app.paper_broker.fill_model`) decides; nothing is interpolated, carried forward
or invented. Each observation's liquidity counts once per order (book-row cursors: the quote
id used, the last trade and bar). One evaluation is one transaction: trigger and fill events,
the 6A posting, the 6C consumption, the fill and the position.
**Why:** spec 15.3/15.4 and the 7B prompt: actual market-data inputs, no look-ahead, no
duplicate fills under retries, restarts and concurrent workers.

### D-165 Conservative execution rules, versioned and content-addressed

**Decision:** marketable orders execute at the touch of the newest fresh, unused quote (buy
at the ask, sell at the bid) plus slippage and a linear impact proportional to participation,
rounded adversely to the tick; resting limits fill only on volume printed strictly through
the limit (trades; bars when there are no trades), at the limit, at a participation share;
stops trigger on the touch or trades/bar extremes through the stop and execute as market
orders never better than the stop; gaps fill worse; stop-limits may stay unfilled; trailing
stops ratchet only after the observations they are checked against; a quote older than the
newest trade or bar is not the current market; entries never fill beyond the reserved worst
entry price and not while a new-entry breaker is tripped. All numbers live in an
`ExecutionModel` stored once per content hash in `execution_models`; every book row and
simulated fill records its version.
**Why:** "do not assume a touch is a fill", "do not guarantee the stop price", "never invent
an intrabar path"; a changed assumption must not rewrite how earlier fills were derived.
These rules are deliberately conservative, not unbiased.

### D-166 Missing data defers; the bar fallback is an explicit, labelled assumption

**Decision:** without a fresh quote a market order may use the newest completed fresh bar's
close widened by an *assumed* half spread (BAR_FALLBACK, configurable, recorded as an
assumption); a quote without size, a bar without volume, a stale quote with no usable bar,
a closed equity session and an unsupported asset class defer (no fill, a DEFERRED attempt).
**Why:** the IEX feed has gaps and covers a small share of volume; a fill is never derived
from fabricated liquidity or a fabricated quote.

### D-167 SIMULATED_EXECUTION: a 6A source for the primary paper account

**Decision:** 6A gains `EntrySource.SIMULATED_EXECUTION` (migration 0017), accepted on any
paper account for the trade, fee, financing and accrual-settlement templates only. A fill's
6A source follows its broker source (CONTROLLED_TEST -> CONTROLLED_TEST, SIMULATION ->
SIMULATED_EXECUTION); there is no setting. `order_fills` and `paper_broker_events` accept
SIMULATION fills, which must name their execution model.
**Why:** D-160 deferred this to 7B: simulated fills must post to the primary paper account
through the existing templates, auditable as simulated, never as anything else.

### D-168 A position is the trade of one ENTRY order

**Decision:** `paper_positions` holds one row per ENTRY order that filled: opened by its first
fill, increased by later entry fills, reduced by its EXIT orders' fills, closed when nothing is
open and the entry order is final. Realized P/L is the 6A ledger's realized P/L of the exit
fills (average cost); costs are the fills' costs plus accrued borrow. Every change is a
gapless, append-only `position_events` row; guards keep identity immutable, quantities and
costs monotonic, the stop only tightening and closed positions final. An entry against an
open position of the other direction is refused (`OPPOSITE_POSITION`).
**Why:** the dashboard needs per-trade entry/exit, exit reason and strategy origin, without
a second accounting system. Limitation: two open positions in one asset share the ledger's
average cost.

### D-169 Protection is position-level; one protective exit at a time (OCO)

**Decision:** a position's protection is levels, not resting broker orders: the approved stop
(6C approval; may only tighten), an operator trailing stop, the strategy proposal's
invalidation level and holding horizon (expiry), and the target. The position monitor checks
the adverse levels against the observations since its last pass and, when one is reached (or
on expiry or an operator close request), withdraws the entry's unfilled remainder, cancels
resting take-profits and, once they are final, submits **one** MARKET exit for what the
position holds. The target is a resting TAKE_PROFIT limit at the broker (filled only by the
limit rules). Exit quantities are bounded by `closable_quantity`, so a stop exit and a
take-profit can never both close the same quantity.
**Why:** OCO without races between two resting orders; simulated execution delay between a
trigger and its exit; exits keep working while new-entry breakers are tripped (D-157).

### D-170 Stop first when the order of events is unknown

**Decision:** when a bar (or the trades since the last pass) reached both the stop side and
the target, the monitor treats the stop as first; the simulator does not fill a take-profit
from activity at or after the first observation that reached the position's binding stop
(`STOP_REACHED_FIRST`). With trades, time order is known and used.
**Why:** spec and prompt section 9: the conservative stop-first assumption; no intrabar path.

### D-171 Costs: commissions per order, regulatory fees per fill, borrow accrued daily

**Decision:** commissions are computed on the order's cumulative fills (minimum and cap per
order); SEC-style and per-share sell fees per fill; all rounded up to the currency's minor
unit. Short positions accrue borrow daily at the model's annual rate on the last fresh mark
(6A `financing_cost`, accrued) and the accrued amount is paid when the position closes
(`accrual_settlement`). Long financing is zero (no margin borrowing); crypto funding is not
applicable (spot only). The default rates are configured assumptions, not current fee
schedules.

### D-172 Workers and polling

**Decision:** the paper execution worker runs the simulator each tick after outbox delivery,
skipping orders with no observation newly available since their last evaluation, and doing
nothing while the broker is not RUNNING. The position-monitor worker passes every 5 s by
default; it evaluates *all* trades and bars since its cursor, so a longer interval delays an
exit but misses no touch. Equity protection is evaluated on regular-session data only.


---

## Phase 8A — event-driven historical backtesting

### D-173 Phase 8A builds on the uncommitted 7A–7B work, brought in as a baseline commit

**Decision:** this branch started at Phase 4B (`main`). Phases 5A–6C existed only on commit
`0023e5e`, and 7A–7B only as uncommitted files in the `atlas-phase-7a-execution` worktree. With
the operator's approval the branch was reset to `0023e5e`, and the 7A–7B working tree was copied
unchanged and committed as `30b9749` ("Phase 7A-7B ... (baseline)"). The full suite on that
commit passed: 3,321 passed, 1 skipped (Redis), the 7B report's count.
**Why:** 8A must reuse 6A accounting, 6B/6C risk and reservations and the 7B fill model. Building
on 4B would have meant a second, disconnected trading system.

### D-174 One replay engine in memory, reusing the pure cores; results in `backtest_*` only

**Decision:** a backtest is an in-memory, single-threaded, event-driven replay
(`app.backtesting.engine`). It calls the same code paths paper trading uses:
`app.strategies.engine.decide` (extracted from `StrategyEngine.run`, which now calls it too),
`compute_features`, the 6B `assess()` with a 6B `PortfolioView`, the 6C reservation amounts and
proportional consumption, the 7A order state machine (`next_status`), the 7B `fill_model.evaluate`
with a versioned `ExecutionModel`, and the 6A posting templates with the posting service's
validation and invariant checks. It does not use the database-bound services (`RiskService`,
`ReservationService`, `ExecutionService`, `PostingService`), and it never writes to a live trading
table. Results are stored in fourteen `backtest_*` tables.
**Why:** the live services read "now" (fresh quotes, current liquidity and correlation
measurements, the latest configuration, breaker tables) and serialize on database locks built for
concurrent live traffic. Historical decisions need the same rules evaluated on point-in-time
inputs. Writing replays into `orders`, `journal_entries` or `risk_reservations` would mix
simulations with the paper account's records. The pure cores are where the rules live, so
reusing them keeps one set of rules. A parity test runs the 4B `StrategyEngine` (HISTORICAL,
RECONSTRUCTED) and the replay on the same inputs and finds identical proposals.

### D-175 Time, availability and ordering

**Decision:** a session's information becomes known at one instant,
`t = regular close + settle_seconds` (default 3,600 s, the 4A strategy setting). At `t` the replay
processes, in a fixed order: corporate actions, execution against the session's bar, entry expiry,
valuation and borrow, reconciliation, the equity snapshot and the risk monitor, horizon exits, and
then strategy decisions. Orders arrive `latency` after `t`, and a bar is used only by orders that
arrived before it started. An entry can therefore never fill on the bar that produced its signal.
Ties are broken by fixed keys (timeframe, strategy, symbol, direction, sequence). There is no
randomness; the seed is recorded and seeds an unused generator.
**Why:** prompt sections 1–2. Only completed and settled candles are visible, events are
chronological, and a replay of the same inputs produces the same results.

### D-176 A REST backfill's retrieval time is never an availability time

**Decision:** a historical daily bar is usable from its session close plus `settle_seconds`,
derived from the calendar. Its `available_at` (the retrieval time) is used only to exclude data
Atlas obtained after the run's knowledge time. Backfilled news counts only if published at least
`news_delay_minutes` (default 5) before the cutoff; this needed a backwards-compatible
`news_delay` on `StoredContext` and `compute_features` (default 0: 4A behaviour unchanged).
Bar revisions are RECONSTRUCTED: the newest revision ingested by the knowledge time. That is a
stated limitation.
**Why:** prompt section 2: when the original availability is unknown, apply a documented
conservative policy.

### D-177 The dataset is frozen at the run's knowledge time and fingerprinted

**Decision:** `data_known_at` (default: the creation time) is resolved once and stored. The loader
reads only rows ingested and available by then: bars, reference versions, instrument
specifications and classifications. A `dataset_sha256` over the identities and revisions of every
row read is stored with the run. A reproduction re-executes the stored resolved configuration
(nothing re-resolved) and records whether its result hash equals the original's.
**Why:** later ingestion cannot change a replay; reproducibility can be checked rather than
assumed (prompt section 9).

### D-178 Point-in-time split adjustment, and positions carried through splits

**Decision:** strategy features use the provider's SPLIT series rescaled by
`C(T) = raw_close(T) / split_close(T)` of the newest completed session. History therefore appears
at the price level it traded at on that session, and no later split leaks backwards. A change in
`C` between consecutive sessions is a split effective at the later session's open. An open position
is carried with existing 6A templates only: a zero-cost sale at the last pre-split close and a
purchase of `floor(q x ratio)` at `close / ratio`; the remainder is cash in lieu. Working entries
in the asset are cancelled and protective levels rescaled. Execution, valuation and accounting use
RAW prices. Dividends are not modelled.
[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

### D-179 Execution on daily bars: the 7B model plus three stated rules

**Decision:** the backtest's execution model is the 7B model with `bar_timeframe = 1Day`, plus
rules added to its content hash. Each session is one evaluation on that session's bar, with no
quotes or trades. Gap rule: a stop triggered by a bar that opened beyond it fills no better than
the open, adjusted adversely by the slippage; the 7B bar fallback alone caps at the stop, which is
optimistic after a gap. Bracket: the protective stop is active from the entry's arrival and is
evaluated in the entry's bar, while a take-profit is never filled in its entry's bar (stop first).
Entries are LIMIT orders at the approval's worst entry price by default (MARKET, STOP and
STOP_LIMIT are selectable), always bounded by that price as in 7A/7B. Exits at the holding horizon
are market orders that fill on the next session's bar.
**Why:** prompt section 5. Candle prices do not guarantee fills, and no intrabar path is invented.
The gap rule is a finding for 7B (its bar fallback has the same optimism at one-minute scale); 7B
is not changed here.

### D-180 Breakers without an operator; data the live risk engine reads "now"

**Decision:** breaker trips and automatic resets use 6B `transitions` unchanged. Manual breakers
are reset by a simulated operator (`breaker_reset_policy`, default `NEXT_SESSION_IF_CLEAR`, or
`NEVER`) only after the session of the trip and only once `reset_allowed`; every reset is recorded
with the actor `backtest:simulated-operator`. Liquidity (median daily dollar volume) is recomputed
point in time with the 3A `metrics.compute` on the decision's bars. Correlations and sector
classifications use the 6B point-in-time readers. Correlations are therefore usually missing for
past dates, and `ASSUME_CORRELATED` applies, as it would live. The price is the session's RAW close
(FRESH by the 6A rule after the close). There is no quote, so the spread is the configured
assumption.
**Why:** 6B stays unchanged and identical; the backtest supplies its inputs point in time.

### D-181 Read-only backtest records under `/api/v1/backtests`; two operator-gated writes

**Decision:** `POST /backtests` (create and queue a run) and `POST /backtests/{id}/reproduce` need
the operator token, like the other operational writes. The paths
`/backtests/{id}/orders|fills|trades` are GET-only, and the route-guard tests allow them
explicitly, as D-136 did for the paper-order pages. Runs execute on one background thread per API
process, in submission order. At start-up, runs whose process died are failed and runs that never
started are resumed.
**Why:** a replay's simulated orders are not orders. The guard tests still forbid anything that
could submit or execute.

### D-182 Results are immutable and written only while their run runs

**Decision:** database triggers allow `backtest_runs` to change only status and progress, along
QUEUED -> RUNNING -> COMPLETED | FAILED; configuration columns never change, a final run never
changes, and nothing is deleted. Result tables accept INSERT only while their run is RUNNING
(strategy versions: while QUEUED) and refuse UPDATE, DELETE and TRUNCATE. All results are stored
in one transaction with the COMPLETED status and the result hash. A failed run stores its fatal
error, not partial results.
**Why:** prompt section 9: historical records remain immutable.

### D-183 Metrics are withheld, not estimated, on short samples

**Decision:** annualized returns need at least 252 sessions; volatility, Sharpe and Sortino need at
least 126 daily returns. Below that they are stored with no value and status
INSUFFICIENT_SAMPLE or NOT_APPLICABLE. Trade statistics with fewer than 30 closed trades are shown
and flagged. Net figures are after all simulated costs; gross figures add costs back. Benchmarks
are price returns bought at the first session's open, over exactly the run's sessions.
**Why:** prompt sections 7–8.

### D-184 No ML or LLM in the backtest decision path (Phase 8A)

**Decision:** decisions flow strategy proposal -> 6B assessment -> reservation -> order. Model
versions validated by the knowledge time are recorded with the run, and the stated model usage is
"none".
**Why:** the risk engine already accepts strategy proposals directly (6B `from_strategy_proposal`).
Model evaluation, champion/challenger and walk-forward validation are Phase 8B.
