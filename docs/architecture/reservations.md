# Atomic risk reservations (Phase 6C)

Reservations hold capacity (risk budget, exposure, capital, margin) for an approved trade
intent until the execution engine consumes or releases it. They are the only path from a
proposal to an order intent. PostgreSQL is authoritative; Redis is not involved. Decisions
D-148 to D-155 in [`decisions.md`](decisions.md); operations in
[`../operations.md`](../operations.md#risk-reservations-phase-6c).

**Paper only.** Nothing here submits, fills or executes anything, and no reservation posts
to the ledger. Phase 7 connects the execution interfaces to the paper broker.

## Modules

| Module | Role |
|---|---|
| `app/risk/reservations/service.py` | `ReservationService`: reserve, attach_order, consume, release, fail, flag_uncertain, expire_due |
| `app/risk/reservations/exposure.py` | Active remainders as `ReservedExposure` for the risk view |
| `app/risk/reservations/reconciliation.py` | `ReservationReconciler`: independent replay and cross-checks |
| `app/risk/reservations/queries.py` | Read models (API, CLI) |
| `app/database/models/reservations.py` | Tables (migration 0015) |
| `app/risk/assessment.py` | The 6B pure assessment, now counting reservations (engine 6C.1) |

## Reserve workflow (one transaction)

```text
validate key, actor, request hash
BEGIN (READ COMMITTED)
  1  SELECT portfolio_accounts ... FOR UPDATE            the account lock (6A/6B share it)
  2  INSERT risk_reservation_states ON CONFLICT DO NOTHING; SELECT ... FOR UPDATE
     -> event counter N (read before any capacity input)
  3  idempotency lookup: same key + same hash -> return the original; else 409
  4  expire due reservations (only those nothing depends on)
  5  identity: proposal expiry, strategy proposal / AI decision exists and matches,
     not superseded, no live reservation for the same intent, approvals not blocked
  6  6B evaluate(): ledger marked to fresh prices, breakers, loss state, plus the
     active reservations (exposure, capital, margin, risk)
  7  pure assess() -> PASS with a permitted quantity, or REJECTED / INSUFFICIENT_DATA /
     RISK_HALTED with every reason
  8  INSERT risk_assessments (+ checks), risk_reservations (PENDING),
     risk_reservation_requests, events CREATED and APPROVED|REJECTED (N+1, N+2),
     risk_reservation_approvals (order intent), risk_events, audit_logs
COMMIT   (deferred trigger: not PENDING, has its request record and approval record)
```

The event trigger (`atlas_risk_reservation_apply`) is the state machine: it checks the
transition, the numbering (per reservation and per account; a stale account number is
SQLSTATE 40001), that an approval equals the same-transaction assessment, that no breaker is
tripped and approvals are not blocked, that a consumption matches a ledger fill, that a
partial consumption frees no more than its proportion and that expiry is allowed; then it
updates the projection row and the account totals. Nothing else may update them.

## Lifecycle

| From | Event | To | Rule |
|---|---|---|---|
| (none) | CREATED | PENDING | inside the reserve transaction only |
| PENDING | APPROVED | APPROVED | reserves the assessment's permitted amounts |
| PENDING | REJECTED | REJECTED | holds nothing; reasons stored |
| APPROVED | ORDER_ATTACHED | APPROVED | before expiry; from now on never expires by itself |
| APPROVED, PARTIALLY_CONSUMED | CONSUMED | PARTIALLY_CONSUMED / CONSUMED | needs an attached order and a ledger fill of that quantity |
| APPROVED, PARTIALLY_CONSUMED | RELEASED | RELEASED | the unconsumed remainder; resolution confirmed (NOT_SUBMITTED only with no order) |
| APPROVED | EXPIRED | EXPIRED | due, no order attached, nothing consumed |
| APPROVED, PARTIALLY_CONSUMED | FAILED | FAILED | confirmed downstream failure; not while uncertain unless an operator |
| active | EXECUTION_UNCERTAIN | (unchanged) | capacity held for review |
| active | EXPIRY_DEFERRED | (unchanged) | due but an order depends on it: held, once |

## Amounts and rounding

At approval (functional currency, rounded against Atlas): risk = the verified potential loss
(<= budget); exposure = notional at the adverse entry fill, rounded up; capital = quantity x
(entry fill + entry costs + commission) + minimum commission (long) or margin collateral +
entry costs (short), rounded up; margin = collateral rate x notional, rounded up. A partial
consumption frees `floor(remaining x q / remaining_q)` of each amount (the remainder never
shrinks below its proportion); the last consumption or a release takes exactly what is left.

## Failure behaviour

| Situation | Result |
|---|---|
| Worker or database dies before COMMIT | PostgreSQL rolls back: no reservation, no events; a retry reserves once |
| Worker dies after COMMIT, retries | same key -> the original decision |
| Duplicate fill or release event | same key -> no new event |
| Over-fill | refused (ReservationError); the fill's posting rolls back with it |
| Worker dies before attaching an order | the reservation expires when due |
| Worker dies after attaching an order | held past expiry (EXPIRY_DEFERRED) until the execution state is resolved |
| Unexplained inconsistency | reconciliation FAILED, CRITICAL system-health event, approvals blocked |

## What is and is not guaranteed

Tested against PostgreSQL with up to 40 concurrent workers per account (see the 6C report):
no over-allocation of loss allowance, gross exposure, capital, single-asset, short and
position limits; no duplicate reservations under concurrent retries; exact totals; correct
partial consumption, release and rollback; recovery after interruption. The database
refuses direct updates and deletes, out-of-sequence events, approvals not computed in their
transaction, consumptions without a matching fill, and forbidden expiries.

Not guaranteed: someone with table-owner rights can disable the triggers (the reconciliation
detects the resulting inconsistencies, it cannot prevent them); open-position risk to the
stop is not tracked (6B), so consumed reservations stop counting against the loss allowance;
price moves after approval are not re-marked for domestic reservations (they expire in 300 s
unless an order holds them).
