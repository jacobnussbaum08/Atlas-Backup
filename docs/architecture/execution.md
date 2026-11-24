# Paper execution, order and position management (Phases 7A-7B)

Simulated paper orders that execute approved Phase 6C reservations through an isolated,
PostgreSQL-backed paper broker. Decisions D-156 to D-163 in [`decisions.md`](decisions.md);
operations in [`../operations.md`](../operations.md#paper-orders-phase-7a).

**Paper only.** There is no brokerage connection, no real order submission, no trading
credential and no switch that could add one (spec 24.1). The paper broker has **no fill
algorithm** in Phase 7A: fills exist only as labelled controlled test events (the database
refuses any other fill source). Fill models are Phase 7B.

## Modules

| Module | Role |
|---|---|
| `app/execution/enums.py`, `state_machine.py` | Statuses, events, order types; the central transition table |
| `app/execution/validation.py` | Authorization, order validation, pre-execution revalidation, fill checks |
| `app/execution/service.py` | `ExecutionService`: submit, cancel, broker-event application, fills, expiry, recovery, outbox handlers |
| `app/execution/portfolio.py` | The fill → 6A posting contract (`PositionLedger`, `AccountingPositionLedger`) |
| `app/execution/reconciliation.py` | `ExecutionReconciler`: independent cross-check of orders, fills, ledger, reservations, broker |
| `app/execution/queries.py`, `cli.py`, `runtime.py` | Read models, operator CLI, wiring |
| `app/paper_broker/interface.py` | The `PaperBroker` protocol (submit, cancel, expire, status, events, health, start, pause, shutdown) |
| `app/paper_broker/controlled.py` | `ControlledPaperBroker`, the only implementation (simulated) |
| `app/events/outbox.py` | Transactional outbox: `enqueue`, `OutboxDispatcher` |
| `app/workers/paper_execution.py` | Worker: recovery at start, then heartbeat → dispatch → expiry |
| `app/database/models/execution.py` | Tables (migration 0016) |

## Order lifecycle

```text
(new) -CREATED-> PROPOSED -APPROVED-> APPROVED -ACCEPTED-> OPEN -FILL-> PARTIALLY_FILLED -FILL-> FILLED
                     \-REJECTED-> REJECTED   |  \-REJECTED (broker)        |   \-CANCEL_REQUESTED-> CANCEL_PENDING
                                             |-CANCEL_REQUESTED-> CANCEL_PENDING     -CANCEL_CONFIRMED-> CANCELLED
                                             |-EXPIRED-> EXPIRED                     -CANCEL_REJECTED-> OPEN | PARTIALLY_FILLED
                                             \-FAILED-> FAILED                       -FILL-> CANCEL_PENDING | FILLED
```

The full table is `state_machine.TRANSITIONS` (26 legal `(from, event, to)` triples; every
other combination is refused). The database enforces the same table through
`atlas_order_transition_allowed` inside the `order_events` trigger; a unit test compares
the two, an integration test compares the database function with Python for all 1,100
combinations.

* **PROPOSED** exists only inside the submission transaction (a deferred trigger refuses
  to commit it).
* **APPROVED** = authorized, revalidated, reservation attached (6C `ORDER_ATTACHED`), and
  an `order.submit` outbox event written in the same transaction.
* ACCEPTED, FILL, CANCEL_CONFIRMED and CANCEL_REJECTED can only come from a paper broker
  event; EXPIRED, REJECTED and FAILED can come from the engine only while the broker is not
  working the order.
* A cancellation is final only when the broker confirms it; until then the order is
  CANCEL_PENDING, may still fill, and its reservation is not released.

Every change is an append-only `order_events` row (gapless per order) with timestamp,
source (EXECUTION_ENGINE, PAPER_BROKER, OPERATOR, EXPIRY, RECOVERY), actor and reason. The
`orders` row is a projection only that trigger may change.

## Submission (one transaction)

```text
lock portfolio_accounts row (6C protocol step 1), then risk_reservation_states (step 2)
idempotency: execution_requests.idempotency_key (same request -> original result; else 409)
authorize submitter: operator:<name> or system:execution[-...] only
validate ENTRY: reservation of this account, APPROVED, no order attached, not expired,
   PASS_FOR_RESERVATION assessment + approval record, origin still valid (strategy
   proposal not expired/superseded, AI decision PRIORITIZE_FOR_REVIEW and unexpired,
   operator intent only for operators), declared asset/side/origin equal to the record,
   quantity <= approved and on the lot grid, prices per type and on the tick grid,
   no limit/stop beyond the approval's worst entry price, expiry within 7 days
validate EXIT: parent ENTRY order of this account with fills; quantity <= what its exits
   may still close and <= the ledger position; reservation = the parent's (link only)
   -> refused: execution_requests row (REFUSED + reasons), nothing else; reservation untouched
INSERT orders (PROPOSED) + CREATED
revalidate (ENTRY): account active, no circuit breaker tripped, approvals not blocked by a
   failed reservation reconciliation, reservation still active
   -> REJECTED + reservation released NOT_SUBMITTED
   -> 6C attach_order_in, APPROVED, outbox order.submit
COMMIT (deferred: not PROPOSED; an approved ENTRY order has its reservation attached)
```

Exits are not blocked by breakers: halting new entries must not prevent reducing a
position (spec 24.2).

## Broker events and fills

The broker answers with `paper_broker_events` (gapless per book row), each written with
an outbox event. The engine applies them **strictly in broker sequence** (`orders.
applied_broker_sequence`; the trigger refuses a gap or a repeat): a delivery for event n
first applies every earlier unapplied event; a re-delivery does nothing.

A fill is one transaction: `check_fill` (state, quantity grid, limit, entry protection) →
6A posting (`long_purchase` / `long_sale` / `short_open` / `short_cover`, commission and
fees passed through) → 6C `consume_in` with that journal entry (ENTRY only) →
`order_fills` row → `FILL` event → `accounting.reconcile` outbox event. The `order_fills`
trigger checks the fill equals its broker event, the journal entry moved exactly the
filled quantity in the order's direction, and (ENTRY) the consumption was written in the
same transaction. When the order becomes final, the reservation's unconsumed remainder is
released with the matching resolution (FILLED → ORDER_COMPLETED for any unordered
remainder, CANCELLED → ORDER_CANCELLED, EXPIRED → ORDER_EXPIRED, REJECTED →
ORDER_REJECTED, never attached → NOT_SUBMITTED) or failed (FAILED). Consumed quantity is in
the ledger and is never touched.

## Outbox

`outbox_events` rows are written in the transaction that changed the state. The dispatcher
claims one due row with `FOR UPDATE SKIP LOCKED`, runs its handler in a savepoint, and
marks it DELIVERED in the same commit. A handler error rolls back the savepoint only: the
attempt and error are recorded and the next attempt is scheduled (5 s, 10 s, 20 s, …,
capped at 10 min). After 5 attempts the row is DEAD and the handler's recovery runs:

| Topic | Handler | When dead |
|---|---|---|
| `order.submit` | broker submit, apply events | broker never received it → FAILED + reservation failed (confirmed); else apply its events |
| `order.cancel` | broker cancel, apply events | order stays CANCEL_PENDING, reservation flagged uncertain (held), EXECUTION_SAFETY tripped |
| `paper_broker.event` | apply pending broker events | reservation flagged uncertain, EXECUTION_SAFETY tripped |
| `accounting.reconcile` | 6A reconciliation after fills | HIGH system-health event |

A crash before COMMIT leaves the row PENDING and the handler's work rolled back.
Duplicates are suppressed by `dedupe_key` and by idempotent handlers.

## Locking

Account row → order row → broker book row → 6C reservation state → reservation rows (the
6C protocol, with the order and book in between). Every per-account writer takes the
account lock first, so no lock cycle exists. The broker has no global counter (sequences
are per book row), so accounts do not serialize on each other.

## Integration contracts

* **6C:** `attach_order_in`, `consume_in`, `release_in`, `fail_in`, `flag_uncertain_in` run
  in the order's transaction (the `*_in` variants were added in 7A; the public 6C methods
  are unchanged).
* **6A:** fills post through the existing templates; positions, cash, average cost,
  realized P/L and costs are 6A's. In 7A the source is CONTROLLED_TEST, which 6A accepts
  only on CONTROLLED_TEST accounts (D-160).
* **6B:** revalidation reads `breaker_statuses` and the reservation reconciliation block;
  dead cancellations and broker events trip EXECUTION_SAFETY.

## Guarantees and limits

Tested against PostgreSQL (see the 7A report): legal transitions only; one order per
reservation under concurrent submission; one broker book per order under concurrent
dispatchers; every broker event applied once, in order; quantity conservation between
order, fills, reservation and ledger; recovery after crashes, lost deliveries and broker
outages. Not guaranteed: a table owner can disable triggers (the execution, reservation
and 6A reconciliations detect the resulting inconsistencies); no fill model, slippage,
spread, or trailing-stop tracking exists yet (Phase 7B).


## Phase 7B: simulated execution and position management

Decisions D-164 to D-172. **Simulated execution only**: fills are derived from stored market
data by the isolated paper broker; they are not real executions and are not guaranteed to
match what a real market would have filled.

### Modules

| Module | Role |
|---|---|
| `paper_broker/execution_model.py` | Versioned, content-addressed assumptions (`ExecutionModel`, per asset class) |
| `paper_broker/market.py` | The stored observations an evaluation may see (VALID, available, after arrival) |
| `paper_broker/fill_model.py` | Pure fill decisions per order type; costs |
| `execution/simulation.py` | One transaction per working order: evaluate, trigger, fill, apply |
| `execution/positions.py` | Positions from applied fills and final orders (in the fill transaction) |
| `execution/protection.py` | Pure protection decisions (binding level, stop first, expiry, trailing) |
| `execution/monitor.py` | The position monitor: marks, protection, exits, take-profits, borrow, valuations |
| `execution/position_queries.py` | Read models: positions, analytics, model versions |
| `workers/position_monitor.py` | The monitoring worker |

### One simulated evaluation (one transaction)

```
lock account -> order -> book row
skip when nothing became available since the last evaluation
view = stored quotes/trades/bars (VALID, available, after arrival + latency, after cursors)
decision = fill_model.evaluate(order, view, model of the book row)
TRIGGERED broker event (stop types), when the stop is reached
before a fill: revalidate_fill (limit, tick, lot, worst entry price, breakers, position)
FILL broker event (SIMULATION, model id) + execution_fill_details + cursors advanced
apply broker events: 6A posting (SIMULATED_EXECUTION) + 6C consume + order_fills
                     + order FILL event + position + accounting.reconcile outbox
execution_attempts row when the outcome or reason changed, a stop triggered or a fill
```

### Position lifecycle

```
first entry fill          -> OPENED, PROTECTION_SET (approved stop/target, invalidation, expiry)
later entry fills         -> INCREASED
monitor: no trigger       -> resting TAKE_PROFIT for the quantity no exit covers
monitor: stop/trailing/invalidation reached, expiry, close request
                          -> TRIGGERED; entry remainder cancelled; take-profits cancelled (OCO);
                             then one MARKET exit for what is held (EXIT_SUBMITTED)
exit fills                -> REDUCED (realized P/L from the 6A movement)
exit order final          -> EXIT_ENDED
nothing open, entry final -> CLOSED; short borrow paid (COSTS_SETTLED)
```

### Guarantees and limits

* Each observation's liquidity is used once per order; a crash rolls back the whole
  evaluation; concurrent simulators serialize on the account lock and find the cursors moved.
* Exits never exceed what the position holds; an entry never fills beyond the reserved worst
  entry price; reservations are consumed only by entry fills and released only for the
  unfilled remainder once the order is final (7A).
* The execution reconciler also checks every position against its fills and the 6A ledger,
  and every simulated fill against its breakdown and its SIMULATED_EXECUTION posting.
* Limitations: IEX's small share of volume (liquidity understated), unknown queue position,
  bar-based fills without an intrabar path, the bar fallback's assumed spread, blended average
  cost for two positions in one asset, protection evaluated on regular-session equity data.
