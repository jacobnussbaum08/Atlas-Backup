# Phase 2 preflight — project organization and verification

**Date:** 2026-10-29

**Scope:** Preparation only. No Phase 2 code was written and no market data was requested.

**Readiness: NOT READY.** The application code is healthy. Two critical blockers remain:

* No Docker, so there is no PostgreSQL, Redis or TimescaleDB service for development.
* No git baseline commit, because git has no author identity configured.

See [§10](#10-readiness-for-phase-2).

## 1. Project location

| Item | Value |
|---|---|
| Project root (`git rev-parse --show-toplevel`) | `E:\trading automater` |
| Branch | `main` |
| Commit | none. The repository has no commits (see §7) |
| Remotes | none. Nothing has been pushed |
| Duplicate Atlas projects | none found on `E:\` |

## 2. Master specification

* **Finding:** The spec existed only as `ATLAS_MASTER_SPEC_V2.md.md` (double extension).
* **Action:** Renamed it with `git mv` to `ATLAS_MASTER_SPEC_V2.md`. The content is unchanged:
  SHA-256 `d00300e3…c72f05` is identical before and after.
* **Designated as authoritative** in two places:
  * `README.md`, which links to it and states it supersedes earlier specs.
  * The new `CLAUDE.md`, which Claude Code loads automatically in this project.
* `docs/phase-reports/phase-1-report.md` still mentions the old filename. It is a historical
  report and was left unedited.

## 3. Documentation organization

| Before | After | How |
|---|---|---|
| `docs/architecture.md` | `docs/architecture/overview.md` | `git mv` |
| `docs/decisions.md` | `docs/architecture/decisions.md` | `git mv` |
| `docs/phase-1-report.md` | `docs/phase-reports/phase-1-report.md` | `git mv` |
| `docs/operations.md` | unchanged | — |
| — | `docs/prompts/phase-1-foundation.md` | new: Phase 1 instructions, verbatim |
| — | `docs/prompts/phase-2-market-data-engine.md` | new: Phase 2 brief, verbatim |
| — | `docs/phase-reports/phase-2-preflight-report.md` | new: this report |
| — | `CLAUDE.md` | new: points Claude Code at the spec, docs, rules, commands |

No document was deleted or overwritten. `README.md` links were updated to the new paths.

## 4. Credential protection

Verified without displaying any credential value. Values were compared by length or by
`grep -F` against a temporary pattern file, which was deleted afterwards.

| Check | Result |
|---|---|
| `.env` exists | ✅ |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` present and non-empty in `.env` | ✅ |
| `ALPACA_DATA_FEED`, `MARKET_DATA_PROVIDER` present in `.env` | ✅ (`iex`, `alpaca`) |
| `.env` ignored by git | ✅ `.gitignore:2:.env` (also `.env.*`, with `!.env.example`) |
| `.env` tracked or staged | ✅ never (0 index entries) |
| Secret values in any working-tree file outside `.env` | ✅ none |
| Secret values in staged files | ✅ none |
| Secret values in git history | ✅ none. There are no commits |
| `.env.example` key and secret | ✅ empty placeholders, with comments. Safe to commit |
| Credentials or `VITE_*` secrets in frontend code | ✅ none |
| Backend reads Alpaca variables | Not yet: Phase 2 adds them. `.env` loads fine (`ALPACA_*` keys are outside the `ATLAS_` namespace) |

**Exposure history.** Earlier in this session the real key and secret had been written into the
tracked template `.env.example`. They were moved to `.env` before any commit, so they never
entered git. Their values were, however, shown to the Claude Code session when that file
changed.

**Recommendation:** rotate both credentials in the Alpaca dashboard. Put the new values only in
`.env`, and use a market-data-only key with no trading permissions.

## 5. Phase 1 test results (rerun, no tests changed)

| Suite | Result |
|---|---|
| `ruff format --check` / `ruff check` | 65 files formatted / all checks passed |
| `mypy` (strict) | no issues, 63 files |
| Backend `pytest`, integration enabled (`ATLAS_REQUIRE_INTEGRATION=1`) | **99 passed, 1 skipped** |
| Frontend `tsc --noEmit` | no errors |
| Frontend `vitest run` | **10 passed** (2 files) |

**Skipped:** `test_real_redis_round_trip`. `ATLAS_TEST_REDIS_URL` is not set because no Redis
server exists.

**Warnings:** one, `StarletteDeprecationWarning` (httpx with `TestClient`). It is known and
filtered in the project config (decision D-013).

**Not a failure:** the integration tests ran against a temporary PostgreSQL 16.2, built from the
`pgserver` package and kept in the session scratchpad. It is not the project's Docker
PostgreSQL.

## 6. Migrations

Run against the `atlas_dev` database on the temporary PostgreSQL 16.2:

* `alembic current` → `0001 (head)`
* `alembic upgrade head` → no-op (idempotent)
* `alembic check` → "No new upgrade operations detected"
* The integration tests also passed the upgrade → downgrade → upgrade round trip on fresh
  databases.

## 7. Infrastructure

| Item | Status |
|---|---|
| Docker / Docker Compose | ❌ **not installed.** `docker` is not on PATH and there is no `C:\Program Files\Docker` |
| `docker compose config` | ❌ cannot run without Docker. A static YAML parse succeeds |
| Compose services | `postgres` (postgres:16-bookworm), `redis` (redis:7.4-bookworm), `migrate`, `backend`, `frontend`, `backend-tests` (profile `test`). Volume: `postgres-data` |
| WSL 2 (required by Docker Desktop) | ❌ not installed |
| Hardware virtualization | Hypervisor present. The firmware flag reads `False` while Hyper-V runs, which is normal |
| PostgreSQL (project service) | ❌ not running, because there is no Docker |
| PostgreSQL (engine) | ✅ the temporary 16.2 server works and is stopped again |
| Redis | ❌ not verified. No server available |
| TimescaleDB | ❌ **not available.** The compose stack uses plain `postgres:16` by design (D-005), and the temporary server has no `timescaledb` extension |

**To resolve,** in an administrator PowerShell:

1. `wsl --install`, then reboot.
2. Install Docker Desktop for Windows from docker.com and enable "Use the WSL 2 based engine".
3. From the project root:
   * `docker --version`
   * `docker compose version`
   * `docker compose config --quiet`
   * `docker compose up -d`
   * `docker compose ps`

The compose file was not changed in this preflight.

**Phase 2 change:** switching `postgres` to a `timescale/timescaledb:*-pg16` image is a drop-in
on the same PostgreSQL major version. It belongs to Phase 2, with a decision-log entry,
because Phase 2 introduces the first time-series tables.

## 8. Git baseline

| Item | Status |
|---|---|
| Identity | ❌ `user.name` and `user.email` are not set (repo or global) |
| Commit `Phase 1 foundation baseline` | **not created.** Instruction: do not commit without identity |
| Staged | 114 files: the whole project, including this preflight's changes |
| Forbidden paths staged (`.env`, `.venv`, `node_modules`, `.coverage`, `dist`, logs) | none |
| Ignored | `.env`, `.venv/`, `.coverage`, tool caches, `frontend/node_modules/` |
| Pushed | nothing. There are no remotes |

To finish, set an identity (use `--global` for every repository) and commit:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
git commit -m "Phase 1 foundation baseline"
```

## 9. Files

**Created**
* `CLAUDE.md`
* `docs/prompts/phase-1-foundation.md`
* `docs/prompts/phase-2-market-data-engine.md`
* `docs/phase-reports/phase-2-preflight-report.md`
* `.env` (local and git-ignored; it holds the credentials moved out of `.env.example`)

**Modified**
* `README.md`: spec link and docs links
* `.env.example`: Alpaca credentials blanked, with a commented market-data section and a
  trailing newline

**Moved** (`git mv`)
* the spec, from `ATLAS_MASTER_SPEC_V2.md.md` to `ATLAS_MASTER_SPEC_V2.md`
* the three docs listed in §3

## 10. Readiness for Phase 2

| Requirement | Status |
|---|---|
| Alpaca configuration available locally, not exposed | ✅ (rotation recommended, §4) |
| PostgreSQL working | ⚠️ engine verified via the temporary server. The project's Docker service is not available |
| Redis working | ❌ not verified |
| Existing API works | ✅ (unit and integration tests, plus the Phase 1 live run) |
| Frontend works | ✅ (tests, typecheck) |
| Existing tests pass | ✅ |
| Migrations work | ✅ |
| Master specification available | ✅ |
| TimescaleDB | ❌ not available |
| Git baseline commit | ❌ missing identity |

**Status: NOT READY.** Phase 2 must persist live market data reliably and decide on TimescaleDB
hypertables. Neither can be developed or verified without a real PostgreSQL/TimescaleDB and
Redis service, and there is no committed baseline to roll back to.

Once Docker is installed (§7) and the baseline commit exists (§8), rerun the §7 commands. If
PostgreSQL and Redis come up healthy and `/ready` reports all components `ok`, Atlas is
**READY**. TimescaleDB is then a planned Phase 2 change, not a blocker.

---

## Addendum — infrastructure verification with Docker (2026-10-29)

Docker Desktop (WSL 2 backend) was installed after the report above, and the infrastructure
checks were rerun.

**Stack state:** the `atlas` stack was already up when checked. Nothing was rebuilt, restarted
or reset, and no volume was touched. `docker compose up -d postgres redis` was a no-op.

| Check | Result |
|---|---|
| `docker --version` / `docker compose version` | ✅ Docker 29.9.2 / Compose v5.6.1 |
| `docker compose config --quiet` | ✅ valid |
| Services | ✅ `postgres`, `migrate`, `redis`, `backend`, `frontend` (+ `backend-tests` under profile `test`) |
| PostgreSQL | ✅ 16.15. Healthy, `pg_isready` OK, timezone UTC, reachable from the host on `127.0.0.1:5432` with the app's engine |
| Redis | ✅ 7.4.12. Healthy. `PING` → `PONG` in the container and from the host (app client). RDB/AOF persistence off as designed |
| TimescaleDB | ❌ not available. `postgres:16-bookworm` has no `timescaledb` extension files (D-005). Planned as a Phase 2 change |
| Migrations | ✅ `migrate` container applied `0001` and exited 0. `alembic current` = `0001 (head)`. Rerunning `upgrade head` is a no-op. `alembic check` reports no drift. Audit-log triggers and PAPER-only constraint present |
| Running API `/health` / `/ready` | ✅ 200 / 200 `ready`: database, migrations and Redis all `ok` |
| Dashboard and proxy (`:5173`) | ✅ 200, `/ready` proxied |
| Backend suite against Docker PostgreSQL + Redis (`ATLAS_REQUIRE_INTEGRATION=1`) | ✅ **100 passed, 0 skipped**, including `test_real_redis_round_trip`. No scratch databases or test keys left behind |

**Minor defect found (not fixed):** the Redis healthcheck in `docker-compose.yml` sets
`REDISCLI_AUTH` even when `ATLAS_REDIS_PASSWORD` is empty. `redis-cli` then prints
`AUTH failed: … called without any password configured` before `PONG`. The healthcheck still
passes, but the message is misleading.

**Revised readiness: READY WITH LIMITATIONS.** All Phase 1 acceptance criteria are now
verified, including `docker compose up` and Redis. The limitations:

* TimescaleDB is not yet in the stack. It is a planned Phase 2 decision.
* The Alpaca credentials should be rotated before Phase 2 uses them (§4).
