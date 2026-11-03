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

Prerequisites: the database at head (migration 0009). Settings: `STRATEGY_*` in
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
