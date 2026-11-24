# Operations (local)

## Start and stop

| Action | Command |
|---|---|
| Start everything | `docker compose up --build` (add `-d` to run in background) |
| Start only data stores | `docker compose up -d postgres redis` |
| Status / health | `docker compose ps` · `curl http://127.0.0.1:8000/ready` |
| Logs | `docker compose logs -f backend` |
| Safe shutdown | `docker compose down` |
| Wipe all local data | `docker compose down -v` (deletes the PostgreSQL volume) |

On shutdown, PostgreSQL gets a 30 s grace period to finish in-flight transactions and
checkpoint, and the API gets 20 s. Redis holds transient data only (persistence is disabled
on purpose), so restarting it loses nothing authoritative.

## Paper environment

For a longer-running paper deployment, set `ATLAS_ENVIRONMENT=paper` and give
`ATLAS_POSTGRES_PASSWORD` (and preferably `ATLAS_REDIS_PASSWORD`) unique values.

The backend refuses to start in `paper` with the development default password or with `DEBUG`
logging. Changing the PostgreSQL password after the volume exists requires `ALTER USER` inside
the database. The `POSTGRES_PASSWORD` variable only applies when the volume is first created.

## Migrations

| Action | Command |
|---|---|
| Apply | `uv run alembic upgrade head` (Compose runs this automatically via the `migrate` service) |
| Current revision | `uv run alembic current` |
| Check models vs migrations | `uv run alembic check` |
| New revision | `uv run alembic revision --autogenerate -m "describe change"`, then **review and edit** the generated file |

Migration history is append-only. Never edit a migration that has been applied anywhere.
Add a new one.

`/ready` reports `not_ready` until the database is exactly at the code's migration head.

## Historical market data

Prerequisites:
* Alpaca market-data credentials in `.env` (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`).
* The database migrated to head.

Run from the repository root.

1. Check the credentials. This calls a market-data endpoint only:

   ```bash
   uv run python -m app.data_engine.cli check-auth
   ```

2. Register assets from `config/market_data/reference_assets.toml`. This is idempotent:

   ```bash
   uv run python -m app.data_engine.cli register-assets --symbols AAPL,SPY
   ```

3. Fetch bars. `--start` and `--end` are inclusive bar start times in UTC. A bare date means
   00:00 UTC.

   ```bash
   uv run python -m app.data_engine.cli fetch --symbols AAPL,SPY --timeframe 1Day --start 2026-10-01 --end 2026-10-28
   ```

   ```bash
   uv run python -m app.data_engine.cli fetch --symbols AAPL,SPY --timeframe 1Min --start 2026-10-28T08:00:00Z --end 2026-10-29T00:00:00Z
   ```

The CLI exits with code 0 only for a `COMPLETED` job. It prints the job (status, counters,
per-symbol results) as JSON on stdout.

Through the API (the operator token is needed when `ATLAS_OPERATOR_TOKEN` is set):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/market-data/historical-fetch -H "Content-Type: application/json" -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN" -d '{"symbols":["AAPL","SPY"],"timeframe":"1Day","start":"2026-10-01T00:00:00Z","end":"2026-10-28T00:00:00Z"}'
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/ingestion-jobs?status=FAILED&status=PARTIALLY_COMPLETED"
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/candles?symbol=AAPL&timeframe=1Day&start=2026-10-01T00:00:00Z&end=2026-10-29T00:00:00Z"
```

To run the opt-in real-provider smoke test (it uses a throw-away database copied from the
migrated template):

```bash
ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=postgresql+psycopg://atlas:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/postgres uv run pytest -m live -s
```

## Live market data streaming

Prerequisites: credentials in `.env`, the assets registered (`register-assets`, above), the
database at head. Alpaca allows **one connection per endpoint**: stop any other stream (or a
running live smoke test) first.

1. Enable it in `.env`:

   ```text
   MARKET_DATA_STREAM_ENABLED=true
   MARKET_DATA_SYMBOLS=AAPL,SPY
   MARKET_DATA_STREAM_CHANNELS=bars,updated_bars,trades,quotes
   ```

2. Start the worker (Compose service `stream-worker`):

   ```bash
   docker compose up -d --build stream-worker
   ```

   Or in the foreground on the host (Ctrl+C stops it gracefully):

   ```bash
   uv run python -m app.workers.market_data_stream
   ```

   `--duration SECONDS` stops it after a fixed time.

3. Stop it. SIGTERM closes the socket, writes queued data, records `STOPPED` and releases the
   single-worker lock:

   ```bash
   docker compose stop stream-worker
   ```

With `MARKET_DATA_STREAM_ENABLED=false` the worker records `DISABLED` and exits 0. Exit codes:
0 ok, 2 configuration error (e.g. unregistered symbol, unavailable channel), 3 another worker
already streams this provider/feed, 4 stopped after a permanent stream failure.

Inspect health and history:

```bash
curl http://127.0.0.1:8000/api/v1/market-data/stream
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/stream/gaps?open_only=true"
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/trades?symbol=AAPL&start=2026-10-30T12:00:00Z&end=2026-10-30T21:00:00Z&limit=50"
```

```bash
docker compose logs -f stream-worker
```

`health` meanings: `HEALTHY` persisted data within `MARKET_DATA_STREAM_STALE_AFTER`; `IDLE`
subscribed and quiet when quiet is expected (outside the regular session, or just subscribed);
`STALE` quiet during the regular session; `DEGRADED` partial subscription, dropped frames,
failing writes; `RECONNECTING`; `FAILED` permanent (fix the cause, then restart the worker);
`NOT_RUNNING` no heartbeat for 30 s. Sessions follow the NYSE calendar (holidays and early
closes, verified for 2025-2027).

Opt-in real-provider streaming smoke tests (throw-away database; data only arrives while IEX
trades, 08:00–17:00 New York):

```bash
ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=postgresql+psycopg://atlas:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/postgres uv run pytest -m live -s backend/app/tests/live/test_alpaca_stream_live.py
```

## Completeness monitoring and recovery (Deliverable 2D)

The recovery worker finds missing data and retrieves it from the provider's history. It is
off by default because it calls the provider.

1. Enable it in `.env`. The other `MARKET_DATA_RECOVERY_*` settings are documented in
   `.env.example`.

   ```text
   MARKET_DATA_RECOVERY_ENABLED=true
   MARKET_DATA_SYMBOLS=AAPL,SPY
   ```

2. Start it (Compose service `recovery-worker`, one per provider/feed):

   ```bash
   docker compose up -d --build recovery-worker
   ```

   To run a single cycle instead (an explicit operator action; it runs even when disabled):

   ```bash
   uv run python -m app.workers.market_data_recovery --once
   ```

3. Stop it with `docker compose stop recovery-worker`. Gaps and jobs live in PostgreSQL, so
   a restart resumes them; jobs a stopped worker left running run again.

Inspect it:

```bash
curl http://127.0.0.1:8000/api/v1/market-data/pipeline-health
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/completeness?symbol=AAPL&timeframe=1Min&start=2026-10-29T00:00:00Z&end=2026-10-30T00:00:00Z"
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/gaps?status=OPEN&status=UNAVAILABLE"
```

```bash
curl "http://127.0.0.1:8000/api/v1/market-data/recovery-jobs?status=FAILED"
```

```bash
curl http://127.0.0.1:8000/api/v1/market-data/metrics
```

Ask for a range to be verified. This records gaps for the parts without evidence; the worker
retrieves them:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/market-data/recovery-requests -H "Content-Type: application/json" -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN" -d '{"symbol":"AAPL","timeframe":"1Min","start":"2026-10-29T12:00:00Z","end":"2026-10-29T21:00:00Z"}'
```

Statuses:
* Gaps: `OPEN` (missing; waiting or retrying), `RECOVERING`, `RECOVERED` (the provider's
  history for the whole interval was retrieved), `PARTIALLY_RECOVERED` (the rest is a new
  gap), `UNAVAILABLE` (the provider refuses it; it stays missing).
* Jobs: `PENDING`, `RUNNING`, `COMPLETED`, `PARTIALLY_COMPLETED`, `FAILED`.
* Quote gaps stay `OPEN` unless `quotes` is in `MARKET_DATA_RECOVERY_DATA_TYPES`.

### Testing an outage and its recovery locally

During IEX hours (08:00-17:00 New York) with the stream worker running:

1. Kill the stream worker hard (no graceful shutdown):

   ```bash
   docker compose kill stream-worker
   ```

2. Wait a few minutes, then start it again. At start-up it records the interruption
   (`WORKER_NOT_RUNNING`), and the frames the dead worker received but never wrote
   (`DATA_NOT_PERSISTED`, exact count):

   ```bash
   docker compose up -d stream-worker
   ```

3. After `MARKET_DATA_RECOVERY_SETTLE_SECONDS` (default 2 minutes), run a cycle (or let the
   recovery worker do it):

   ```bash
   docker compose run --rm recovery-worker python -m app.workers.market_data_recovery --once
   ```

4. Check `GET /api/v1/market-data/gaps`:
   * bar and trade gaps for the interval are `RECOVERED`;
   * quote gaps are `OPEN` unless quotes are recovered.

   Then check that `GET /api/v1/market-data/completeness` reports the interval `COMPLETE`
   for bars.

The opt-in real-provider tests replay an outage over the previous trading day against
Alpaca's history (throw-away database):

```bash
ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=postgresql+psycopg://atlas:CHANGE_ME_LOCAL_ONLY@127.0.0.1:5432/postgres uv run pytest -m live -s backend/app/tests/live/test_alpaca_recovery_live.py
```

## Market-data dashboard (Deliverable 2E)

```bash
docker compose up -d --build backend frontend
```

* Dashboard: <http://127.0.0.1:5173> (Overview). Asset pages: <http://127.0.0.1:5173/markets/AAPL>.
* API documentation (OpenAPI): <http://127.0.0.1:8000/docs>.
* Rebuild the `frontend` image whenever `frontend/package.json` or `pnpm-lock.yaml` changes
  (2E added `lightweight-charts`); the container mounts `src/` and `public/` only.
* Without containers: `uv run python -m app`, then `cd frontend && pnpm dev`.

Live updates need the stream worker (`MARKET_DATA_STREAM_ENABLED=true`, off by default). The
dashboard never opens a provider connection: the API's hub reads what the worker stored and
pushes it over `GET /api/v1/market-data/live` (Server-Sent Events). Without a running worker
the pages show the last stored data, labelled with its age and freshness; the header and
Data Health show the stream as not running.

What to expect outside trading hours: freshness `Closed`, chart labelled "provider stream is
… : no new bars", IEX quotes often one-sided (shown as "no bid"/"no ask", never 0).

Troubleshooting:

| Symptom | Check |
|---|---|
| Header shows "API unreachable" | `docker compose ps`, `curl http://127.0.0.1:8000/ready` |
| Pages show "The database is unavailable." (HTTP 503) | PostgreSQL container health |
| Chart says "No stored … candles" | Request the range on the Ingestion page (or `historical-fetch`); 5Min/15Min/1Hour are not streamed |
| "Live updates reconnecting" | API restarted or proxy dropped the stream; the page reloads its data when it reconnects |
| Change shows "—" | No stored VALID daily bar for the previous session: retrieve 1Day history |

Tests: `cd frontend && pnpm test` (mocked API, labelled fixtures); backend
`backend/app/tests/integration/test_dashboard_api.py` (real PostgreSQL, SSE end to end, real
stream worker against the fake Alpaca server); opt-in real-provider check:
`ATLAS_LIVE_PROVIDER_TESTS=1 uv run pytest -m live -s backend/app/tests/live/test_dashboard_live.py`.

## Asset discovery and universe (Phase 3A)

Prerequisites: Alpaca **paper-account** keys in `.env` (the asset directory is read from
`paper-api.alpaca.markets`, GET `/v2/assets` only, D-050), the database at head.

Run from the repository root. Every command prints JSON on stdout and exits 0 only when every
job it ran finished `COMPLETED`.

Qualification measures **split-adjusted** daily bars (`ASSET_DISCOVERY_PRICE_ADJUSTMENT=SPLIT`,
D-063), a series separate from the RAW history other features use. After upgrading from the
RAW-based version, run `qualify` a few times: each run prepares adjusted history for at most
`ASSET_DISCOVERY_HISTORY_PREP_MAX_ASSETS` instruments, and the others stay `INSUFFICIENT_DATA`
until theirs is prepared.

1. First full catalog, then qualification, scoring and membership updates (~2 min for
   ~33,500 records; history is prepared for at most `ASSET_DISCOVERY_HISTORY_PREP_MAX_ASSETS`
   instruments per run):

   ```bash
   uv run python -m app.asset_discovery.cli discover --mode FULL
   ```

2. Later runs: an active-only listing (new listings and changes), or re-qualification
   without downloading the directory:

   ```bash
   uv run python -m app.asset_discovery.cli discover --mode INCREMENTAL
   ```

   ```bash
   uv run python -m app.asset_discovery.cli qualify
   ```

3. Counts and the newest job of each kind; refresh reference data for tracked assets:

   ```bash
   uv run python -m app.asset_discovery.cli status
   ```

   ```bash
   uv run python -m app.asset_discovery.cli refresh --max 50
   ```

4. Point in time: which instruments a layer held at a past time, as Atlas knew it then
   (`--known-at` defaults to `--at`). Layers: `DISCOVERY_POOL`, `QUALIFIED_UNIVERSE`,
   `ACTIVE_WATCHLIST`. A historical qualification (`qualify --as-of ...`) never changes
   membership.

   ```bash
   uv run python -m app.asset_discovery.cli members --layer QUALIFIED_UNIVERSE --at 2026-11-02T04:00:00Z
   ```

Through the API (operator token needed when `ATLAS_OPERATOR_TOKEN` is set; a repeated
request while a job of that kind is active returns the existing job with `"created": false`):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/discovery/run -H "Content-Type: application/json" -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN" -d '{"mode":"INCREMENTAL"}'
```

```bash
curl "http://127.0.0.1:8000/api/v1/discovery/jobs?limit=10"
```

Scheduled operation: set `ASSET_DISCOVERY_ENABLED=true` and start the worker (several may
run; jobs are claimed with `SKIP LOCKED`). Intervals: `ASSET_DISCOVERY_*_INTERVAL_HOURS`.

```bash
docker compose up -d --build discovery-worker
```

Qualification thresholds and ranking weights are versioned in the database, not in `.env`.
A change is a new version (audited): `POST /api/v1/discovery/config/rule-set` or
`/config/scoring`; the current versions are shown on the Asset Discovery page
(<http://127.0.0.1:5173/discovery>).

| Symptom | Check |
|---|---|
| Most assets `INSUFFICIENT DATA` | Expected after the first runs: history is prepared in bounded batches (D-061). Run `qualify` again or raise the bound |
| Job `FAILED` with an authentication error | The keys must be paper-account Trading API keys |
| Job stuck `RUNNING` | A crashed worker's lease expires after `ASSET_DISCOVERY_JOB_LEASE_SECONDS`; the job is then retried |

Opt-in real-provider test (throw-away database):
`ATLAS_LIVE_PROVIDER_TESTS=1 uv run pytest -m live -s backend/app/tests/live/test_discovery_live.py`.

## Market intelligence (Phase 3B)

Prerequisites: the database at head (migration 0007), Alpaca credentials in `.env`, the
benchmark (`MARKET_INTELLIGENCE_BENCHMARK_SYMBOL`, default SPY) registered, and a qualified
universe from Phase 3A for participation and correlation (at least 20 members).

1. A live run: re-retrieves the split-adjusted daily history window of the benchmark and
   the qualified universe (one read-only request each, at most
   `MARKET_INTELLIGENCE_HISTORY_PREP_MAX_ASSETS`), then measures and stores intelligence at
   now (basis RECORDED):

   ```bash
   uv run python -m app.market_intelligence.cli live
   ```

2. Reconstruct past decision times (basis RECONSTRUCTED; each session's close + settle),
   e.g. for the dashboard's regime history:

   ```bash
   uv run python -m app.market_intelligence.cli reconstruct --sessions 120
   ```

3. Print the newest recorded broad-market classifications, or as known at a time:

   ```bash
   uv run python -m app.market_intelligence.cli market --at 2026-11-03T22:00:00Z
   ```

The dashboard's **Market Intelligence** page starts the same runs (operator token outside
development). Only one run per provider and feed runs at a time (HTTP 409 otherwise); a run
whose process died is failed as interrupted after 5 minutes without a heartbeat. Results
are append-only; thresholds and windows are versioned configuration
(`GET /api/v1/intelligence/config`, decisions D-066 to D-071).

## News and events (Phase 3C)

Prerequisites: the database at head (migration 0008) and Alpaca credentials in `.env` (the
market-data keys; news is read-only). Settings: `NEWS_*` in `.env.example` (decisions D-072
to D-081). Article bodies are not stored unless `NEWS_STORE_CONTENT=true` (D-074).

1. Historical retrieval (all news, or `--symbols AAPL,MSFT`; `--start/--end` or `--days`).
   Re-running a range stores nothing twice; articles are labelled *retrieved later* and are
   available to simulations only from their retrieval time:

   ```bash
   uv run python -m app.news.cli backfill --days 3
   ```

2. The real-time worker (one connection per provider, separate from the stock stream). Set
   `NEWS_STREAM_ENABLED=true` in `.env`, then:

   ```bash
   docker compose up -d news-worker
   ```

   With the flag off it records `DISABLED` and exits 0. After every (re)connection it
   retrieves an overlapping window over REST (`STREAM_RECOVERY` jobs). Health:
   `GET /api/v1/news/stream` (`STREAMING`, `CONNECTING`, `DEGRADED`, `FAILED`,
   `NOT_RUNNING` after `NEWS_STREAM_STALE_AFTER` seconds without a heartbeat). Minutes
   without articles are normal; health comes from the heartbeat and subscription.

3. Verify credentials against the news endpoint:

   ```bash
   uv run python -m app.news.cli auth
   ```

4. After a change to the keyword rules (a new classifier version), add events under the new
   version for stored revisions (earlier events are kept; the new ones are available from
   now):

   ```bash
   uv run python -m app.news.cli reclassify
   ```

The dashboard's **News & Events** page shows the feed with filters, provider, stream and
economic-calendar status (no calendar provider is connected, D-077), category counts and a
historical-retrieval form (operator token outside development). Point-in-time reads:
`GET /api/v1/news/articles?known_at=...`.

## Strategy laboratory (Phase 4A)

Prerequisites: the database at head (migration 0009 or later). Settings: `STRATEGY_*` in
`.env.example` (decisions D-082 to D-089). Strategy parameters are not settings: they are
immutable versions in PostgreSQL.

Phase 4A ships six strategy families with interfaces, parameter schemas and feature
dependencies but no evaluation logic (Phase 4B): none is evaluated, none can be enabled, and
no proposals exist yet. Nothing here sizes, approves or submits a trade.

1. Register the catalog (feature definitions, strategies, version 1 of each). The API and
   the worker also do this on first use; it is idempotent:

   ```bash
   uv run python -m app.strategies.cli sync
   uv run python -m app.strategies.cli list
   ```

2. Inspect the point-in-time features a strategy would receive for an asset (computed,
   not stored; `--at` for a past cutoff, `--basis reconstructed` to use data retrieved
   later). Missing or unsettled periods show as `MISSING_INPUTS`, never as values:

   ```bash
   uv run python -m app.strategies.cli features --strategy mean_reversion --symbol SPY --at 2026-11-02T08:00:00Z
   ```

3. New parameters create a new immutable version (operator token outside development;
   unspecified parameters keep their current values):

   ```bash
   curl -X POST http://127.0.0.1:8000/api/v1/strategies/breakout/versions \
     -H 'Content-Type: application/json' -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN" \
     -d '{"parameters": {"channel_length": 55}, "reason": "longer range"}'
   ```

   `POST /api/v1/strategies/{id}/status` enables or disables a strategy; strategies without
   logic are refused (409).

4. The evaluation worker (proposals only). Set `STRATEGY_WORKER_ENABLED=true`, then:

   ```bash
   docker compose up -d strategy-worker
   ```

   Every `STRATEGY_WORKER_INTERVAL_SECONDS` it records proposal expiry, detects completed
   periods (a VALID stored daily bar for the newest settled session, per qualified-universe
   member) and news events, and evaluates enabled strategies with logic. Duplicates are
   skipped by idempotency key; failures are recorded per evaluation and retried up to
   `STRATEGY_MAX_ATTEMPTS`. With the flag off it exits 0.

The dashboard's **Strategy Laboratory** page shows the catalog, versions, parameters,
required features, status history, proposals and evaluation history (read-only; no
performance statistics exist).

## Strategies (Phase 4B)

Prerequisites: the database at head (migration 0010). Decisions D-090 to D-098.

The six families now have evaluation logic. After deploying, sync the catalog once (the API
and worker also do this): each family gets a new version whose configuration keeps the
stored parameters and adds the new fields at their defaults. Strategies stay **disabled**
until an operator enables them; nothing here sizes, approves or submits a trade.

```bash
uv run python -m app.strategies.cli sync
uv run python -m app.strategies.cli list
```

Enable one strategy (operator token outside development):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/strategies/momentum/status   -H 'Content-Type: application/json' -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN"   -d '{"enabled": true, "reason": "paper evaluation"}'
```

Evaluate a strategy at a past cutoff (stored as a HISTORICAL evaluation; the output includes
its signal state):

```bash
uv run python -m app.strategies.cli evaluate --strategy trend_following --symbol SPY --at 2026-11-02T08:00:00Z
```

Data the strategies need:

* **Momentum** reads relative strength against `benchmark_symbol` (default SPY), which must
  be an active Atlas asset with stored bars for the same sessions; otherwise its evaluations
  are UNAVAILABLE_DATA (set `require_relative_strength=false` to make it context only).
* **Event driven** needs news coverage (a completed backfill or a subscribed stream) over the
  last session; without it every evaluation is UNAVAILABLE_DATA. Macroeconomic events are
  not evaluated (no economic calendar).
* **SHORT** proposals need the provider reference data to report the asset shortable and
  easy to borrow; assets without reference data are INELIGIBLE_ASSET.

Inspect results: `GET /api/v1/strategies/evaluations?signal_state=NO_SIGNAL` (and the other
states), `GET /api/v1/strategies/proposals/{id}` (levels, evidence, conflicts) and
`GET /api/v1/strategies/conflicts`. The dashboard's Strategy Laboratory page shows the same.

## Machine learning (Phase 5A)

Prerequisites: the database at head (migration 0011), and the ML dependencies installed
(`uv sync`). Decisions D-099 to D-112.

Everything runs through the CLI from the repository root (it reads `.env` there). Nothing here
sizes, approves or submits a trade, and no model is ever promoted automatically.

**1. Build a dataset version** from a JSON specification. `config/ml/` holds the specifications
used for the Phase 5A real-data runs. The command prints the version id, counts and warnings:

```bash
uv run python -m app.ml_engine.cli build-dataset --spec config/ml/dev-direction-1d.json
```

Fix `data_known_at` in the specification: a rebuild of the same specification against the
same stored data then reproduces it exactly. Check that with:

```bash
uv run python -m app.ml_engine.cli verify-dataset --version <dataset-version-id>
```

**2. Train.** Either the defaults:

```bash
uv run python -m app.ml_engine.cli train --dataset-version <id> --model XGBOOST
```

or a JSON `TrainingSpec`, with hyperparameters or a grid of at most 12 candidates, splits,
CPCV, preprocessing and filters:

```bash
uv run python -m app.ml_engine.cli train --spec my-run.json
```

Every run registers a CANDIDATE model version and records:

* its periods;
* its walk-forward folds;
* its test metrics against baselines;
* calibration evidence;
* the evidence verdict (`validation_status`).

`test_period_reuse` counts earlier runs that already looked at the same test period. Treat a
growing count as erosion of that period's independence.

**3. Lifecycle.** Each step is an explicit, audited decision:

```bash
uv run python -m app.ml_engine.cli transition --model <id> --status VALIDATED --reason "..."
```

The API equivalent needs the operator token outside development:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ml/models/<id>/status -H 'Content-Type: application/json' -H "X-Atlas-Operator-Token: $ATLAS_OPERATOR_TOKEN" -d '{"status": "VALIDATED", "reason": "..."}'
```

* VALIDATED and APPROVED need a PASSED evidence verdict.
* APPROVED needs SHADOW first.
* A second APPROVED version of the same name is refused until the first is RETIRED.

**4. Predict** point in time (RECORDED inputs, recorded in `ml_predictions`):

```bash
uv run python -m app.ml_engine.cli predict --model <id> --symbol SPY --at 2026-10-30T22:00:00Z --mode SHADOW
```

`APPROVED` mode serves only the approved version, and `SHADOW` only shadow versions. Other
requests are recorded as REJECTED_MODEL_STATUS. Check a stored prediction with:

```bash
uv run python -m app.ml_engine.cli replay --prediction <id>
```

**Artifacts** are written to `ML_ARTIFACT_DIR` (default `data/ml/artifacts`, git-ignored) and
are loaded only if their sha256 matches the registry.

* The compose `backend` mounts the volume `ml-artifacts` there. Train inside the container
  (`docker compose run --rm backend python -m app.ml_engine.cli train ...`) when the API
  should serve the model.
* Artifacts trained on the host are not visible to the container: replay and inference
  report REJECTED_ARTIFACT for them.
* Back the directory or volume up together with the database: an artifact cannot be
  recreated bit for bit without the same library versions.

Inspect: the dashboard's **Machine Learning** page, or `GET /api/v1/ml/overview`, `/runs`,
`/runs/{id}`, `/models`, `/models/{id}`, `/compare?run_ids=...`, `/datasets` and `/predictions`.

## AI decision engine and LLM (Phase 5B)

Prerequisites: the database at head (migration 0012) and the strategy catalog synced
(`python -m app.strategies.cli sync`; the strategy worker does this on start). Decisions D-113
to D-123. Nothing here sizes, approves or submits a trade: a PRIORITIZE_FOR_REVIEW decision is
a candidate for the future risk engine (Phase 6).

Everything runs from the repository root (it reads `.env` there).

**Decision runs.** A LIVE run decides now from RECORDED information; it first asks the ML
engine for point-in-time predictions of APPROVED (used) and SHADOW (context only) models:

```bash
uv run python -m app.ai_engine.cli decide
```

A historical run at one decision time, strictly RECORDED (only rows stored by then) or
RECONSTRUCTED (rows stored later about periods completed by then; labelled):

```bash
uv run python -m app.ai_engine.cli decide --at 2026-10-30T21:00:00Z --basis RECORDED
```

Every session close plus the settle delay in a range (repeats return the existing run):

```bash
uv run python -m app.ai_engine.cli decide-range --start 2026-09-01T00:00:00Z --end 2026-10-30T23:59:00Z --basis RECONSTRUCTED
```

**Replay** re-evaluates a stored run from its stored inputs (no re-read, no LLM call) and
compares hashes; `result_identical: true` is the reproducibility check:

```bash
uv run python -m app.ai_engine.cli replay --run <run-id>
```

**Baselines.** Compare prioritized decisions with take-all and strongest-signal baselines on
realized net outcomes known now (stored in `decision_evaluations`):

```bash
uv run python -m app.ai_engine.cli evaluate --start 2026-08-01T00:00:00Z
```

**Policy.** `python -m app.ai_engine.cli policy` prints the current policy and its sha256.
Pass `--policy FILE` (before the command) or set `AI_DECISION_POLICY_FILE` to use a JSON
policy; a changed policy is registered as a new version on first use.

**LLM analysis** is off and costs nothing by default (`AI_LLM_PROVIDER=none`). To enable it, set
in `.env` (never commit a key):

```
AI_LLM_PROVIDER=anthropic            # or openai_compatible (+ AI_LLM_BASE_URL)
AI_LLM_MODEL=<exact model id>
AI_LLM_API_KEY=<key>
AI_LLM_INPUT_USD_PER_MTOK=<price>    # from the provider's price list
AI_LLM_OUTPUT_USD_PER_MTOK=<price>
AI_LLM_PRICING_SOURCE=<where the prices came from, and when>
```

Without both prices every call is refused (UNPRICED). Budgets: `AI_LLM_DAILY_BUDGET_USD`
(0.50), `AI_LLM_MONTHLY_BUDGET_USD` (5.00), `AI_LLM_MAX_REQUESTS_PER_DAY` (200). Then:

```bash
uv run python -m app.ai_engine.cli analyze-news
uv run python -m app.ai_engine.cli explain-conflicts
uv run python -m app.ai_engine.cli research --symbol SPY
uv run python -m app.ai_engine.cli usage
```

**Worker.** `python -m app.workers.ai_engine` (compose `ai-worker`) runs three independent
loops, each off unless enabled: `AI_DECISIONS_ENABLED` (a LIVE run every
`AI_DECISIONS_INTERVAL_SECONDS`), `AI_NEWS_ANALYSIS_ENABLED` and
`AI_CONFLICT_EXPLANATIONS_ENABLED`. With all off it exits 0. A slow or unavailable LLM never
delays a decision run.

**Real-provider smoke test** (one small request, a fraction of a cent at small-model prices):

```bash
ATLAS_LIVE_PROVIDER_TESTS=1 ATLAS_TEST_DATABASE_URL=... uv run pytest -m live backend/app/tests/live/test_llm_live.py
```

**API** (read-only): `/api/v1/ai/overview`, `/runs`, `/runs/{id}`, `/runs/{id}/replay`,
`/rankings`, `/decisions`, `/decisions/{id}`, `/decisions/{id}/evidence`,
`/decisions/{id}/models`, `/conflicts`, `/policies`, `/llm/analyses`, `/llm/requests`,
`/llm/usage`, `/evaluations`. Dashboard: **Central AI Activity** (`/ai`).

## Portfolio accounting (Phase 6A)

Prerequisites: the database at head (migration 0013). Decisions D-125 to D-137; conventions in
[`architecture/accounting.md`](architecture/accounting.md). Paper only: there is no broker
and no execution source. Run from the repository root (it reads `.env` there).

**Create the paper account** with its simulated opening funding (`ACCOUNTING_OPENING_FUNDING`,
default 100,000.00 USD), then reconcile. Re-running is a no-op; the funding is posted once and
is contributed capital, never a return:

```bash
uv run python -m app.accounting.cli init
```

In Docker Compose:

```bash
docker compose run --rm backend python -m app.accounting.cli init
```

**Valuation checkpoint** (posts the changes in valuation adjustments and FX translation from
fresh prices and rates, writes a snapshot, then reconciles). Stale or missing prices are
flagged in the snapshot, never substituted:

```bash
uv run python -m app.accounting.cli checkpoint
```

**Reconcile** (exit status 1 and a HIGH-severity accounting error when anything disagrees;
nothing is ever corrected automatically):

```bash
uv run python -m app.accounting.cli reconcile
```

**Overview** as JSON:

```bash
uv run python -m app.accounting.cli show
```

**Controlled test transactions** exercise the posting templates on a separate
CONTROLLED_TEST account; the primary paper account never receives them. Instruments need a
lot size first (registered explicitly, or derived from the provider reference data); FX
rates are recorded with their provenance (TEST_FIXTURE rates value test accounts only):

```bash
uv run python -m app.accounting.cli create-test-account --name "Controlled test"
```

```bash
uv run python -m app.accounting.cli instrument-spec --symbol AAPL --from-reference
```

```bash
uv run python -m app.accounting.cli fx-rate --base EUR --quote USD --rate 1.0850 --observed-at 2026-11-04T21:00:00Z --source TEST_FIXTURE --provenance "test fixture"
```

```bash
uv run python -m app.accounting.cli post --account "Controlled test" --template long_purchase --event-id buy-1 --params '{"asset_id": "<asset-id>", "quantity": "10", "price": "150.25", "commission": "1.00"}'
```

Templates: `long_purchase`, `long_sale`, `short_open`, `short_cover`, `fee`, `financing_cost`,
`accrual_settlement`, `fx_conversion`. An event ID posts once; repeating it returns the
original entry, and repeating it with different inputs is refused.

**Corrections** are reversal entries (the original stays):

```bash
uv run python -m app.accounting.cli reverse --account "Controlled test" --entry <entry-id> --reason "wrong price"
```

**Read-only API:** `/api/v1/portfolio/{accounts,overview,cash,holdings,pnl,snapshots}` and
`/api/v1/accounting/{chart,templates,journal,journal/{id},balances,reconciliation,reconciliation/{id},fx-rates}`
(`account_id` defaults to the primary account). Dashboard: **Portfolio & Accounting**
(`/portfolio`).

**Immutability:** every ledger table rejects UPDATE, DELETE and TRUNCATE. `currency_balances`
is a cache that reconciliation re-checks against the journal lines.

## Risk engine (Phase 6B)

Prerequisites: the database at head (migration 0014), the paper account (Phase 6A `init`)
and a passing, current reconciliation. Decisions D-138 to D-147; conventions in
[`architecture/risk.md`](architecture/risk.md). Paper only: an assessment executes,
reserves and submits nothing, and no breaker ever liquidates.

Operator actions need `--actor operator:<name>` and a `--reason`; each writes an immutable,
audited record. Strategies, models and LLM tasks cannot run them (refused by the service
and by the database).

**Create the risk configuration** (version 1: the documented starting values, which are
not optimal or recommended settings). Re-running is a no-op:

```bash
uv run python -m app.risk.cli init --actor operator:jacob --reason "initial starting values"
```

**Change limits or preferences** (writes the next version with the field-by-field diff):

```bash
uv run python -m app.risk.cli set --actor operator:jacob --reason "fewer simultaneous positions" --hard max_open_positions=6
```

**Classify an asset** (sector and correlation cluster used by the concentration limits;
unclassified assets share one UNCLASSIFIED bucket):

```bash
uv run python -m app.risk.cli classify --actor operator:jacob --symbol NVDA --sector SEMICONDUCTORS --cluster SEMICONDUCTORS --provenance "operator classification"
```

**Monitor** (marks the ledger to fresh prices in memory, fixes the period baselines, records
an exposure snapshot and trips breakers). Run it periodically, and at least once after each
regular close so the next day's start-of-period equity is observed at the boundary:

```bash
uv run python -m app.risk.cli monitor
```

**Assess a proposal** (manual levels, a stored strategy proposal or a prioritized AI
decision; `--strict` exits 1 unless it passes). A PASS_FOR_RESERVATION is not an execution
approval:

```bash
uv run python -m app.risk.cli assess --symbol AAPL --direction LONG --entry 150.25 --stop 145.00
```

```bash
uv run python -m app.risk.cli assess --decision-id <ai-decision-id>
```

**Replay** a stored assessment from its stored inputs (exit 1 if it does not reproduce):

```bash
uv run python -m app.risk.cli replay --assessment <assessment-id>
```

**Kill switch and breakers.** Engaging is also possible from the dashboard (operator token);
releasing and resetting are CLI-only. A reset is refused while the breaker's condition is
still breached (for a daily loss: until the next trading day's loss is below the limit);
DATA_QUALITY recovers automatically, recorded, once every price is fresh again:

```bash
uv run python -m app.risk.cli kill-switch engage --actor operator:jacob --reason "manual halt"
```

```bash
uv run python -m app.risk.cli kill-switch release --actor operator:jacob --reason "reviewed, resuming"
```

```bash
uv run python -m app.risk.cli reset --breaker DAILY_LOSS --actor operator:jacob --reason "loss reviewed"
```

**State** as JSON (live figures are computed, not recorded):

```bash
uv run python -m app.risk.cli show
```

**API:** `GET /api/v1/risk/{state,limits,exposure,exposure/snapshots,assessments,
assessments/{id},circuit-breakers,circuit-breakers/events,configurations,events}` and
`POST /api/v1/risk/kill-switch/engage` (operator token). Dashboard: **Risk Center** (`/risk`).

**Immutability:** every risk table rejects UPDATE, DELETE and TRUNCATE; the database refuses
a passing assessment while a breaker is tripped, against an outdated configuration, above the
configured risk per trade, or whose stored result does not follow from its stored checks.

## Risk reservations (Phase 6C)

Prerequisites: the database at head (migration 0015), the Phase 6B configuration and a
passing, current accounting reconciliation. Decisions D-148 to D-155; design in
[`architecture/reservations.md`](architecture/reservations.md). A reservation holds
capacity for an approved intent; it is not an order and posts nothing to the ledger.

**Reserve** (the assessment is recomputed inside the reservation transaction; the same
`--idempotency-key` returns the original result; `--strict` exits 1 unless approved):

```bash
uv run python -m app.risk.cli reserve --symbol AAPL --direction LONG --entry 150.25 --stop 145.00 --idempotency-key operator:aapl:2026-11-06-1
```

Strategy proposals and AI decisions: `--proposal-id ID` or `--decision-id ID` instead of
the levels (their identity, expiry and supersession are checked; one live reservation per
intent).

**Inspect:** `reservations list [--active]`, `reservations show --id ID`,
`reservations capacity` (reserved and available, live), `reservations events`.

**Expire due reservations** (only those with no attached order and nothing consumed; others
are held and reported as held past expiry). Run it on a schedule, e.g. every minute:

```bash
uv run python -m app.risk.cli reservations expire
```

**Reconcile** (exit 1 when unexplained inconsistencies are found; a FAILED run writes a
CRITICAL `system_health_events` row and blocks new approvals until a later run passes):

```bash
uv run python -m app.risk.cli reservations reconcile
```

**After a crash or restart:** `reservations recover` (expiry per the rules, then a
reconciliation). Nothing is released because a worker timed out: reservations with an
attached order or an uncertain execution state are held until the execution engine (Phase
7) or an operator resolves them:

```bash
uv run python -m app.risk.cli reservations release --id ID --resolution OPERATOR --key operator:release:ID --actor operator:jacob --reason "broker confirms no order"
```

A reconciliation that fails because of a real inconsistency needs investigation: the
reservation tables are append-only (the projection changes only through its events), so
the finding stays in the history. Consumption is not available from the CLI: it requires
a ledger fill (Phase 7).

## Paper orders (Phase 7A)

Prerequisites: the database at head (migration 0016). Decisions D-156 to D-163; design in
[`architecture/execution.md`](architecture/execution.md). **Every order is simulated** and
handled by the isolated paper broker; nothing reaches a brokerage. Phase 7A has no fill
model: fills are controlled test events on CONTROLLED_TEST accounts only.

**Run the worker** (paper broker start + recovery, then outbox delivery, expiry and a
heartbeat every second; SIGTERM stops it cleanly; several may run at once):

```bash
uv run python -m app.workers.paper_execution
```

Without the worker, run the jobs by hand: `dispatch` (deliver outbox events), `expire`
(expire due orders), `recover` (after a crash: broker start, catch-up of lost deliveries,
resolution of dead submissions and cancellations, expiry):

```bash
uv run python -m app.execution.cli recover
```

**Inspect:** `orders list [--group open|closed|filled|cancelled|rejected|expired|failed]`,
`orders show --id ID`, `orders events`, `orders requests` (refused submissions),
`broker status`; `reconcile [--account NAME]` (exit 1 on an unexplained finding, which also
writes a CRITICAL `system_health_events` row).

**Operator actions** (`--actor operator:<name>`; idempotency keys of 8 to 160 characters):

```bash
uv run python -m app.execution.cli submit --reservation-id ID --type LIMIT --quantity 92 --limit 99.60 --key operator:aapl:order-1 --actor operator:jacob
```

```bash
uv run python -m app.execution.cli cancel --id ORDER_ID --key operator:cancel:1 --actor operator:jacob --reason "no longer wanted"
```

`exit --parent-id ID ...` places an EXIT order against an ENTRY order's filled position.
`test-fill --id ID --quantity Q --price P --ref REF --actor operator:NAME` records a
CONTROLLED TEST fill (refused on the primary account). `broker start|pause|stop` changes
the simulator's run state.

**When something is stuck:** an order CANCEL_PENDING with EXECUTION_SAFETY tripped means a
cancellation could not be confirmed; its reservation is held (uncertain). Fix the cause
(e.g. `broker start`), run `recover`, then reset the breaker with the risk CLI. Order
history is append-only: nothing is edited.

## Simulated execution and positions (Phase 7B)

Prerequisites: the database at head (migration 0017). Decisions D-164 to D-172; design in
[`architecture/execution.md`](architecture/execution.md). **Fills are simulated** by the
isolated paper broker from stored market data under the recorded execution model; nothing
reaches a brokerage.

**Workers** (both safe to run several times; SIGTERM stops them cleanly):

```bash
uv run python -m app.workers.paper_execution
```

```bash
uv run python -m app.workers.position_monitor
```

The first delivers the outbox and simulates working orders (and expires them); the second
marks positions, enforces their protection, keeps take-profits resting, accrues short borrow
and takes 6A valuation checkpoints. Both are in `docker-compose.yml`. Without them, run
single passes: `simulate`, `monitor`, `dispatch`.

**Inspect:** `positions list [--status open|closed]`, `positions show --id ID`,
`positions summary`, `analytics` (simulated slippage, fill rates, costs, assumptions),
`models` (execution-model versions); `orders show --id ID` now includes the simulator's
evaluations and fill breakdowns.

**Operator actions** (`--actor operator:<name>`):

```bash
uv run python -m app.execution.cli close --position-id ID --key operator:close:1 --actor operator:jacob --reason "flatten"
```

```bash
uv run python -m app.execution.cli protect --position-id ID --stop 97.00 --actor operator:jacob --reason "lock in gains"
```

`protect` also takes `--trail-amount A` or `--trail-percent P`. A stop looser than the
current one is refused (the reservation was sized with the approved stop).

**Changing assumptions:** the execution model is code (`app/paper_broker/execution_model.py`);
give a changed model a new `version`. Orders already working keep the version they started
with; new orders use the new one. Every version stays in `execution_models`.

* Version 2 (D-217) added the gap rule.
* Version 3 (D-237, the current default) fills a limit at its first post-arrival
  observation, plus costs, never worse than the limit. It applies when that observation
  (a trade, or a 1-minute bar's open) is already through the limit and no fresh quote
  executed the order. Before version 3, such a fill was at the limit.

**When nothing fills:** look at `orders show --id ID` -> `execution.attempts`. DEFERRED codes
(`MARKET_CLOSED`, `STALE_QUOTE`, `NO_QUOTE`, `LIQUIDITY_UNKNOWN`, `VOLUME_UNKNOWN`) mean the
data was insufficient; NO_FILL codes (`LIMIT_NOT_THROUGH`, `WAITING_FOR_QUOTE`,
`PROTECTION_EXCEEDED`, `STOP_NOT_TRIGGERED`, `RISK_HALTED`) mean it was sufficient and the
order did not execute. A position with `monitor_status = STALE` has no fresh price: its
protection waits for one (no exit at a stale or invented price).

## Backtesting (Phase 8A)

Prerequisites: the database at head (migration 0018), the strategy catalog synced
(`python -m app.strategies.cli sync`), RAW **and** SPLIT daily bars for the symbols (the
default point-in-time split adjustment needs both), and an instrument specification (lot size)
per symbol. Lot sizes are never guessed: register one with
`python -m app.accounting.cli instrument-spec --symbol S --increment 1` or `--from-reference`.
Decisions D-173 to D-184; design in [`architecture/backtesting.md`](architecture/backtesting.md).
**Backtests are simulations over stored historical data**: nothing is ordered, filled or
posted to the paper account.

**Run from the dashboard:** `/backtesting`. Choose dates, capital, strategies, timeframes,
directions, universe, entry and stop order types, benchmark and seed, then submit with the
operator token if the server sets one. The run executes on the API process's backtest
thread; its page shows progress, then the results. Select two or more completed runs to
compare them. **Reproduce** on a run's page re-executes it from its stored configuration and
reports whether the results are identical.

**Run from the CLI** (the request is the API body as JSON):

```bash
uv run python -m app.backtesting.cli run --config request.json
```

```bash
uv run python -m app.backtesting.cli show RUN_ID
```

```bash
uv run python -m app.backtesting.cli reproduce RUN_ID
```

`list` shows recent runs; `fail-stale` fails runs whose process ended mid-replay (the API
does this at start-up and re-queues runs that never started).

**Request fields** (all optional except name, dates, universe and strategies): `risk.hard_limits`
and `risk.preferences` override the stored 6B configuration for this run only (recorded, and
the run's risk source says `+BACKTEST_OVERRIDES`); `execution.equity_overrides` overrides 7B
cost and participation assumptions (a new content-addressed execution model);
`data.data_known_at` fixes the dataset's knowledge time; `data.news_delay_minutes`,
`data.settle_seconds` and `data.feature_adjustment` (`POINT_IN_TIME_SPLIT` | `RAW`) set the
availability policies; `breaker_reset_policy` is `NEXT_SESSION_IF_CLEAR` or `NEVER`.

**Engine 8A.2 (D-237).** New runs use the corrected execution conventions
(`execution_conventions: "daily-v2"`, default) and credit cash dividends (`dividends: true`,
default).
* Pass `"execution_conventions": "daily-v1"` for the Phase 8A conventions, a pessimistic
  comparison.
* Runs stored before 8A.2 replay and reproduce under daily-v1 without dividends.
* Dividends need migration 0023 and a retrieval covering the run's dates (GET-only):

```bash
uv run python -m app.backtesting.dividends --symbols SYMBOLS --start 2019-01-01 --end 2026-11-10
```

A symbol without a covering retrieval is reported as `NOT_RETRIEVED` in the run summary and is
credited nothing. Retrieval rows are append-only; a changed provider value becomes a new
revision, and a run uses only rows retrieved by its knowledge time.

**When a run trades little:** look at the run's rejection reasons. `SECTOR_EXPOSURE` and
`CORRELATED_EXPOSURE` bind when assets have no sector classification
(`risk_asset_classifications`) or correlation measurements for past dates: the 6B policies
pool them (as they would live). `LOT_SIZE_MISSING` means no instrument specification;
`MARGIN_DATA_MISSING` means a short without a provider margin requirement;
`ENTRY_EXPIRED` means the limit entry was never traded through before the proposal expired.
Strategy evaluations by state show missing data (`INSUFFICIENT_DATA`, `UNAVAILABLE_DATA`:
for example news without retrieval coverage).

**A run fails** (and stores no partial results) when a posting is refused or the books do not
reconcile, which would be a bug, or when the configuration cannot be replayed (a stored
strategy version whose logic differs from the code). The error is on the run's page and in
`backtest_run_errors`.

## Research data foundation (D-239)

**Simulation database only.** The CLI refuses a database whose name lacks `sim`. Requests to
the provider and to the SEC are GET-only, and credentials are never printed. The tables need
migrations 0024 and 0025 (`research_exclusions`). Both are applied to `atlas_sim_profitability`
only; the main database stays at 0022. Run the steps in this order:

```bash
uv run python -m app.research.cli build-universe      # security master, formations, universes
```

```bash
uv run python -m app.research.cli correct-symbology   # reused tickers (traded months), fund collisions
```

```bash
uv run python -m app.research.cli form-current        # the current universes (point-in-time identity)
```

```bash
uv run python -m app.research.cli load-bars           # 2016-2019 SIP daily bars (RAW, SPLIT)
```

```bash
uv run python -m app.research.cli infer-delistings
```

```bash
uv run python -m app.research.cli dividends
```

```bash
uv run python -m app.research.cli lots-missing        # lot evidence + registration for new members
```

```bash
uv run python -m app.research.cli classify            # SIC as filed -> Fama-French 12 sectors
```

```bash
uv run python -m app.research.cli validate            # writes runs/research_validation[.N].json
```

```bash
uv run python -m app.research.cli reserve             # reservations with the dataset hash
```

**Reserved data.** A backtest that overlaps 2016-2019 (`V1_2016_2019`), or that trades a member
of a reserved research universe, is refused unless its request names a registered
pre-registration (`"preregistration": "CODE"`) that lists those reservations. Register one
from its final document; this starts no experiment:

```bash
uv run python -m app.research.cli preregister CODE docs/phase-reports/DOC.md V1_2016_2019
```

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

```bash
uv run python -m app.research.cli exclusion-evidence research_validation.5.json research_bar_manifest.5.json
```

```bash
uv run python -m app.research.cli register-exclusions docs/phase-reports/x1-phantom-split-exclusions-preregistration.md config/simulation/runs/research_exclusions_x1_evidence.json
```

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

**Corporate-action audit (D-241, read only).** `app.research.ca_audit` scans the current
universes for corporate-action defects. It writes only JSON artifacts, never the database.
It refuses to overwrite an output, and it records every provider look-up (GET only) so a
rerun with an existing look-up file is offline. Run from `backend/` in the simulation
environment:

```bash
uv run python -m app.research.ca_audit ../config/simulation/runs/ca_audit_N.json ../config/simulation/runs/ca_audit_provider_actions_1.json ../config/simulation/runs/ca_audit_provider_lookups_N.json
```

`reconcile OUT LOOKUPS` and `identity OUT LOOKUPS` (as the first arguments) price candidate
corrections and identity probes. Findings:
`docs/phase-reports/corporate-action-integrity-audit.md`.

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

```bash
uv run python -m app.research.cli x2-evidence
```

```bash
uv run python -m app.research.cli register-x2 docs/phase-reports/x2-corporate-action-exclusions-preregistration.md config/simulation/runs/research_exclusions_x2_evidence.json
```

Registration refuses if X2 exists already, if a file changed after it was hashed, or if an
event's audited RAW closes are no longer in the database. In a research run the replay closes
a position at the close before the contaminated session (exit reason `CA_EXCLUSION`). It also
keeps the asset out of every decision whose read window contains that session, and keeps a
splice inside a span out through the span's end. The run records the policy, the read
window, every set's content hash and its windows under `config.exclusions`. A replay whose
registered sets differ from the recorded ones refuses to start.

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

```bash
uv run python -m app.research.cli x3-evidence --lookups ca_dividend_lookups_2.json
```

```bash
uv run python -m app.research.cli register-x3 docs/phase-reports/x3-dividend-identity-corrections-preregistration.md config/simulation/runs/ca_dividend_corrections_evidence.2.json
```

A run applies a set only when it names it: `"corporate_actions": "CA_DIVIDEND_IDENTITY_V1"`,
with dividends modelled. The configuration records the code, content hash, policy and
pre-registration, and the replay refuses a set whose recomputed hash differs. The run's
`dividends.corrections` summary counts credited events, voided rows, skipped duplicates and
not-retrieved assets credited through corrections. X1 and X2 stay in force; a correction never
releases an exclusion.

**Dividend completeness, calendar and identity X3.1 (D-244).**

* **Retrieval.** The provider filters corporate actions on the *processing* date. Dividend
  retrievals (`python -m app.backtesting.dividends --symbols ... --start ... --end ...`) now
  take an **ex-date** range: they query the processing-date window `[start − 31 d,
  end + 366 d]` and record `details.basis = "ex_date"`.
* **Dividend policy v2.** New runs use `cash-at-ex-date-v2`, which credits only from
  ex-date-basis retrievals. To cover a symbol, retrieve it again. Stored runs keep v1.
* **Calendar.** It now carries the unscheduled NYSE closures since 2001. It matches the
  provider's calendar day by day from 2004. A replay stops (`data` stage) if the loaded bars
  contradict the calendar.
* **Commands, in order.** Each run is GET only; artifacts are never replaced; the evidence is
  built offline once the look-ups exist.

```bash
uv run python -m app.research.cli x31-calendar
```

```bash
uv run python -m app.research.cli x31-retrieve
```

```bash
uv run python -m app.research.cli x31-evidence --lookups x31_dividend_lookups_1.json
```

```bash
uv run python -m app.research.cli x31-table config/simulation/runs/x31_dividend_completeness_evidence.json docs/phase-reports/x31-dividend-completeness-by-asset.md
```

```bash
uv run python -m app.research.cli register-x31 docs/phase-reports/x31-dividend-completeness-preregistration.md config/simulation/runs/x31_dividend_completeness_evidence.json
```

`x31-retrieve` appends provider rows to the simulation database; it never changes stored rows.
The registered set `CA_DIVIDEND_IDENTITY_V2` holds every V1 member plus the X3.1 records. A
research run on V4 should name it (`"corporate_actions": "CA_DIVIDEND_IDENTITY_V2"`, with
`"dividends": true`).

**Historical universe evidence X4.1 (D-245).** This is evidence for rebuilding the 2016-2018
universes without today's listings. It lives in `config/simulation/evidence/x4`, where every
artifact is listed in `manifest.json` with its source, parameters, retrieval time and SHA-256.
Capture is GET only, write-once and resumable, and steps on different hosts may run in
parallel. No database row is written.

```bash
uv run python -m app.research.cli x4-capture assets
```

The other steps, run the same way: `snapshot`, `archive`, `sec`, `sweep` (optionally
`--formation` and `--length`), `classes`, `history`, `dividends`, `derive`. Verify offline:

```bash
uv run python -m app.research.cli x4-verify
```

`x4-verify` recomputes every hash and re-derives every report from the raw evidence. A derived
report is never overwritten: a changed derivation must be written as a new artifact.

**Security lifecycle ledger X4.2 (D-246).** Migration 0028 adds the append-only lifecycle
tables. It is applied only to the disposable copy `atlas_sim_x42_rehearsal`; the simulation
database stays at 0027. Build the ledger from the X4.1 evidence (a file; deterministic, never
replaced):

```bash
uv run python -m app.research.cli x42-build
```

Register it once into a database at 0028; the command refuses any other version:

```bash
uv run python -m app.research.cli x42-register config/simulation/lifecycle/x42_ledger_v1.json
```

The 0028 downgrade refuses while the ledger holds rows.

**Historical entity resolution X4.3 (D-247).** X4.3 evidence lives in its own write-once store,
`config/simulation/evidence/x43`. The identity snapshot is read-only, in one read-only
transaction, against the database that `ATLAS_POSTGRES_DB` selects (the simulation database).
The probes are GET only:

```bash
uv run python -m app.research.cli x43-capture identities
uv run python -m app.research.cli x43-capture asof
```

Build the identity graph from X4.1, the X4.2 ledger (checked against its SHA-256) and X4.3.
The result is a file, written once:

```bash
uv run python -m app.research.cli x43-build
```

`x43-verify` checks the X4.3 hashes, rebuilds the graph and compares content hashes:

```bash
uv run python -m app.research.cli x43-verify
```

Nothing is written to a database.

**Lifecycle ledger V2 X4.3.1 (D-248).** V2 is built from V1, the X4.3 graph and the stored
evidence; each input is checked against its SHA-256. It is a file, written once:

```bash
uv run python -m app.research.cli x431-build
```

`x431-verify` recomputes V2's hashes and rebuilds it offline:

```bash
uv run python -m app.research.cli x431-verify
```

Register V2 beside V1 only into a database at 0028; the command refuses any other version. It
has been rehearsed only on the disposable copy `atlas_sim_x431_rehearsal`:

```bash
uv run python -m app.research.cli x431-register config/simulation/lifecycle/x431_ledger_v2.json.gz
```

**Identity graph V2 X4.3.2 (D-249).** The provider's corporate actions go into the X4.3.2 store
(GET only; written once). Graph V2 is built from X4.1, X4.3, X4.3.2, the X4.3 graph and
Ledger V2, each checked against its SHA-256. It is a file, written once, and nothing is written
to a database:

```bash
uv run python -m app.research.cli x432-capture
uv run python -m app.research.cli x432-build
```

`x432-verify` checks the X4.3.2 hashes, rebuilds graph V2 and compares content hashes:

```bash
uv run python -m app.research.cli x432-verify
```

**Historical universe X4.4 (D-250).** This builds the point-in-time equity and ETF universes from
the stored evidence, Ledger V2 and graph V2 (each input checked against its SHA-256). It is a
file, written once, and nothing is written to a database:

```bash
uv run python -m app.research.cli x44-build
```

`x44-verify` recomputes the hash, rebuilds the universe and compares:

```bash
uv run python -m app.research.cli x44-verify
```

**Member data X4.5 (D-251).** This retrieves the market data of the confirmed X4.4 members. It
needs the simulation environment with provider credentials from `.env`; the values are never
printed. Capture writes to the write-once store `config/simulation/evidence/x45`, and an
artifact already captured is not fetched again. Run the bars first, then the corporate actions,
then the recoveries, which are worked out from the captured bars:

```bash
uv run python -m app.research.cli x45-capture bars
uv run python -m app.research.cli x45-capture actions
uv run python -m app.research.cli x45-capture recovery
```

The build is offline. It checks the universe, Ledger V2, graph V2 and the store manifest against
their SHA-256 and writes `config/simulation/lifecycle/x45_member_data_v1.json.gz` once; it
refuses to overwrite. Nothing is written to a database:

```bash
uv run python -m app.research.cli x45-build
```

`x45-verify` checks every evidence hash, rebuilds the dataset and compares content hashes:

```bash
uv run python -m app.research.cli x45-verify
```

**Research-data validation X4.6 (D-252).** This runs offline. It first checks every earlier X4
artifact against its reported SHA-256 and refuses to run if any differs. It then writes
`config/simulation/lifecycle/x46_validation_v1.json.gz`, which holds the status, restrictions and
usable range of every membership slot, and the derived table
`docs/phase-reports/x46-slot-status.csv`. Both are written once. Nothing is written to a
database:

```bash
uv run python -m app.research.cli x46-build
```

`x46-verify` rechecks the pinned hashes, rebuilds the validation and compares content hashes:

```bash
uv run python -m app.research.cli x46-verify
```

Research code reads member data by slot (`x46_validation.Lookup`), only inside the slot's
`usable_range`, and skips its `excluded_sessions`.

**Corrections X4.6.1 (D-253).** These steps write revisions and never change an earlier
artifact. Captures are GET only (SEC filings; provider bars and actions of admitted members)
into `config/simulation/evidence/x461`:

```bash
uv run python -m app.research.cli x461-capture filings
uv run python -m app.research.cli x461-capture bars
uv run python -m app.research.cli x461-capture actions
```

`x461-build` writes, once each:
* `x44_historical_universe_v2.json.gz`;
* `x45_member_data_v2.json.gz`, with terminal values;
* `x46_validation_v2.json.gz`, with `docs/phase-reports/x461-slot-status.csv`.

```bash
uv run python -m app.research.cli x461-build
```

`x461-verify` checks the X4.6.1 evidence hashes and every pin, then rebuilds all three revisions
and compares content hashes:

```bash
uv run python -m app.research.cli x461-verify
```

Research uses the V2 files and the V2 validation from here on. V1 remains for audit.

**Corporate actions and total returns X5 (D-254).** These steps read the V2 files, pinned by
SHA-256, and never change an earlier artifact. Captures are GET only (EDGAR filings; raw SIP
bars of securities received in exits or distributed in spin-offs) into
`config/simulation/evidence/x5`:

```bash
uv run python -m app.research.cli x5-capture filings
uv run python -m app.research.cli x5-capture prices
```

`x5-build` writes, once each:
* `x5_corporate_actions_v1.json.gz`: the ledger, terminal values and spin-off review;
* `x5_total_returns_v1.json.gz`: daily total-return factors and verification.

```bash
uv run python -m app.research.cli x5-build
```

`x5-verify` checks the evidence hashes and every pin, then rebuilds both artifacts and compares
content hashes:

```bash
uv run python -m app.research.cli x5-verify
```

Rules for research that uses these files:
* Use a slot's total-return factors only inside its usable range.
* A break (`MISSING_OR_EXCLUDED` or `RESTRICTED_*`) is never bridged.
* An exit adds value only through its `terminal_step`, which exists for `VALUED` and
  `VALUED_DEFAULT_NO_ELECTION` only.
* Dividends and the cash part of an exit are receivables, never spendable cash: the evidence
  has no pay dates.

**Execution realism and portfolio accounting X6 (D-255).** X6 is a library for later
backtests (`research.x6_data`, `x6_execution`, `x6_portfolio`). It runs no backtest and places
no order, and it writes nothing to a database. It reads the final X5 artifacts and the X4.6.1
V2 files, and refuses to start if any file's SHA-256 differs from its pin.

`x6-build` writes `x6_execution_inputs_v2.json.gz` once (V1, `aeda44c1…`, predates the exact-share fix of D-256 and stays for audit). It holds:
* the pins and the configuration's hash and labelled assumptions;
* every slot's formation step;
* the exit and spin-off treatments;
* the ledger's reconciliation with X5's factors;
* worked executions.

```bash
uv run python -m app.research.cli x6-build
```

`x6-verify` checks every pin, rebuilds the artifact and compares content hashes:

```bash
uv run python -m app.research.cli x6-verify
```

Rules for research that uses X6:
* Decide at a session's close with data up to it; the execution is in the next session
  (`X6Config.timing`).
* **Shorts:** every short is rejected unless `X6Config.borrow_book` holds a sourced borrow
  entry for both sessions. There is no historical borrow evidence.
* **Fees not modelled unless configured:** regulatory fees, withholding, taxes and interest on
  cash. Each needs a configured value with its source.
* **Frozen positions:** a snapshot with `equity_complete: false` has positions held at their
  last valid mark (a break, a restricted spin-off, an unvalued exit, or a window end without
  data). Treat its equity as incomplete, not as a value.
* **Configuration changes:** a changed configuration has a different `X6Config.sha256()`.
  Record it with any result.

**Historical backtest X7 (D-256).** **This is a hypothetical, long-only simulation. It places
no order, runs no paper or live trading, and writes nothing to a database.** Every input is
checked against its SHA-256 before a build:
* the X4.6.1 V2 and X5 artifacts;
* `x6_execution_inputs_v2`;
* the registered strategy capture;
* the spread measurements.

1. **Spread capture (once).** `x7-capture-spreads` reads the measured 2017–2019 SIP quoted
   spreads from the simulation database (SELECT only) into
   `config/simulation/evidence/x7/`. It is already done; the file is pinned.

   ```bash
   uv run python -m app.research.cli x7-capture-spreads
   ```

2. **Signals.** `x7-signals` evaluates the registered strategies at every member decision
   session and writes `x7_signals_v1.json.gz` once (about 16 minutes with 6 workers).

   ```bash
   uv run python -m app.research.cli x7-signals --workers 6
   ```

3. **Backtest.** `x7-backtest` replays the signals through the X6 engine: 6 strategy sets,
   4 accounts and 7 execution scenarios. It writes `x7_backtest_results_v1.json.gz` once
   (about 8 minutes).

   ```bash
   uv run python -m app.research.cli x7-backtest
   ```

4. **Verify.** `x7-verify` rebuilds both from scratch and compares content hashes and every
   ledger reconciliation (about 25 minutes).

   ```bash
   uv run python -m app.research.cli x7-verify
   ```

Rules for reading the results:
* A run with `equity_complete: false` has a headline over its complete prefix only. Its
  incomplete period and frozen positions are listed separately.
* `costs_off` is a diagnostic, not a result.
* Sharpe uses a zero risk-free rate.
* The 2017–2019 period is now seen data for the registered strategies. Confirming any of them
  needs data outside it.

**X7 V2 (D-257): implemented, not yet run.** V2 implements the frozen correction specification.
V1 is unchanged and still verifies with `x7-verify`.

1. **One-time evidence** (done, pinned):
   * the fund classification (offline, from pinned names);
   * spread evidence V2 (SELECT only, identity-checked).

   ```bash
   uv run python -m app.research.cli x7v2-classify-funds
   uv run python -m app.research.cli x7v2-capture-spreads
   ```

2. **The V2 runs** (refused unless the spec, the pre-registration and the evaluator match
   `config/simulation/x7/v2_freeze.json`, and, since D-258, unless the fund classification,
   spread evidence V2 and their two builder sources match its `pre_run_inputs` entries, and,
   since D-259, unless `x7v2_pipeline.py` matches both its manifest entry and the anchor in
   `x7v2_gate.py`, which the CLI checks before importing the pipeline):

   ```bash
   uv run python -m app.research.cli x7v2-backtest
   uv run python -m app.research.cli x7v2-verify
   ```

3. **The frozen decision rule,** on the stored V2 results only:

   ```bash
   uv run python -m app.research.cli x7v2-evaluate
   ```

**Not built:** the `include_leveraged` sensitivity, which needs an X6 hook proposed
separately.

**Docker:** starting Docker Desktop restarts the `atlas` compose stack (API, frontend,
Postgres, Redis). The API container runs no trading worker, and its startup does not migrate.

**Main database and migrations 0023 to 0027.** The main `atlas` database is at 0022; the
simulation database is at 0027. 0023 to 0025 and 0027 only add tables; 0026 alters only the
table 0025 adds. A rehearsal on a copy (`atlas_migcheck_0026`, made with `pg_dump` from
`atlas` on 2026-11-17) ran 0022 to 0026, then 0027 on 2026-11-18. Both ran cleanly,
`alembic check` reported no drift, and every pre-existing table kept its exact row count. X3.1
(D-244) adds no migration. Two
cautions:

* `docker compose up --build` runs `migrate` (`alembic upgrade head`) first, so it **will**
  apply 0023 to 0027 to the main database.
* It also rebuilds the paper-trading backend from the working tree, uncommitted changes
  included (engine 8A.2, `paper_broker` fill and execution model changes).

Decide on both before rebuilding the paper stack.

Each step only adds rows (members without bars, securities without a recorded delisting,
assets without a classification from that filing), so a rerun is safe. Artifacts in
`config/simulation/runs/` are never replaced: a rerun writes `name.2.json`, `name.3.json`...
(`build-universe` is the first-time step; a changed rule needs a new universe code.)

* `"universe": {"mode": "RESEARCH", "research_universe": "RESEARCH_US_EQUITY_V4"}` (current:
  equity V4 and ETF candidates V5, both with point-in-time identity; earlier versions are
  kept, superseded and still reserved).
* Excluded assets (D-240, `research_exclusions` scope `ASSET`) are never part of a research run.
  An explicit or layer run on reserved data that names one is refused.
* Date-bounded exclusions (D-242, scope `WINDOW`) work as follows:
  * A research run closes the position before the contaminated session and leaves the asset
    out of decisions while its read window contains it.
  * An explicit or layer run on reserved data whose dates overlap a blocked range is refused.
  * The run's `exclusions` key records the sets (code → content sha256), the read window and
    the windows.
* `"corporate_actions": "CODE"` (D-243) applies a registered correction set. The set is
  pinned by its content hash and needs `"dividends": true`.
* Delisting exits apply to research universes.
* `"correlation_source": "replay-bars-v1"` makes the risk engine's correlations
  point-in-time, computed from the replay's own bars.
* Every new key is omitted from the stored configuration when absent.

## Validation and governance (Phase 8B)

Prerequisites: everything backtesting needs (Phase 8A above), and the database at migration
0019. Design: [`architecture/validation.md`](architecture/validation.md); decisions D-185 to
D-199. **Validation replays stored history**: nothing is ordered, filled or posted, and nothing
is promoted automatically.

**Run a walk-forward validation** from the dashboard (`/research`, *Run a walk-forward
validation*) or the CLI. The configuration is the API body as JSON; examples for the six
strategies on the development data are in `config/validation/`.

```bash
uv run python -m app.learning.cli run --config ../config/validation/dev-momentum.json
```

```bash
uv run python -m app.learning.cli show RUN_ID
```

The holdout (the final 20% of sessions by default) is reserved when the run is created and is
never read by the run. Once you have finished choosing, and only then, read it once:

```bash
uv run python -m app.learning.cli holdout RUN_ID
```

Every holdout read is numbered. A second read of an overlapping holdout for the same strategy is
labelled as no longer an untouched test, and governance accepts only a first read.

`reproduce RUN_ID` re-executes a run from its stored configuration and records whether the
result hash is identical. `list` shows recent runs. The API resumes queued runs and fails
interrupted ones at start-up.

**Configuration fields** (beyond the backtest fields): `kind` (`WALK_FORWARD` with one strategy
and a search; `EVALUATION` with up to six strategies, fixed parameters, `search: NONE`);
`strategies[].space` (dimensions as `values` or `low`/`high`/`step`, plus `constraints`; default:
the family's space in `app/learning/search.py`); `search` (`GRID` | `RANDOM` | `BAYESIAN`);
`max_trials` (at most 60, including the stored parameters as trial 0); `windows` (`mode`
ROLLING | EXPANDING, `train_sessions`, `validation_sessions`, `test_sessions`, `step_sessions`,
`embargo_sessions` (at least the longest label), `holdout_sessions`, `refit_every`, `max_folds`);
`objective` (`NET_SHARPE` | `NET_EXPECTANCY` | `NET_RETURN`); `min_selection_trades`; `top_k`;
`robustness` (PBO/CPCV groups, bootstrap and Monte Carlo resamples, cost and slippage
multipliers, sensitivity); `thresholds` (the evidence gates); `seed`.

**When a run is refused at creation:** "one fold needs N sessions" means the date range is too
short for the windows and the embargo. Shorten the windows or extend the data. A long holding
horizon (trend following: 20 sessions) needs a long embargo.

**Champion/challenger** (API under `/api/v1/validation/candidates`, or the CLI):

```bash
uv run python -m app.learning.cli candidate-strategy momentum --run RUN_ID --reason "..."
```

```bash
uv run python -m app.learning.cli transition CANDIDATE_ID start_validation --reason "..."
```

Then `shadow --run RUN_ID`. This needs a CANDIDATE_FOR_REVIEW run with the candidate's exact
parameters and a first holdout read. Next, `POST /api/v1/validation/candidates/{id}/shadow-evaluation`
after enough forward sessions, then `approve --replaces CHAMPION_ID`. `reject`, `retire` and
`rollback` complete the lifecycle. Approval and rollback create a new strategy version; retiring
a strategy champion without a replacement disables the strategy. To record today's live version
as the governed champion without changing it, use `POST /api/v1/validation/champions/adopt`.

**Model comparison:** `uv run python -m app.learning.cli models --store` compares the stored 5A
training runs and records the comparison that model challengers need.

**Degradation monitoring:** `uv run python -m app.learning.cli monitor` (or
`POST /api/v1/validation/monitoring/run`) runs one pass. It only warns unless an operator
approves a rule:

```bash
curl -X POST -H "X-Atlas-Operator-Token: $TOKEN" -H "Content-Type: application/json" -d '{"subject_kind":"STRATEGY","subject_key":"momentum","action":"SUSPEND","reason":"..."}' http://localhost:8000/api/v1/validation/monitoring/rules
```

Under a SUSPEND rule, a performance BREACH disables the strategy. Re-enabling it is an operator
action (`POST /api/v1/strategies/{id}/status` with the operator token). Nothing in monitoring
changes risk limits.

## Autonomous paper trading (Phase 10A)

Prerequisites: the database at head (migration 0020), the paper account and its risk
configuration (`accounting.cli init`, `risk.cli init`), and the paper execution worker and
position monitor running (they fill, protect and exit). Design in
[`architecture/orchestration.md`](architecture/orchestration.md); decisions D-204 to D-212.
**Paper trading only**: every order is a simulated paper order.

**Two separate switches.** `ORCHESTRATOR_WORKER_ENABLED=true` only starts the worker. New
entries also need the operator control ENABLED, which is DISABLED until an operator enables it:

```bash
uv run python -m app.orchestration.cli status
```

```bash
uv run python -m app.orchestration.cli enable --actor operator:jacob --reason "start the paper trial"
```

```bash
uv run python -m app.workers.trading_orchestrator
```

`--once` runs a single tick. With the research stages on (the defaults) the worker needs the
market-data provider configuration, as the strategy and AI workers do; set
`ORCHESTRATOR_RUN_STRATEGY_STAGE=false` etc. to run only intake, delivery and the scheduled jobs.

**Stopping trading** (also from the dashboard's Autonomous Trading page or the API with the
operator token): `pause` (resume later), `disable`, or

```bash
uv run python -m app.orchestration.cli emergency-stop --actor operator:jacob --reason "halt everything"
```

which also engages the risk kill switch and cancels working entry orders; exits keep running.
Afterwards: `clear-emergency` (back to DISABLED), `python -m app.risk.cli kill-switch release`,
then `enable` when ready. Stopping the worker (SIGTERM) does not change the trading mode.

**Inspect:** `intents [--status S]` (each prioritized decision and what risk and execution did
with it), `jobs [--kind K] [--status S]` (scheduled monitoring and reconciliations),
`history` (control changes). A DEAD intent or job writes a HIGH `system_health_events` row; a
failed reconciliation writes HIGH (reservations) or CRITICAL (accounting, execution).

**After a crash or restart** nothing needs to be done by hand: pending intents are delivered by
the next tick, stuck job leases are reclaimed, and decisions older than the intake lookback are
not taken in.

## Reliability and operator access (Phase 10B)

Migration 0021. Design in [`architecture/orchestration.md`](architecture/orchestration.md);
decisions D-213 to D-221. **Paper trading only.**

**Run all three workers.** The orchestrator takes no new entry unless the position monitor
and the paper execution worker are running (their heartbeats are fresh): the *entry gate*.
While either is down it alerts (CRITICAL if anything is open) and runs their protective work
itself; exits never stop. Check it with:

```bash
uv run python -m app.orchestration.cli protection
```

**Named operators.** Issue each person a credential; the token goes to a new file you name and
is never printed (only its SHA-256 digest is stored). `--trading` lets it use the API's
pause/disable/emergency-stop and kill switch; nothing lets it enable trading.

```bash
uv run python -m app.orchestration.cli operator-add --name jacob --trading --token-file ./jacob.token --actor operator:jacob --reason "desk operator"
```

```bash
uv run python -m app.orchestration.cli operators
```

```bash
uv run python -m app.orchestration.cli operator-revoke --name jacob --actor operator:jacob --reason "rotate the token"
```

Send the token as `X-Atlas-Operator-Token` (or `Authorization: Bearer`); the dashboard's
Autonomous Trading page has a field for it. Delete the token file after storing the token
safely. In `ATLAS_ENVIRONMENT=paper` the trading controls accept only named credentials issued
with `--trading`, every CLI operator action must name an active credential (the first
`operator-add` bootstraps), and the orchestrator needs `ORCHESTRATOR_ACCOUNT_NAME`.

**Alerts and metrics.** Each alert says what to do:

```bash
uv run python -m app.orchestration.cli alerts --open
```

```bash
uv run python -m app.orchestration.cli ack-alert --id 42 --actor operator:jacob --note "monitor restarted"
```

```bash
uv run python -m app.orchestration.cli metrics
```

**Dead letters.** A scheduled job that failed every attempt is DEAD (HIGH alert). Fix the
cause, then:

```bash
uv run python -m app.orchestration.cli requeue-job --id 17 --actor operator:jacob --reason "provider back"
```

**Outages.**

| Event | What Atlas does | What to do |
|---|---|---|
| position monitor stops | gate closed; CRITICAL alert; the orchestrator runs protective passes | restart `position-monitor-worker` |
| paper execution worker crashes | gate closed; CRITICAL alert; the orchestrator delivers and simulates | restart `paper-execution-worker` |
| paper broker stopped cleanly | gate closed; CRITICAL alert; nothing fills | start `paper-execution-worker` |
| market data stale | `FEED_STALE`; DATA_QUALITY halts entries; no exit at a stale price | check the stream worker / provider; the recovery worker backfills |
| orchestrator down for days | on restart: daily jobs caught up, interval gaps reported | check why it stopped |
| database restarted | failing steps are recorded and retried; nothing is duplicated | none |
| Redis down | orchestration is unaffected (PostgreSQL holds every state) | none for trading |

**Execution model version 2.** New paper orders are simulated with the gap rule (a stop
crossed by a gap fills no better than the first observed price beyond it). Orders already
being worked keep version 1. `GET /api/v1/paper-execution/models` lists both.

## Evidence and readiness (Phase 11A)

Migration 0022. Design in [`architecture/evidence.md`](architecture/evidence.md); decisions
D-222 to D-228. **Paper trading only; a scorecard changes nothing that trades and promotes
nothing.**

1. **Register the criteria first** (before the evidence they judge exists). In the paper
   environment the actor must be an active named credential.

   ```bash
   uv run python -m app.evidence.cli register-protocol --actor operator:jacob --rationale "why these gates"
   ```

   `criteria` prints the defaults; `--criteria FILE.json` registers different ones as a new
   version. Identical criteria are refused.

2. **Audit the historical data** behind an evaluation:

   ```bash
   uv run python -m app.evidence.cli audit --actor operator:jacob --symbols SYMBOLS --start 2021-01-04 --end 2026-10-30 --selected-on 2026-11-09 --brief
   ```

3. **Validate** each strategy (Phase 8B), e.g. `config/validation/11a-*.json`, and read each
   run's holdout once when selection is finished.

4. **Build a scorecard**:

   ```bash
   uv run python -m app.evidence.cli scorecard --actor operator:jacob --protocol PROTOCOL_ID --run momentum=RUN_ID --run breakout=RUN_ID --portfolio-run EVALUATION_RUN_ID --audit AUDIT_ID --account "paper-trading" --brief
   ```

   `latest --brief`, `show ID` and the dashboard's **Evidence & Readiness** page (`/evidence`)
   display it. API: `/api/v1/evidence/...` (POST needs the operator token).

**Reading a scorecard.** REJECTED: negative evidence with a sufficient sample (an operator may
disable the strategy in paper trading). INSUFFICIENT_EVIDENCE / NOT_DEMONSTRATED /
CONTINUE_PAPER: keep collecting evidence. CANDIDATE_FOR_REVIEW: every criterion met; a human
reviews. Strategy approval through governance needs a CANDIDATE_FOR_REVIEW scorecard built after
the candidate entered SHADOW, under a protocol registered before that.

### Live paper observation (Phase 11A)

**Paper trading only.** A dedicated paper database (`atlas_paper_11a`), a restricted database
role (`atlas_paper`: no superuser, no DDL) and host processes started by
[`scripts/paper_observation.sh`](../scripts/paper_observation.sh). Configuration is the
git-ignored `.env.paper11a` (paper environment; overrides `.env`, which keeps the provider
credentials). Logs and PIDs: `logs/paper11a/`. API on `127.0.0.1:8124`.

```bash
scripts/paper_observation.sh start
```

```bash
scripts/paper_observation.sh status
```

```bash
scripts/paper_observation.sh stop
```

`start` runs the market-data stream, recovery, news stream, paper execution, position monitor
and orchestrator workers plus the API. **Starting them does not enable trading**: the trading
control starts DISABLED, and only a named operator enables it (`ctl` loads the paper
environment and runs `app.orchestration.cli`; on Windows run the script with Git Bash, e.g.
`& "C:\Program Files\Git\bin\bash.exe" scripts/paper_observation.sh ...`):

```bash
scripts/paper_observation.sh ctl enable --actor operator:jacob --reason "11A paper observation"
```

`ctl status`, `ctl protection`, `ctl alerts --open`, `ctl pause`, `ctl disable` and
`ctl emergency-stop` (the last three with `--actor` and `--reason`) are the same CLI. The operator's API token is in `data/paper11a/jacob.token` (git-ignored; never printed).

* **Strategies.** Only the strategies the latest scorecard did not reject are enabled in the
  paper database (2026-11-12: mean reversion and event driven). Enabling another one is an
  operator action (`POST /api/v1/strategies/{id}/status`).
[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]
* **Durability.** These are plain host processes: they stop when the machine sleeps or shuts
  down or the processes are killed. Only sessions in which the orchestrator actually ran count
  as observed (D-226). For an unattended multi-week run, host them under Docker Compose or a
  service manager.
* **Scorecard with paper evidence** (after enough sessions): add `--account "Atlas paper
  account"` to the `scorecard` command, run against the paper database.

## Backup and restore

```bash
# Backup (custom format, compressed) into ./data/backups
mkdir -p data/backups
docker compose exec -T postgres pg_dump -U "$ATLAS_POSTGRES_USER" -d "$ATLAS_POSTGRES_DB" -Fc \
  > "data/backups/atlas-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

```bash
# Restore into an empty database (stop the backend first)
docker compose stop backend
docker compose exec -T postgres pg_restore -U "$ATLAS_POSTGRES_USER" -d "$ATLAS_POSTGRES_DB" \
  --clean --if-exists < data/backups/<file>.dump
docker compose up -d backend
```

Because the audit log is append-only, `pg_restore --clean` drops and recreates it rather than
deleting rows. The triggers are restored with it.

## Recovery checklist

1. `docker compose ps`: are `postgres` and `redis` healthy?
2. `curl http://127.0.0.1:8000/ready`: which component fails, and what is its detail?
3. If the `migrations` component fails, run `docker compose run --rm migrate`.
4. If `database` fails, check `docker compose logs postgres`, then disk space and credentials.
5. If `redis` fails, run `docker compose restart redis`. Nothing authoritative lives in Redis.
6. If the backend fails to start, look for `Invalid Atlas configuration` in
   `docker compose logs backend`. Startup validation names the offending variable.
