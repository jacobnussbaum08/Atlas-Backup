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
