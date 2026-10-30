# Atlas AI Trader — guidance for Claude Code

## Authoritative specification

[`ATLAS_MASTER_SPEC_V2.md`](ATLAS_MASTER_SPEC_V2.md) is the authoritative specification and
supersedes all earlier versions. Read it before any phase work. Do not change the architecture
it defines without documenting the reason in [`docs/architecture/decisions.md`](docs/architecture/decisions.md).

* Phase prompts as given by the user: [`docs/prompts/`](docs/prompts/)
* Phase completion reports: [`docs/phase-reports/`](docs/phase-reports/)
* Architecture and decisions: [`docs/architecture/`](docs/architecture/)
* Operations (start/stop, migrations, backup): [`docs/operations.md`](docs/operations.md)

## Non-negotiables

* **Paper trading only.** No live-broker integration, no order submission to any broker (paper
  or live), and no configuration switch that could enable either (spec §24.1).
* **Never print or log credential values.** Real credentials live only in the git-ignored
  `.env`. `.env.example` holds placeholders only.
* Work one phase at a time. Implement only what the current phase asks for.
* Never fabricate market data or provider responses. Label fixtures, mocks and simulations.
* PostgreSQL is the source of truth. Use UTC timestamps and `Decimal`/`NUMERIC` for money.
  Never overwrite historical records.

## Commands

| Task | Command |
|---|---|
| Backend tests | `uv run pytest` (integration tests need `ATLAS_TEST_DATABASE_URL`) |
| Lint / types | `uv run ruff check backend` · `uv run mypy` |
| Frontend tests | `cd frontend && pnpm test` |
| All checks | `scripts/check.sh` |
| Migrations | `uv run alembic upgrade head` · `uv run alembic check` |
| Full stack | `docker compose up --build` |
