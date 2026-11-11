# Phase 7A — Paper execution engine foundation and order management: completion report

Prompt: [`docs/prompts/phase-7a-paper-execution.md`](../prompts/phase-7a-paper-execution.md).
Design: [`docs/architecture/execution.md`](../architecture/execution.md). Decisions:
D-156 to D-163 in [`decisions.md`](../architecture/decisions.md).

**Paper only.** Every order is a simulated paper order handled by the isolated Atlas paper
broker. There is no brokerage connection, no real order submission, no trading credential
and no switch that could add one. Phase 7A has **no fill algorithm**: every fill in this
phase is a labelled controlled test event (the database refuses any other fill source).

## 0. Preflight

* **Base.** This worktree started at `main` (Phase 4B). Phases 5A–6C were committed on the
  unmerged branch `claude/atomic-risk-reservations-portfolio-f0ee6c` (`0023e5e`, which
  contains 5A/5B, 6A, 6B and 6C). The branch was fast-forwarded to it (no local commits
  existed, nothing was lost); the 6C worktree was clean.
* **Phases 1–6C verified before any 7A code** (full backend suite against PostgreSQL 16):
  **1869 passed, 1 skipped** (Redis, no `ATLAS_TEST_REDIS_URL`), 14 live deselected, in
  18 min 37 s — identical to the 6C report's final count. No failing test, no open P0/P1
  finding, no accounting discrepancy, no reservation-integrity failure (the 6A
  double-entry, 6B breaker and 6C reservation reconciliation and concurrency suites all
  passed). The gate passed.

## 1. Order-management architecture

```text
execution coordinator (system:execution...) / operator CLI          strategies, AI, LLM:
     |  OrderRequest (reservation id or parent order id, type, quantity, levels)   no access
     v                                                                     (static test)
ExecutionService.submit -- one transaction, READ COMMITTED --------------------------+
  1 portfolio_accounts FOR UPDATE   2 risk_reservation_states FOR UPDATE (6C protocol) |
  3 idempotency (execution_requests.idempotency_key)                                   |
  4 authorize submitter; validate against the reservation / parent order, instrument   |
    spec, tick grid, approval quantity, entry protection, expiry                       |
       refused -> execution_requests REFUSED (+reasons); nothing else changes          |
  5 orders PROPOSED + CREATED   6 revalidate: breakers, reconciliation block, live     |
       -> REJECTED + reservation released NOT_SUBMITTED                                 |
       -> 6C attach_order_in + APPROVED + outbox "order.submit"           COMMIT <------+
         database: insert guard (against the reservation/approval), event trigger =
         state machine + projection, deferred: never PROPOSED, approved => attached

OutboxDispatcher (worker; FOR UPDATE SKIP LOCKED; handler in a savepoint; retry/DEAD)
  order.submit   -> ControlledPaperBroker.submit_order -> ACCEPTED / REJECTED event
  order.cancel   -> ControlledPaperBroker.cancel_order -> CANCEL_CONFIRMED (or tombstone)
  paper_broker.event -> apply broker events in broker sequence:
       FILL: 6A posting + 6C consume_in + order_fills + FILL event + accounting.reconcile
       final order -> release (or fail) the reservation's unconsumed remainder
  accounting.reconcile -> 6A reconciliation (POST_FILL)
```

New packages `app/execution` (state machine, validation, service, portfolio contract,
reconciliation, read models, CLI, runtime), `app/paper_broker` (interface + controlled
simulator), `app/events/outbox.py`, worker `app/workers/paper_execution.py`.

Order types: MARKET, LIMIT, STOP, STOP_LIMIT, TAKE_PROFIT (exit only: a limit at the
target), TRAILING_STOP (amount or percent). Purposes: ENTRY (executes a reservation,
consumes it by fills) and EXIT (reduces an ENTRY order's filled position, consumes nothing;
D-157). Standard fields: id, portfolio, asset, side + position direction, type, quantity,
limit/stop/trail levels, filled/remaining/average fill, reservation, assessment, strategy
proposal / AI decision, parent, broker order id, submission and expiration timestamps,
status and reason. NUMERIC everywhere; the instrument's lot size and tick size are copied
onto the order and enforced for quantities, prices and fills.

## 2. State machine and transitions

Statuses: PROPOSED, APPROVED, OPEN, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED,
EXPIRED, REJECTED, FAILED. 26 legal `(from, event, to)` triples
(`app/execution/state_machine.py`), the same table frozen in migration 0016:

| From | Event | To |
|---|---|---|
| (new) | CREATED | PROPOSED |
| PROPOSED | APPROVED / REJECTED | APPROVED / REJECTED |
| APPROVED | ACCEPTED, REJECTED, CANCEL_REQUESTED, EXPIRED, FAILED | OPEN, REJECTED, CANCEL_PENDING, EXPIRED, FAILED |
| OPEN | FILL | PARTIALLY_FILLED or FILLED (FILLED exactly when nothing remains) |
| OPEN, PARTIALLY_FILLED | CANCEL_REQUESTED, EXPIRED, FAILED | CANCEL_PENDING, EXPIRED, FAILED |
| PARTIALLY_FILLED | FILL | PARTIALLY_FILLED or FILLED |
| CANCEL_PENDING | FILL | CANCEL_PENDING or FILLED |
| CANCEL_PENDING | CANCEL_CONFIRMED | CANCELLED |
| CANCEL_PENDING | CANCEL_REJECTED | OPEN (nothing filled) or PARTIALLY_FILLED |
| CANCEL_PENDING | ACCEPTED (late acknowledgement) | CANCEL_PENDING |
| CANCEL_PENDING | EXPIRED, FAILED | EXPIRED, FAILED |

Final statuses have no transition. Every change is an append-only `order_events` row
(gapless per order) with timestamp, source, actor and reason; `orders` is a projection only
the event trigger may update; nothing deletes or rewrites history. ACCEPTED, FILL,
CANCEL_CONFIRMED and CANCEL_REJECTED must reference a paper broker event, and the engine
cannot expire/reject/fail an order the broker is working. Spec 15.1's PENDING_RISK and
APPROVED_RESERVED map to the prompt's PROPOSED and APPROVED (D-156).

## 3. Phase 6C integration

* **Only from reservations:** an ENTRY order requires an APPROVED reservation of the same
  account with no order attached, unexpired, a PASS_FOR_RESERVATION assessment and its
  approval record; expired, released, rejected, consumed or foreign reservations are
  refused (tested each). The database insert guard checks the same against the
  reservation and approval (account, asset, direction, quantity ≤ approved, protection =
  worst entry price, assessment and origin).
* **Originating intent:** strategy proposal not expired or superseded; AI decision
  PRIORITIZE_FOR_REVIEW and unexpired; operator-sourced reservations only by an operator;
  declared ids must equal the reservation's.
* **Attach in the same transaction** (`attach_order_in`): from then on the reservation
  never expires by itself (6C EXPIRY_DEFERRED; tested).
* **Revalidation before execution:** breakers, reservation reconciliation block, account
  status and reservation liveness when the order is approved; limit, entry protection,
  quantity/tick grid and breakers before any fill is recorded (`revalidate_fill`, which the
  7B fill model must use).
* **Consumption by actual fills:** each fill posts to 6A and calls `consume_in` with that
  journal entry in one transaction; the 6C trigger and the `order_fills` guard both check
  that the entry moved exactly the filled quantity. Partial fills consume proportionally.
* **Release only genuinely unfilled exposure:** at FILLED (any unordered remainder,
  ORDER_COMPLETED), CANCELLED (only after the broker confirms), EXPIRED, REJECTED
  (ORDER_REJECTED, or NOT_SUBMITTED if never attached), FAILED (6C `fail`). Consumed
  quantity stays in the ledger.
* **No double counting:** after a fill the ledger holds the filled quantity and the
  reservation the remainder only (e.g. 40 of 92: ledger 40, reservation 52 remaining; after
  completion of a 50-share order: ledger 50, reservation released 42, active count 0,
  reserved risk 0).
* 6C `ReservationService` gained `attach_order_in`, `release_in`, `fail_in`,
  `flag_uncertain_in` (the public methods now delegate to them; behaviour unchanged, all
  6C tests pass).

## 4. Idempotency implementation

* **Submissions:** `execution_requests.idempotency_key` UNIQUE with the request SHA-256,
  looked up after the account lock. Same key + same request returns the original order
  (`created=False`), same key + different request is a 409. `orders.idempotency_key` and
  `execution_request_id` UNIQUE too.
* **One order per reservation:** partial unique index on `orders(reservation_id) WHERE
  purpose = 'ENTRY'`, the validation's DUPLICATE_ORDER check under the lock, and 6C's one
  ORDER_ATTACHED.
* **Commands:** `order_requests.idempotency_key` UNIQUE (cancellation); the 6C commands
  use derived keys (`order:<id>:attach|release|fail|uncertain`, `fill:<broker event>:consume`).
* **Broker:** UNIQUE `client_order_id` (a duplicate submission returns the original
  answer); controlled test events keyed by UNIQUE `external_ref` (a retry with other values
  is refused); fills UNIQUE per broker event and per journal entry; 6A `event_id =
  paper-fill:<broker event>`.
* **Outbox:** UNIQUE `dedupe_key`; every handler re-reads state under the order lock and
  does nothing when the work is done.

## 5. Transactional event handling

`outbox_events` rows are written in the transaction that changed the state. The
dispatcher claims one due row with `FOR UPDATE SKIP LOCKED`, runs the handler in a
savepoint and marks it DELIVERED in the same commit. A handler error rolls back only the
savepoint; the attempt, error and next attempt time (5 s, 10 s, 20 s, … ≤ 10 min) are
recorded; after 5 attempts the row is DEAD and a per-topic recovery runs (dead submission →
confirmed FAILED if the broker never received it; dead cancellation or broker event →
reservation held as uncertain + EXECUTION_SAFETY tripped). A process crash before COMMIT
leaves the row PENDING and the work rolled back. Broker events are applied strictly in
broker order (D-161). Recovery (`recover`, worker start) restarts the broker, applies
broker events whose delivery was lost, resolves dead submissions/cancellations and expires
due orders. PostgreSQL is the only source of truth; Redis is not used.

## 6. Paper broker interfaces

`app/paper_broker/interface.py` — `PaperBroker` protocol: `submit_order`, `cancel_order`,
`expire_order`, `order_status`, `events_after` (fill and partial-fill events),
`health`, `start` (restart + recovery report), `pause`, `heartbeat`, `shutdown`; errors
`BrokerUnavailableError` (retried) and `BrokerRefusedError`. Separate from the Alpaca
market-data adapter; a static test checks that `app.execution`, `app.paper_broker` and
`app.events` import no provider, HTTP, socket or websocket client and name no brokerage
endpoint.

`ControlledPaperBroker` (the only implementation): its own book (`paper_broker_orders`,
`simulated` pinned to true) and gapless events; deterministic acknowledgement, rejection
(expired before receipt), cancellation (IMMEDIATE, or MANUAL for tests: acknowledge, then a
controlled confirmation), tombstone cancellation of an order never received, expiry of due
working orders; `inject_fill` / `confirm_cancel` record CONTROLLED_TEST events. Fill model:
"NONE (Phase 7A: controlled test events only)".

## 7. Database migrations

Migration **0016** (`0016_paper_execution_engine.py`): tables `execution_requests`,
`orders`, `order_events`, `order_fills`, `order_requests`, `paper_broker_state`,
`paper_broker_orders`, `paper_broker_events`, `outbox_events`; functions
`atlas_order_transition_allowed`, `atlas_order_insert_guard`, `_update_guard`,
`atlas_order_event_apply` (state machine + projection), `atlas_order_fill_guard`,
`atlas_paper_broker_order_guard`, `atlas_paper_broker_event_guard`,
`atlas_outbox_update_guard`, deferred `atlas_order_decided`; append-only triggers on
`execution_requests`, `order_events`, `order_fills`, `order_requests`,
`paper_broker_events`; no-delete/no-truncate on `orders`, `paper_broker_orders`,
`paper_broker_state`, `outbox_events`. CHECKs: quantity conservation, price fields per order
type, purpose/side/direction consistency, take-profit is an exit, filled/partial
consistency, closed iff final, expiry after submission, fills only from CONTROLLED_TEST,
simulated = true. Indexes for status/expiry sweeps, account history, parent orders, outbox
due rows. NUMERIC money/quantities, timestamptz everywhere, foreign keys RESTRICT. Verified:
upgrade from 0015, `alembic check` (no drift), downgrade to 0015, upgrade again. Downgrade
works only before any 7A row exists (append-only history).

## 8. API endpoints

GET only (the OpenAPI safety test now lists them and asserts GET-only):
`/api/v1/paper-orders` (`?status=`, `?group=open|closed|filled|cancelled|rejected|expired|failed`),
`/paper-orders/open`, `/paper-orders/summary`, `/paper-orders/events` (`?event_type=`),
`/paper-orders/requests` (`?decision=REFUSED`), `/paper-orders/{id}` (events, fills,
reservation linkage, execution request, broker book and events, outbox),
`/paper-orders/{id}/events`, `/api/v1/paper-broker/status`. Every payload carries
`simulated: true` / a notice. CLI `python -m app.execution.cli` (reads; operator submit,
exit, cancel, controlled test fill; dispatch, expire, recover, reconcile, broker
start/pause/stop) and the worker `python -m app.workers.paper_execution`.

## 9. Dashboard additions

New **Paper Orders** page (`/paper-orders`, navigation "Paper Orders"): a permanent
"Simulated orders only" notice; the paper broker (run state, accepting orders, working
orders, fill model "None", outbox backlog, heartbeat); counts by status and refused
requests; the orders table with Open / All / Filled / Cancelled / Rejected / Expired /
Failed filters; per-order detail (event history with transitions, sources, actors and
reasons; fills labelled "Controlled test" with their journal entries; reservation linkage:
role, status, approved / consumed / released / held; broker book); recent events; refused
submissions with reasons; account selector (primary / controlled test). No submit, cancel
or other trading control. The header label now reads "No real order execution · simulated
paper orders only". Checked in the browser against this worktree's backend (port 8012) and
a separate preview database `atlas_phase7a_preview` (TEST FIXTURE data: partial fill, full
fill with costs, confirmed cancellation, take-profit exit, a refused `llm:claude`
submission and an entry rejected under the kill switch); at 375 px width no horizontal
page scroll; no console errors after a fresh load. That check found a real defect, fixed
before this report: messages showed quantities as `1E+1` / `3E+1` (Decimal exponent form).

## 10. Files created and modified

Created: `backend/alembic/versions/0016_paper_execution_engine.py`,
`backend/app/database/models/execution.py`,
`backend/app/execution/{enums,state_machine,models,validation,service,portfolio,reconciliation,queries,runtime,cli}.py`,
`backend/app/paper_broker/{interface,controlled}.py`, `backend/app/events/outbox.py`,
`backend/app/workers/paper_execution.py`, `backend/app/api/v1/paper_orders.py`; tests
`unit/test_order_state_machine.py`, `unit/test_execution_isolation.py`,
`integration/execution_support.py`, `integration/test_paper_orders.py`,
`integration/test_execution_recovery.py`, `integration/test_execution_concurrency.py`,
`integration/test_paper_orders_api.py`; frontend `pages/PaperOrdersPage.tsx`,
`pages/orders.test.tsx`, `services/orders.ts`, `types/orders.ts`; docs
`architecture/execution.md`, this report, the prompt.

Modified: `backend/app/risk/reservations/service.py` (`*_in` command variants),
`backend/app/database/models/__init__.py`, `backend/app/api/v1/__init__.py`,
`backend/app/main.py` (execution context), tests `unit/test_api.py` (8 GET-only paths),
`unit/test_models.py` (9 tables), `unit/test_health_checks.py` (head 0016),
`integration/test_discovery_api.py` (the route guard allows the read-only paper prefixes,
GET only); frontend
`App.tsx`, `navigation.ts`, `layouts/DashboardLayout.tsx`, `App.test.tsx` (header label);
docs `architecture/{decisions,overview}.md`, `operations.md`, `README.md`. Also added:
`.claude/launch.json` (local preview servers, not part of the deliverable).

No dependency added. No environment variable added (order lifetime and outbox limits are
documented constants, D-159/D-162).

## 11. Unit-test results

| Suite | Result |
|---|---|
| `unit/test_order_state_machine.py` | 1,114 passed: all 26 valid transitions; every one of the 1,074 other `(from, event, to)` combinations refused; final statuses have no exit; quantity-dependent targets (FILLED iff nothing remains, CANCEL_REJECTED → OPEN iff unfilled); zero/negative/over-fills refused; the migration's frozen table equals the Python table |
| `unit/test_execution_isolation.py` | 61 passed: execution, paper broker and outbox packages import no provider/HTTP/socket client and name no brokerage endpoint; strategies, AI, ML, intelligence and news import nothing from them; no setting selects a broker or live mode; submitter allow/deny matrix; price fields per order type; fill checks (limit, protection, grid, over-fill, state); fills map to the four existing 6A templates only |
| Whole unit suite | **2,644 passed** (incl. the updated OpenAPI GET-only test, 27 new per-table schema checks, migration head 0016) |
| `ruff format --check`, `ruff check` | clean |
| `mypy` (strict) | application code clean; the same 76 pre-existing errors in five older test files as 6C |

## 12. Integration-test results

All against real PostgreSQL 16 (each test on its own migrated database):

| Suite | Result |
|---|---|
| `test_paper_orders.py` | 43 passed: full lifecycle with partial fills and exact reservation consumption; event history (timestamp, source, reason, gapless); all six order types; limit and protection enforced on fills; 11 refusal cases leaving the reservation untouched; expiry bounds; strategies/LLMs refused; coordinator only for strategy reservations; missing / foreign / rejected / released / expired / consumed reservations refused; duplicate submissions and duplicate test events; revalidation under the kill switch (entry REJECTED + NOT_SUBMITTED release, exit allowed); no entry fill while halted; cancellation released only on confirmation (fills during CANCEL_PENDING kept); cancellation before receipt (tombstone); a full fill overtaking a cancellation; expiry at and before the broker; partial-use release; exits with take-profit and trailing stop, realized P/L 275.00, costs; short entry and cover; database refuses direct UPDATE/DELETE, illegal transitions, unbrokered ACCEPTED, engine-side finish of a working order, non-test fills, committing PROPOSED or unattached orders; DB transition function equals Python for all 1,100 combinations; each entry fill's consumption in the same transaction. Execution, 6C and 6A reconciliations PASSED after each lifecycle test |
| `test_execution_recovery.py` | 9 passed: outbox retry with backoff then single delivery; dead submission → confirmed FAILED + reservation failed + health event; crash mid-handler (BaseException) rolls everything back and a new process submits once; restart with lost deliveries recovered (12 shares applied, idempotent second recovery); out-of-order delivery applied in broker order; caller rollback leaves nothing and the key is reusable; duplicate outbox writes suppressed and redundant deliveries no-ops; unconfirmed cancellation holds capacity (UNCERTAIN), trips EXECUTION_SAFETY, recovery then cancels and releases; submissions wait while the broker is stopped |
| `test_paper_orders_api.py` | 3 passed: every GET endpoint against seeded data (filters, detail, reservation linkage, events, summary, refused requests, broker status, 404); no write method on the order routes; CLI reads, reconcile, recover, operator-only cancel, dispatch, broker status |
| **Full backend suite** | **3,129 passed, 1 failed, 1 skipped** (Redis), 14 live deselected, 21 min 31 s. The one failure was the Phase 3A guard `test_no_order_or_trading_routes_exist`, which forbade any path containing "order"; it now allows only the read-only paper-order/paper-broker prefixes and asserts they are GET-only (re-run: passed). One test (`test_no_entry_fill_is_recorded_while_new_entries_are_halted`) was added after that run started; the 7A files were re-run afterwards: 1,235 passed. Expected total: 1,869 (6C) + 1,262 new = 3,131 |
| Frontend `pnpm typecheck` / `pnpm test` / `pnpm build` | clean / **108 passed** (16 files; 104 before + 4 new Paper Orders tests) / succeeds (pre-existing chunk-size warning) |

## 13. Concurrency-test results

`test_execution_concurrency.py`, 5 passed; threads with their own pooled connections,
released together by a barrier:

| Test | Workers | Result |
|---|---|---|
| Same submission, same key | 20 | one order, one execution request, 1 created / 19 replays, one ORDER_ATTACHED |
| Competing keys for one reservation | 20 | exactly one order; 19 refused DUPLICATE_ORDER; reservation's order_ref is the winner |
| 12 reservations × 3 senders, then 8 dispatcher workers racing over the outbox | 36 + 8 | 12 orders, 12 broker books, 12 ACCEPTED events, no failed/dead/pending outbox rows; all reconciliations PASSED |
| 15 distinct fills each sent twice + a cancellation | 31 | each broker event applied once; filled = Σ fills = ledger position = consumed; consumed + released = 92; order CANCELLED; late fills refused, not recorded; all reconciliations PASSED |
| One set of 4 broker events delivered by many workers | 16 | applied exactly once in total (Σ applied = 4); order FILLED 40; reservation consumed 40, released 52 |

No deadlock or unhandled serialization failure occurred in any run.

## 14. Known limitations

* **No fill model** (by design): fills are controlled test events only, on
  CONTROLLED_TEST accounts. Spread, slippage, liquidity, gaps, delays, commissions models,
  stop/limit triggering and trailing-stop tracking are Phase 7B; the trailing-stop order
  stores its offset but nothing trails it yet.
* **The primary paper account cannot be filled** in 7A: 6A accepts the CONTROLLED_TEST
  source only on test accounts (D-160). Orders can be created, cancelled and expired there.
* **Open-position risk is still not tracked** (inherited from 6B/6C): stops of filled
  positions are not stored, so a consumed reservation stops counting against the loss
  allowance; protective exits are not created automatically.
* **No breaker sweep:** when a breaker trips, resting entry orders are not cancelled
  automatically; their fills are refused (`revalidate_fill`) and operators can cancel them.
* **Account currency:** fills post in the instrument's currency through 6A; FX for foreign
  instruments is 6A's existing path, not exercised by 7A tests (USD fixtures).
* **Worker scheduling:** the worker is not added to `docker-compose.yml`; run it (or the
  CLI jobs) explicitly.
* The database guarantees hold against application bugs and direct writes through normal
  roles; a table owner can disable triggers (the three reconciliations detect it).
* The preview database `atlas_phase7a_preview` and the scratch `atlas_phase7a_gen` remain
  on the dev PostgreSQL server; they can be dropped.

## 15. Readiness for Phase 7B

Ready. Phase 7B should: implement a fill model that decides fills from documented market
data and execution assumptions, calls `ExecutionService.revalidate_fill` and records fills
through the `PaperBroker` interface (a new SIMULATED source: extend `BrokerEventSource`, the
two CHECKs that accept only CONTROLLED_TEST, and `AccountingPositionLedger`/6A
`EntrySource` with an explicit migration); model spreads, slippage, commissions and fees as
execution costs; track stop/limit triggers and trailing stops; consider a breaker sweep that
cancels resting entries, and storing position stops so open risk counts against the loss
allowance.
