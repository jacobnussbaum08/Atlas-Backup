ATLAS AI TRADER — PHASE 6C
ATOMIC RISK RESERVATIONS & PORTFOLIO INTEGRATION
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify Phase 6A accounting and Phase 6B risk management, including their migrations, tests, and financial calculations.
Stop if unresolved P0/P1 issues or accounting discrepancies exist.
Preserve all working functionality.
OBJECTIVE
Integrate the Portfolio Management and Risk Engines through a transactionally safe reservation system.
Prevent concurrent strategy workers from approving trades that collectively exceed available capital, risk budgets, or portfolio exposure limits.
PostgreSQL is the authoritative source of truth.
1. ATOMIC RESERVATION WORKFLOW
Implement this workflow:

1. Receive a standardized trade proposal.
2. Validate its identity and expiration.
3. Begin a PostgreSQL transaction.
4. Lock the relevant portfolio account or risk-state row using `SELECT ... FOR UPDATE`.
5. Read current equity, open positions, pending orders if applicable, and active reservations.
6. Recalculate risk and exposure under the transaction.
7. Apply all hard risk limits.
8. Reject or calculate an approved permissible quantity.
9. Persist the reservation and associated approval record atomically.
10. Record an audit event.
11. Commit.

All reservation paths for the same portfolio must follow a consistent locking protocol.
Do not rely on in-memory locks or Redis as the authority for financial reservations.
Use appropriate transaction isolation and lock ordering.
Avoid deadlocks.
2. RESERVED EXPOSURE
Reservations must account for:

* Risk budget.
* Notional exposure.
* Gross exposure.
* Net exposure.
* Available cash.
* Margin requirements.
* Asset exposure.
* Sector exposure.
* Correlated-cluster exposure.
* Maximum position count.

Calculate remaining capacity using both actual positions and outstanding reservations.
Do not double-count a reservation after it has been converted into an actual position.
For short proposals, account for short exposure, borrow restrictions, and margin requirements.
3. RESERVATION LIFECYCLE
Implement explicit states:

* PENDING
* APPROVED
* PARTIALLY_CONSUMED
* CONSUMED
* RELEASED
* EXPIRED
* REJECTED
* FAILED

Support:

* Reservation creation.
* Partial consumption.
* Full consumption.
* Release.
* Expiration.
* Rejection.
* Failure recovery.

Store append-only reservation events so every state transition is auditable.
Maintain a current-state projection for efficient queries.
Do not silently delete reservation history.
Do not automatically release a reservation simply because a worker timed out if the underlying order or execution state is uncertain.
Expiration must follow explicit, recoverable rules.
4. IDEMPOTENCY
Implement unique idempotency keys.
Repeated requests for the same reservation must not create duplicate reservations or consume risk capacity twice.
Use database uniqueness constraints and transaction handling.
Handle:

* Worker retries.
* Client retries.
* Duplicate events.
* Process restarts.
* Concurrent requests.
* Interrupted transactions.

A retry must return the original reservation result when appropriate.
5. PERMISSIBLE QUANTITY
Integrate position-sizing calculations from Phase 6B.
Recalculate quantities using current account state under the reservation transaction.
Respect:

* Risk per trade.
* Existing exposure.
* Pending reservations.
* Available capital.
* Gross and net limits.
* Asset-class limits.
* Correlation limits.
* Liquidity restrictions.
* Tick and lot sizes.
* Fees and estimated slippage.
* Short and margin requirements.

Never round quantities upward in a way that exceeds permitted risk.
Record the proposed quantity and approved quantity separately.
Explain every reduction or rejection.
6. PARTIAL CONSUMPTION AND RELEASE
Design the reservation interface for future execution integration.
When a reservation is partially consumed:

* Convert the consumed portion into actual position exposure.
* Retain the remaining reservation.
* Recalculate available capacity atomically.
* Preserve the complete reservation history.

When an order is canceled or rejected:

* Release only the appropriate unconsumed reservation.
* Preserve the reason and timestamp.
* Do not reverse actual filled exposure through a reservation release.

Phase 7 will connect these interfaces to the paper broker.
For this phase, test consumption and release through controlled test events, not actual broker execution.
7. RISK REVALIDATION
Before a reservation is approved, revalidate:

* Current equity.
* Current positions.
* Existing reservations.
* Active circuit breakers.
* Current prices and freshness.
* Required stop information.
* Asset eligibility.
* Relevant portfolio restrictions.

Do not trust risk calculations made by an asynchronous strategy worker before the database transaction begins.
A proposal is not automatically valid simply because it passed an earlier analytical assessment.
8. DATABASE DESIGN
Inspect existing Phase 6A and 6B models before creating migrations.
Implement or extend:

* Risk reservations.
* Reservation events.
* Reservation idempotency records.
* Portfolio risk-state records where needed.
* Approval records.
* Reservation audit records.

Add appropriate:

* Unique constraints.
* Foreign keys.
* Indexes.
* Transactional consistency.
* Immutable history.
* UTC timestamps.
* Decimal financial fields.

PostgreSQL must remain authoritative.
Redis may support transient coordination but must not authorize, store, or independently release financial reservations.
Reservations must not create fictitious posted financial transactions in the double-entry ledger. They represent reserved capacity, not actual cash movement or executed positions.
9. FAILURE RECOVERY
Handle:

* Database interruption during reservation.
* Worker crash.
* Duplicate submission.
* Transaction rollback.
* Process restart.
* Partial state transitions.
* Expired proposals.
* Uncertain consumption state.

Ensure recovery cannot create double reservations or silently release valid reservations.
Provide a reconciliation process that compares active reservations against the underlying portfolio and reservation events.
Unexplained inconsistencies must trigger a high-priority system-health event and block unsafe new approvals.
10. API AND DASHBOARD
Expose:

* Active reservations.
* Reservation details.
* Reserved risk.
* Reserved exposure.
* Available capacity.
* Reservation history.
* Rejections.
* Expirations.
* Release events.
* Reconciliation status.

Extend the Risk Center dashboard with:

* Total reserved risk.
* Available risk budget.
* Reserved gross exposure.
* Available exposure.
* Active reservation count.
* Reservation lifecycle history.

Do not add actual order execution controls.
11. CONCURRENCY AND FINANCIAL TESTING
This is the highest-priority testing requirement.
Create stress tests involving many simultaneous reservation requests against the same portfolio.
Verify:

* No over-allocation.
* No double spending.
* No duplicate reservations.
* No incorrect exposure totals.
* Correct idempotent retries.
* Correct partial consumption.
* Correct reservation release.
* Correct handling of rollback.
* Correct recovery after worker interruption.

Include adversarial tests where every worker independently calculates that sufficient exposure is available before requesting reservation.
Only the transactional reservation process may authorize the final allocation.
Also test:

* Long and short reservations.
* Multiple currencies.
* Concentration limits.
* Correlation limits.
* Daily and weekly circuit breakers.
* Drawdown restrictions.
* Decimal precision.
* Ledger reconciliation.

Run tests against actual PostgreSQL, not SQLite substitutes for concurrency-critical behavior.
Preserve all previous tests.
12. STRICT RESTRICTIONS
Implement Phase 6C only.
Do not implement:

* Paper brokerage.
* Order submission.
* Fill simulation.
* Market execution.
* Live brokerage.
* Automatic liquidation.

These belong to Phase 7.
The reservation system must expose well-defined interfaces for the future execution engine without implementing execution itself.
13. COMPLETION REPORT
Report:

1. Reservation architecture.
2. Database locking strategy.
3. Transaction isolation.
4. Idempotency implementation.
5. Reservation lifecycle.
6. Partial consumption and release behavior.
7. Exposure calculations.
8. Failure-recovery behavior.
9. Database migrations.
10. API and dashboard changes.
11. Files created and modified.
12. Concurrency-test results.
13. Financial reconciliation results.
14. Known limitations.
15. Readiness for Phase 7.

Do not claim the system prevents over-allocation unless concurrent database-backed stress tests demonstrate it.
Do not weaken tests or introduce artificial successful results.
FINAL INSTRUCTION: Implement Phase 6C only. Atomicity, financial integrity, idempotency, and concurrency safety take priority over performance or convenience.
