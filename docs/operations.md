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

**When nothing fills:** look at `orders show --id ID` -> `execution.attempts`. DEFERRED codes
(`MARKET_CLOSED`, `STALE_QUOTE`, `NO_QUOTE`, `LIQUIDITY_UNKNOWN`, `VOLUME_UNKNOWN`) mean the
data was insufficient; NO_FILL codes (`LIMIT_NOT_THROUGH`, `WAITING_FOR_QUOTE`,
`PROTECTION_EXCEEDED`, `STOP_NOT_TRIGGERED`, `RISK_HALTED`) mean it was sufficient and the
order did not execute. A position with `monitor_status = STALE` has no fresh price: its
protection waits for one (no exit at a stale or invented price).

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
