# Phase 10A — Autonomous paper-trading orchestration: completion report

**Paper trading only.** No live-broker integration, no order reaches any broker, and no setting
can enable either. Every order the orchestrator causes is a simulated paper order of the 7A
engine, filled by the isolated paper broker from stored market data.

## 0. Preflight and scope

| Item | Finding |
|---|---|
| Starting point | Branch `phase-10a-orchestration` (from `main` at `cce91a7`, Phases 1–9). Phase 10A was **partial and uncommitted**: `app/orchestration/` (config, controls, enums, intents, jobs, orchestrator, service), the models and migration 0020. No worker entrypoint (referenced but missing), no API, CLI, dashboard, tests or report. |
| Phase 10A prompt | Not in `docs/prompts/`. Scope reconstructed from spec §27 Phase 10, §20 Stage 4, §24.2 and the Phase 10B prompt's references to 10A (D-212). |
| Open findings | The 8B/9 gate lists no open P0/P1 and one known P2 (the 7B gap-through-stop rule, in Phase 10B scope). The partial 10A code had no P0/P1 defect: it was unreachable (no worker). One question was checked: an entry order rejected at revalidation **does** release its reservation (7A `_settle_reservation`). |
| Baseline checks | ruff clean; mypy 76 errors in 5 pre-existing test files (unchanged baseline from 8B/9); migration round-trip including 0020 passed. |

## 1. Files

**Created**

* `backend/app/orchestration/runtime.py` — wiring for the worker, CLI and API; scheduled-job bodies; reconciliation findings → health events.
* `backend/app/orchestration/queries.py` — read models (status, intents, jobs, counts).
* `backend/app/orchestration/cli.py` — operator CLI.
* `backend/app/workers/trading_orchestrator.py` — the worker (tick loop, heartbeat task, graceful stop, outage tolerance).
* `backend/app/api/v1/orchestration.py` — reads and fail-safe controls.
* `backend/app/tests/unit/test_orchestration.py`, `backend/app/tests/integration/orchestration_support.py`, `test_orchestration.py`, `test_orchestration_api.py`.
* `frontend/src/pages/AutonomyPage.tsx`, `frontend/src/services/orchestration.ts`, `frontend/src/types/orchestration.ts`, `frontend/src/pages/autonomy.test.tsx`.
* `docs/architecture/orchestration.md`, this report, `docs/prompts/phase-10b-reliability-sustained-operation.md` (the next phase's prompt as given).

**Completed (were untracked, now part of the phase):** `backend/app/orchestration/{__init__,config,controls,enums,intents,jobs,orchestrator,service}.py`, `backend/app/database/models/orchestration.py`, `backend/alembic/versions/0020_autonomous_orchestration.py`.

**Modified**

* `orchestrator.py` — heartbeat-only `beat()`; `WORKER_UNHEALTHY` de-duplicated (once per worker per hour); worker name aligned with the workers report.
* `backend/app/monitoring/workers.py` — the orchestrator is a HEARTBEAT worker in `/system/workers`.
* `backend/app/main.py`, `backend/app/api/v1/__init__.py` — orchestration context and router.
* `backend/app/database/models/__init__.py` — model registration (import order fixed).
* `backend/app/tests/integration/test_system_workers_api.py` — expects the new worker row.
* `backend/app/tests/unit/{test_api,test_health_checks,test_models}.py` — route, migration-head and table inventories.
* `frontend/src/App.tsx`, `frontend/src/navigation.ts` — the Autonomous Trading page.
* `docker-compose.yml` — `orchestrator-worker` service (off unless enabled).
* `.env.example` — `ORCHESTRATOR_*` settings (worker off by default).
* `docs/architecture/decisions.md` (D-204 – D-212), `docs/operations.md`.

## 2. Architecture

See [`architecture/orchestration.md`](../architecture/orchestration.md). In short:

* **One tick:** reservation expiry → strategy cycle → LIVE decision run (only when new LIVE
  proposals arrived) → intake (only when ENABLED, in an entry session) → delivery → job sweep →
  scheduled jobs → heartbeat. Each step isolated; failures recorded and retried.
* **Intent handling:** one transaction under the account lock: mode check → expiry check → 6C
  `reserve_in` (the risk engine decides and sizes) → 7A `submit_in` ENTRY (LIMIT at the reserved
  worst entry price) → outcome. Idempotency keys per intent; UNIQUE intent per decision.
* **Controls:** durable, append-only, operator-only trading mode, DISABLED by default. The API
  may only pause, disable or emergency-stop; enabling, resuming and clearing are CLI actions.
* **Jobs:** one row per (kind, slot), leased, retried with back-off, DEAD after max attempts,
  stuck leases reclaimed.
* **State:** PostgreSQL only. Redis plays no part.

## 3. Features

| Spec Phase 10 item | How Atlas does it now |
|---|---|
| Scan markets, discover assets | Existing discovery worker (3A) and market intelligence (daily job here) |
| Generate strategies / evaluate opportunities | Strategy stage (4A/4B) and decision stage (5B) in each tick |
| Approve permitted paper orders through the risk system | Intents → 6C reservation → 7A order; the risk engine alone sizes and approves |
| Manage / close positions | Existing position monitor (7B), never blocked by any trading mode |
| Record outcomes, evaluate performance | Intents with reasons; decision-outcome evaluation after the close; reconciliations |
| Runs within market schedules | Entry sessions configurable; after-close jobs keyed to trading days |
| Correct accounting | Accounting, reservation and execution reconciliations scheduled; failures alert |
| Safely handles failures | Isolated steps, transactional delivery, leased jobs, outage-tolerant worker |
| Emergency controls (§24.2) | Pause, disable, emergency stop (kill switch + entry cancellation); exits untouched |

## 4. Database and configuration

* **Migration 0020** (additive; no existing table changed): `trading_control_events`,
  `trade_intents`, `orchestration_jobs`, `orchestrator_status`, with append-only / never-delete
  / final-intent guards. Round-trip (upgrade/downgrade/upgrade) and model-drift tests pass.
* **Configuration:** `ORCHESTRATOR_*` (see `.env.example`). `ORCHESTRATOR_WORKER_ENABLED=false`
  by default; unknown `ORCHESTRATOR_*` keys are refused at start. No setting enables trading.
* **No risk limit, paper balance or existing record was changed.** No production or Docker
  database was migrated; verification used throw-away test databases and one disposable preview
  database (`atlas_phase10a_preview`).

## 5. Dependencies

None added.

## 6. Commands executed

```
.venv/Scripts/ruff.exe format --check backend; .venv/Scripts/ruff.exe check backend
.venv/Scripts/mypy.exe
ATLAS_TEST_DATABASE_URL=… .venv/Scripts/pytest.exe            (full backend suite)
cd frontend; pnpm typecheck; pnpm test; pnpm build
alembic upgrade head; accounting.cli init; risk.cli init        (disposable preview DB only)
python -m app.orchestration.cli status|enable|pause|jobs        (preview DB)
python -m app.workers.trading_orchestrator --once               (preview DB, research stages off)
```

## 7. Tests

**New tests (all pass):**

| Area | Tests |
|---|---|
| Unit (13) | defaults never enable trading; no live/broker/mode setting; unknown keys refused; `.env.example` loads; transition table (nothing reaches ENABLED from EMERGENCY_STOP); slots, back-off, order expiry; the orchestrator is an authorized submitter but not an operator |
| Controls | disabled by default and nothing ordered; operator-only, reason required, audited; DB rejects rewritten, skipped or inconsistent control events; concurrent control changes serialize gaplessly |
| Intents | enabled path reserves and orders exactly the risk engine's approval (LIMIT at worst entry price); idempotent across repeated ticks, two concurrent orchestrators and a restart; non-prioritized, historical, failed-run, stale and expired decisions are not taken in; mode change before delivery → NOT_TRADED; kill switch → RISK_REJECTED; crash mid-delivery rolls back and the retry creates exactly one reservation and order; permanent failure → DEAD with nothing reserved; final intents immutable |
| Emergency stop | engages the kill switch, cancels the autonomous entry (its reservation released), keeps an exit order working and allows a new exit; no intake while stopped; repeat is safe; clear returns to DISABLED |
| Jobs | one run per slot across 8 concurrent runners; back-off then DEAD with an alert; stuck lease reclaimed; sweep of past slots; real risk monitor and all three reconciliations pass on clean books; after-close slot runs once a day |
| Worker / recovery | status and clean stop; survives a simulated database outage and recovers without duplicates; restart between intake and delivery resumes once; a disabled worker exits without touching the database; a failing stage degrades the tick, not the trading work |
| Extended operation | 12 ticks across a simulated session with new decisions, pause/resume and a restart: one intent per decision, at most one reservation and order per decision, one row per job slot |
| API / CLI | defaults; only fail-safe controls exist (no enable/resume/clear routes); token enforced when configured and never echoed; intents and jobs listed; orchestrator in `/system/workers`; CLI enable/stop/clear/history and refusal codes |
| Frontend (5) | default disabled state with no enabling control; intents, jobs and history; pause and emergency stop send reason and token; refused control shown |

**Regression results:** see §7a.

**Real-environment check:** against the disposable preview database, the CLI enabled, paused
and listed state; the real worker ran one tick (all five scheduled jobs SUCCEEDED, every
reconciliation passed, intake correctly skipped while PAUSED); the dashboard showed the state,
history and jobs, and its Emergency stop button moved the account to EMERGENCY_STOP with the
kill switch engaged. One defect found this way was fixed: the worker card took the API
process's switch over the worker's own record (now the record decides, D-210).

### 7a. Full regression

| Check | Result |
|---|---|
| Backend full suite (`pytest`, real PostgreSQL) | First run: 3594 passed, **3 failed**, 1 skipped (34 min). The 3 failures were inventory guards that list every API route, the migration head and every table; they were updated for 10A (the route guard now also asserts that the orchestration writes are POST-only and that no enable/resume/clear route exists). Re-run of all unit tests: **3002 passed**; orchestration API, workers API and unit files re-run after the last code change: **26 passed**. Net: 3597 passed, 0 failed, 1 skipped. |
| Skipped | 1: Redis integration test (`ATLAS_TEST_REDIS_URL` not set), as in earlier phases. |
| `ruff format --check` / `ruff check` | clean |
| `mypy` | 76 errors in 5 files: the unchanged pre-existing baseline; none in Phase 10A code |
| Frontend `typecheck` / `test` / `build` | clean / **161 passed** (21 files) / built |
| Migrations | upgrade/downgrade/upgrade round trip and model-drift check pass with 0020 |

## 8. Known issues and limits

* **Not exercised against live market data.** The research stages (strategy cycle, decision run,
  market intelligence) are the existing, separately tested engines; in 10A tests they are
  replaced by labelled stubs, and the preview run had them switched off. A sustained run with
  real data is Phase 10B/11A work.
* One account per orchestrator process.
* Missed scheduled slots are not back-filled after a long outage (the next slot runs).
* The orchestrator depends on the paper execution worker and position monitor running; it
  reports their staleness but cannot restart them.
* API operator identity is the shared token (`operator:api`); named operators use the CLI.
* The 7B gap-through-stop P2 and the 88-session history limitation are unchanged (Phase 10B).

## 9. Remaining work (Phase 10B, not started)

Reliability hardening per the Phase 10B prompt: durable position-monitoring schedule, missed
slot recovery, Redis/DB outage drills with real processes, gap-through-stop simulation, operator
authentication, alerting, a soak test, and the historical-data review.

## 10. How to run

See [`operations.md` → Autonomous paper trading](../operations.md#autonomous-paper-trading-phase-10a).
Trading stays DISABLED until an operator runs
`python -m app.orchestration.cli enable --actor operator:<name> --reason "<why>"`.
