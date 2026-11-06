# Phase 6C — Atomic risk reservations and portfolio integration: completion report

Prompt: [`docs/prompts/phase-6c-atomic-risk-reservations.md`](../prompts/phase-6c-atomic-risk-reservations.md).
Design: [`docs/architecture/reservations.md`](../architecture/reservations.md). Decisions:
D-148 to D-155 in [`decisions.md`](../architecture/decisions.md).

**Paper only. Nothing is submitted, filled, executed or liquidated, and no reservation posts
to the ledger.** Consumption and release were exercised only by controlled test events
(6A CONTROLLED_TEST postings on test accounts).

## 0. Preflight

* **Base.** This worktree started at `main` (Phase 4B). Phase 6A (`3e95e37`, which contains
  5A/5B) was on an unmerged branch; the branch was fast-forwarded to it (no local commits
  existed, nothing was lost). Phase 6B was **complete but uncommitted** in the worktree
  `atlas-risk-engine-phase-6b-34aef4` (its report finished minutes earlier). Its working
  tree was copied here unchanged (verified identical with `diff -r`, line endings aside).
  It was committed unchanged as its own commit (`Phase 6B: deterministic risk management
  engine`), followed by the 6C commit.
* **6A + 6B verified before any 6C code:** full backend suite with PostgreSQL: 1790 passed,
  1 skipped (Redis), 14 live deselected. The one failure,
  `test_expected_migration_heads_come_from_the_code_base`, came from my new migration 0015
  appearing while that run was going (the test expects head 0014). The 6B report's
  1791 = 1790 + this test. No open P0/P1 finding, no accounting discrepancy. `mypy`: the same
  76 pre-existing errors in five test files as the 6A/6B trees.

## 1. Reservation architecture

```text
strategy worker / AI decision / operator CLI
     |  Proposal (levels and ids; the worker's own assessment is never trusted)
     v
ReservationService.reserve  -- one PostgreSQL transaction, READ COMMITTED ---------------+
  1 portfolio_accounts FOR UPDATE      2 risk_reservation_states FOR UPDATE (counter N)  |
  3 idempotency lookup                 4 expire due reservations (explicit rules)        |
  5 identity / expiry / duplicate intent / approvals-blocked checks                      |
  6 6B evaluate(): ledger marked to fresh prices + breakers + loss state                 |
       + active reservations (exposure, capital, margin, risk)                           |
  7 6B pure assess() (engine 6C.1): every limit minus positions minus reservations       |
  8 risk_assessments, risk_reservations, risk_reservation_requests,                      |
    risk_reservation_events (CREATED, APPROVED|REJECTED), risk_reservation_approvals,    |
    risk_events, audit_logs                                               COMMIT <-------+
         database: event trigger = state machine + projection + totals + counter (40001)
                   deferred trigger: never PENDING; request and approval records present

Phase 7 interfaces: attach_order, consume_in (in the fill's transaction), release,
fail, flag_uncertain; expire_due; ReservationReconciler
```

New package `app/risk/reservations` (service, exposure, reconciliation, queries); the 6B
assessment and evaluation now include active reservations. The risk engine still imports
no strategy, ML or AI code (the 6B static test caught one import during this phase and it
was removed).

## 2. Database locking strategy

One protocol for every reservation path, every account:

1. `portfolio_accounts` row `SELECT ... FOR UPDATE`. The same lock every 6A posting and every
   6B assessment, monitor and breaker action takes, so reservations, fills and breaker
   trips of one account are serialized.
2. The account's `risk_reservation_states` row (`INSERT ... ON CONFLICT DO NOTHING`, then
   `FOR UPDATE`). Its event counter is read before any capacity input.
3. Reservation rows `FOR UPDATE`, in id order (expiry sweep, commands).

The same order everywhere and one account per transaction means no lock cycle (no
deadlocks were observed in any run). **Backstop:** every event carries `account_sequence =
N + 1`; the trigger advances the counter only `WHERE last_sequence = N`, so a writer that
skipped the lock and computed from a stale state is refused with SQLSTATE 40001
(demonstrated: two lock-free transactions writing for different reservations; the second
waits on the counter row and is refused). In-memory locks and Redis play no part.

## 3. Transaction isolation

READ COMMITTED with explicit row locks (D-148). After the account lock is granted, every
later statement sees all transactions committed before it, so the capacity read under the
lock is current. SERIALIZABLE was not used: the explicit lock already serializes the
writers, and 6A/6B rely on the same lock. Serialization failures (40001) and deadlocks
(40P01) are retried by the service up to four attempts.

## 4. Idempotency implementation

* `risk_reservation_requests.idempotency_key` UNIQUE, with the request's SHA-256 and the
  status it produced; `risk_reservations.idempotency_key` UNIQUE too.
* Lookup happens **after** the lock: a concurrent duplicate waits, then finds the committed
  original. Same key + same request returns the original decision with `created=False`
  (and the reservation's current status). Same key + different request is a 409
  `idempotency_conflict`.
* Every command has its own key (consume, release, fail, flag, attach); a duplicate fill
  event is a no-op. A ledger fill can be consumed once (UNIQUE `journal_entry_id`).
* A partial unique index allows one live reservation per strategy proposal or AI decision:
  another key for the same intent is rejected `DUPLICATE_ACTIVE_RESERVATION`.
* A uniqueness race (another account using the same key) is retried once and then returns
  or refuses against the committed winner.
* Interrupted transactions leave nothing (PostgreSQL rollback; PENDING can never commit), so
  the retry reserves exactly once (tested with a dropped connection and with
  `pg_terminate_backend` mid-transaction).

## 5. Reservation lifecycle

States PENDING, APPROVED, PARTIALLY_CONSUMED, CONSUMED, RELEASED, EXPIRED, REJECTED, FAILED;
events CREATED, APPROVED, REJECTED, ORDER_ATTACHED, CONSUMED, RELEASED, EXPIRED, FAILED,
EXECUTION_UNCERTAIN, EXPIRY_DEFERRED (append-only, gapless per reservation and per account).
`risk_reservations` is the current-state projection; only the event trigger may update it,
nothing may delete it. Transitions are in the design doc. Key rules:

* PENDING exists only inside the reserving transaction (deferred trigger refuses to commit
  it). A refusal before assessment (expired, identity, duplicate, blocked) is recorded as
  REJECTED with its reasons and no assessment.
* Expiry: at the assessment's validity (300 s) or the proposal's expiry, whichever is first,
  **only if no order was attached and nothing was consumed**. Otherwise EXPIRY_DEFERRED
  (once) and held. EXECUTION_UNCERTAIN holds the capacity until a confirmed resolution.
  A system actor cannot NOT_SUBMITTED-release or fail an uncertain reservation; an operator
  can, with a reason.

## 6. Partial consumption and release behaviour

* `consume_in(session, ...)` runs in the transaction that posts the fill. It requires an
  attached order and a journal entry whose asset-quantity movement equals the consumed
  quantity, in the reservation's direction (checked by the database). It frees
  `floor(remaining × q / remaining_q)` of each amount (the last consumption takes exactly
  what is left), keeps the remainder and updates the totals atomically.
* An over-fill is refused and rolls back with its posting.
* `release` gives back only the unconsumed remainder, with a resolution (NOT_SUBMITTED only
  when no order was ever attached; ORDER_CANCELLED / REJECTED / EXPIRED / COMPLETED;
  OPERATOR with an operator actor). Consumed quantity and the ledger position are never
  touched (tested: after a 40-share fill and a release, the ledger still holds 40 shares).

## 7. Exposure calculations

Capacity = limit − actual positions (6A ledger marked to fresh prices) − active
reservations' unconsumed remainders:

| Limit | Reservations counted |
|---|---|
| Loss allowance (daily, weekly, drawdown room) | reserved risk (potential loss at the stop) |
| Available capital | reserved capital (long: notional + entry costs + commission; short: collateral + entry costs) |
| Gross exposure, leverage | reserved gross exposure |
| Net exposure | same-direction reservations only (worst case) |
| Single asset, asset class, sector, correlated cluster | reserved exposure in the group (correlation rules as for positions) |
| Short exposure, margin usage | reserved short exposure, reserved margin |
| Liquidity | same-asset reserved exposure |
| Open positions | distinct reserved assets not yet held |

An opposite-direction reservation on the same asset is refused. Approval amounts are
rounded against Atlas (exposure, capital and margin up; risk is the verified loss ≤
budget). Foreign reservations are re-converted at the current rate when it is higher; with
no fresh rate, new approvals are INSUFFICIENT_DATA. Hand-worked (tested): 92 shares → risk
499.45, exposure 9,188.46, capital 9,190.84; 40 of 92 consumed → risk 282.30, exposure
5,193.48 remain; four reservations use 1,997.80 of the 2,000.00 allowance and the fifth is
rejected.

## 8. Failure-recovery behaviour

| Failure | Behaviour (tested) |
|---|---|
| Database interruption / connection loss mid-reservation | rollback; nothing persisted; retry reserves once |
| Caller's transaction rolls back after `reserve_in` | nothing persisted, counter unchanged |
| Worker crash after commit, retry | original result returned (also from a new service instance) |
| Worker crash, no order attached | reservation expires when due |
| Worker crash with an order attached | held past expiry, never released automatically |
| Duplicate submission / duplicate fill | no second reservation / no second consumption |
| Uncertain consumption state | `flag_uncertain`; held; only confirmed resolutions or an operator release it |
| Tampering (triggers disabled, projection edited) | reconciliation FAILED, CRITICAL `system_health_events`, approvals blocked |

`ReservationReconciler` replays all events independently of the trigger and checks the
projection, gapless numbering, account totals, approvals against same-transaction
assessments, idempotency records and each consumption against the asset quantity ledger.
CLI `reservations recover` = expiry by the rules + reconciliation.

## 9. Database migrations

Migration **0015**: tables `risk_reservations`, `risk_reservation_requests`,
`risk_reservation_events`, `risk_reservation_states`, `risk_reservation_approvals`,
`risk_reservation_reconciliations`, `system_health_events`; functions/triggers
`atlas_risk_reservation_insert_guard`, `_update_guard`, `_state_guard`, `_apply` (the state
machine), `_approval_guard`, `_request_guard`, deferred `atlas_risk_reservation_decided`;
append-only/no-delete/no-truncate triggers; CHECKs (quantity conservation, amounts within
approval, final states hold nothing, approved is sized, never above the proposal, …);
unique keys and partial unique indexes. 6B changes: `risk_exposure_snapshots` CHECK
`pending_exposure = 0` → `>= 0`; `risk_events.event_type` gains nine reservation types.
NUMERIC money/quantity columns, timestamptz everywhere. Verified: upgrade from 0014,
`alembic check` (no drift), downgrade to 0014 and upgrade again. Downgrade works only
before any 6C row exists (append-only history).

## 10. API and dashboard changes

GET only (the OpenAPI safety test asserts it): `/api/v1/risk/reservations` (`?status=`,
`?active=`), `/reservations/{id}` (events, approval record, idempotency records,
assessment), `/reservations/capacity` (reserved and available, live),
`/reservations/events` (`?event_type=` for rejections, expirations, releases,
consumptions), `/reservations/reconciliations`. `/risk/state` now returns the capacity
object as `reserved_exposure` and live `pending_exposure`.

CLI: `reserve` (levels, `--proposal-id`, `--decision-id`, `--idempotency-key`, `--strict`),
`reservations list|show|capacity|events|release|expire|reconcile|recover`. No consume
command (it needs a ledger fill, Phase 7).

Risk Center: new **Risk reservations** panel (total reserved risk, available risk budget
after reservations, reserved gross exposure, available exposure, active count, reservation
reconciliation status, blocked-approvals banner, active reservations table with proposed vs
approved and consumed vs remaining, lifecycle history with event filter); the headline's
"Reserved exposure" now shows the real figure. No execution control was added. Checked in
the browser against this worktree's backend (port 8011) and a separate preview database
`atlas_phase6c_preview` (TEST FIXTURE data): 499.45 reserved risk, 1,500.55 available risk
budget, 90,811.54 available exposure, proposed 500 / approved 92, history with the
rejection reasons and the NOT_SUBMITTED release; the filter works; no console errors.

## 11. Files created and modified

Created (6C): `backend/alembic/versions/0015_atomic_risk_reservations.py`,
`backend/app/database/models/reservations.py`,
`backend/app/risk/reservations/{__init__,service,exposure,reconciliation,queries}.py`,
tests `unit/test_reservation_assessment.py`, `integration/reservation_support.py`,
`integration/test_risk_reservations.py`, `integration/test_reservation_concurrency.py`,
`integration/test_reservation_api.py`; `frontend/src/pages/RiskReservationsPanel.tsx`;
`docs/architecture/reservations.md`, this report, the prompt.

Modified (relative to 6B): `app/risk/{assessment,models,state,service,enums,queries,runtime,cli}.py`,
`app/api/v1/risk.py`, `app/database/models/{__init__,risk}.py`,
tests `unit/{test_api,test_models,test_health_checks,test_risk_assessment}.py`,
`integration/test_risk_api.py`, frontend `pages/{RiskCenterPage.tsx,risk.test.tsx}`,
`services/risk.ts`, `types/risk.ts`, docs `architecture/{decisions,overview,risk}.md`,
`operations.md`, `README.md`.

Test changes to 6B (not weakening): the pending-exposure message test now expects "no
active risk reservation" instead of "Phase 6C"; the overview test reads the new capacity
object; the migration head is 0015; the table and API path lists include the new tables
and GET routes. Also added: `.claude/launch.json` (local preview servers, not part of the
deliverable).

No dependency added. No new environment variable.

## 12. Concurrency-test results

All against real PostgreSQL 16, each thread with its own pooled connection, released
together by a barrier (`test_reservation_concurrency.py`, 10 tests, all passing):

| Test | Workers | Result |
|---|---|---|
| Loss allowance (adversarial: every worker's own assessment PASSES first; trusting them would allocate 30 × 499.45) | 30 | exactly 4 approved; reserved risk 1,997.80 ≤ 2,000.00; 26 rejected with reasons |
| Gross exposure and capital (12 assets, sum of pre-assessed notional > 100,000) | 40 | gross ≤ 100,000, capital ≤ 100,000, single asset ≤ 20,000; < 100 left unused |
| Position count (limit 3) | 27 | exactly 3 assets |
| One asset, single-asset limit | 24 | ≤ 200 shares, reductions explained, < one share of slack |
| Long and short mixed | 24 | short ≤ 30,000, margin ≤ 50,000, gross and capital within limits |
| Same key from 20 workers | 20 | one reservation, one `created`, risk counted once |
| 12 keys × 3 concurrent senders | 36 | 12 reservations |
| 30 fills (with duplicates) + a cancellation | 31 | consumed + released = 92; ledger quantity = consumed = 5 × consumption events; refused fills rolled back their postings; 6A reconciliation PASSED |
| Reserve / release / expiry interleaved | 18 | totals exact, 3 released |
| Lock-free stale writers | 2 | second refused, SQLSTATE 40001 |

After every run: totals maintained by the trigger equal independent sums over the rows,
event numbering is gapless, no idempotency key appears twice, reservations posted nothing
to the ledger, and the reservation reconciliation PASSED.

## 13. Financial reconciliation results

* **Reservations never touch the ledger:** the journal-entry count of every stress-test
  account equals its opening funding plus the controlled test fills, nothing else.
* **Exposure moves, never duplicates:** after consumption the ledger quantity of the asset
  equals the consumed quantity; the risk view's pending exposure equals the unconsumed
  remainder; long exposure from the ledger (40 × 99.70 = 3,988.00) is unchanged by a
  release.
* **Exact decimal arithmetic:** four partial consumptions (30, 30, 31, 1 of 92) free exactly
  499.45 risk, 9,188.46 exposure and 9,190.84 capital in total; every partial step frees no
  more than its proportion (also enforced by the database).
* **Totals:** after every concurrency run, `risk_reservation_states` totals equal
  independent sums over the reservations; the reservation reconciliation PASSED; the 6A
  accounting reconciliation PASSED after the concurrent fills.
* **Tampering detected:** disabling the triggers and zeroing a reservation's remaining risk
  makes the reconciliation FAIL with PROJECTION_MISMATCH and TOTALS_MISMATCH, writes a
  CRITICAL system-health event and blocks the next approval; nothing held is released.
* Financial correctness is claimed only to the extent these tests cover.

### Test results

| Check | Result |
|---|---|
| Backend `pytest` (full, PostgreSQL) | **1869 passed, 1 skipped** (Redis integration, no `ATLAS_TEST_REDIS_URL`), 14 live deselected (23 min). 6B baseline 1791 + 57 new tests + 21 per-table schema checks for the 7 new tables |
| Re-run after final test-only type fixes | reservation, risk API and all unit tests: 1480 passed |
| New unit tests (pure assessment with reservations) | 23: each capacity counts reservations, reservation = position equivalence, net worst case, short/margin, positions, opposite reservations, valuation issues, seeded random sets (up to 720 draws; degenerate stops skipped) never exceeding allowance/gross/capital |
| New integration tests (PostgreSQL) | 10 concurrency/stress, 21 lifecycle/financial/guards/recovery, 3 API/CLI |
| `ruff format --check`, `ruff check` | clean |
| `mypy` (strict) | application code clean; the same 76 pre-existing errors in five older test files |
| `alembic upgrade head`, `check`, `downgrade 0014`, `upgrade head` | pass |
| Frontend `pnpm typecheck` / `pnpm test` / `pnpm build` | clean / 104 passed (15 files; 103 before + 1 new) / succeeds (pre-existing chunk-size warning) |

One frontend run, made while the full backend suite was loading the machine, reported a
failure that I did not capture; two later full runs passed (104/104). It is likely a timing
flake but is unconfirmed.

Defects found by these tests and fixed before the report: approved reservations did not
store their FX rate, and the CHECK accepted NULL (now `fx_rate IS NOT NULL AND fx_rate > 0`);
the reservation service imported a strategy enum (caught by the 6B separation test; the
value is now compared as stored).

## 14. Known limitations

* **Open-position risk is not tracked** (6B): stops of filled positions are not stored, so a
  reservation's risk stops counting against the loss allowance once consumed. The allowance
  therefore limits concurrent *pending* risk, not total open risk. Phase 7 should store
  position stops and count them.
* Domestic reservations are not re-marked after approval (they expire in 300 s unless an
  order holds them); a held reservation keeps its approval-time exposure. Foreign ones are
  revalued upward only.
* The database guarantees hold against application bugs and direct writes through normal
  roles; a table owner can disable triggers. The reconciliation detects the resulting
  inconsistencies; it cannot prevent them or repair them (blocked approvals need
  investigation, and the history is append-only).
* No scheduled worker: `reservations expire` and `reconcile` are CLI jobs (as 6A
  reconciliation and 6B monitoring are).
* Every accounting posting makes the 6A reconciliation not current, which makes the next
  assessment INSUFFICIENT_DATA until reconciliation runs (6B rule). Phase 7 must reconcile
  after fills, or the rule needs revisiting.
* The reservation TTL is the 6B assessment validity (300 s, a governed preference); there
  is no separate reservation TTL setting.
* One portfolio account per transaction; multi-account reservations would need an
  account-ordering rule (sorted by id) that is not implemented.
* The preview database `atlas_phase6c_preview` and the scratch `atlas_phase6c_gen` remain
  on the dev PostgreSQL server; they can be dropped.

## 15. Readiness for Phase 7

Ready, with the limitations above. Phase 7 should: create an order only from a
`risk_reservation_approvals` row (approved quantity, worst entry price, stop, valid until);
call `attach_order` before submitting; post each simulated fill with 6A and call
`consume_in` in the **same** transaction; release with a confirmed resolution only after
the order state machine confirms cancellation/rejection/expiry (spec 15.1); use
`flag_uncertain` when the broker state is unknown; run `expire_due` and the reconciliation
on a schedule; report execution errors through 6B `report_execution_safety`.
