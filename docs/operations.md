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
