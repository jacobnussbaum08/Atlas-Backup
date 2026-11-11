ATLAS AI TRADER — PHASE 7A
PAPER EXECUTION ENGINE FOUNDATION & ORDER MANAGEMENT
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify Phases 1–6C, especially:

* Double-entry accounting.
* Portfolio reconciliation.
* Deterministic risk engine.
* Circuit breakers.
* Atomic risk reservations.
* PostgreSQL concurrency tests.

Stop if unresolved P0/P1 issues, accounting discrepancies, or reservation-integrity failures exist.
Preserve all working functionality.
OBJECTIVE
Build Atlas's paper execution infrastructure and order-management system.
Convert risk-approved, reserved trade proposals into simulated paper orders.
This is strictly simulated execution. Do not implement real brokerage connectivity or real order submission.
1. PAPER EXECUTION ARCHITECTURE
Create a modular execution engine with a clearly isolated paper broker interface.
Accept orders only when they have:

* Valid originating decision.
* Valid strategy proposal.
* Approved deterministic risk assessment.
* Active Phase 6C reservation.
* Valid asset and instrument specifications.
* Valid entry and quantity parameters.

Never allow strategies or LLMs to submit orders directly.
Reject unauthorized orders.
2. ORDER MANAGEMENT
Support order types:

* Market.
* Limit.
* Stop.
* Stop-limit.
* Take-profit.
* Trailing stop.

Implement standardized order fields:

* Order ID.
* Portfolio ID.
* Asset ID.
* Direction.
* Order type.
* Quantity.
* Limit price where applicable.
* Stop price where applicable.
* Filled quantity.
* Remaining quantity.
* Reservation ID.
* Strategy and decision references.
* Creation timestamp.
* Expiration timestamp.
* Order status.

Use Decimal for financial values and enforce instrument-specific precision.
3. ORDER STATE MACHINE
Implement explicit, centrally validated transitions.
Support:

* PROPOSED.
* APPROVED.
* OPEN.
* PARTIALLY_FILLED.
* FILLED.
* CANCEL_PENDING.
* CANCELLED.
* EXPIRED.
* REJECTED.
* FAILED.

Define legal transitions and reject invalid transitions.
Record every state change with timestamp, source, and reason.
Do not silently overwrite order history.
Cancellation must not release an active reservation until cancellation is confirmed in the simulated execution system.
4. PHASE 6C INTEGRATION
Integrate directly with existing atomic risk reservations.
Requirements:

* Every executable paper order must reference a valid reservation.
* Never accept an expired, released, rejected, or consumed reservation.
* Preserve idempotency.
* Handle duplicate order submissions.
* Revalidate relevant risk conditions before execution.
* Consume reservations according to actual simulated fills.
* Release only genuinely unfilled, canceled, expired, or rejected exposure.

Do not double-count positions and consumed reservations.
All financial state changes must be transactional.
5. ORDER EVENT PROCESSING
Implement durable order events and transactional outbox processing.
Ensure:

* Order state updates are persistent.
* Event delivery is idempotent.
* Worker retries cannot duplicate orders.
* Process restarts do not lose order state.
* Partial failures can be recovered.
* Duplicate events are safely suppressed.

PostgreSQL remains authoritative.
Redis may support transient processing but cannot become the source of truth.
6. PAPER BROKER INTERFACE
Create a dedicated simulated broker adapter.
It must be separate from all existing Alpaca market-data functionality.
Define interfaces for:

* Order submission.
* Order status.
* Cancellation.
* Fill events.
* Partial fills.
* Broker health.
* Shutdown and recovery.

Do not connect this interface to a real brokerage.
Do not implement actual fill algorithms yet. Those belong to Phase 7B.
Use controlled test execution events to verify order management.
7. POSITION MANAGEMENT INTEGRATION
Prepare interfaces for the existing Phase 6A portfolio and accounting system.
Ensure future fills can update:

* Asset quantities.
* Position state.
* Cash balances.
* Average cost basis.
* Realized P/L.
* Unrealized valuation.
* Transaction costs.

Do not create duplicate portfolio or accounting implementations.
For this deliverable, use controlled test events to verify the integration contracts.
8. DATABASE
Reuse existing order, portfolio, reservation, and accounting models where possible.
Implement necessary migrations for:

* Orders.
* Order events.
* Order idempotency.
* Broker simulation state.
* Execution requests.
* Order expiration.

Preserve immutable order history.
Use appropriate database constraints, indexes, foreign keys, and UTC timestamps.
9. API AND DASHBOARD
Expose:

* Order history.
* Order details.
* Order status.
* Order events.
* Open paper orders.
* Canceled orders.
* Rejected orders.
* Reservation linkage.
* Paper broker status.

Add a functional Paper Orders page to the Atlas dashboard.
Clearly display that all orders are simulated.
Do not add live brokerage controls.
10. TESTING
Test:

* Every valid order transition.
* Every invalid order transition.
* Duplicate submissions.
* Idempotency.
* Reservation verification.
* Reservation consumption interfaces.
* Cancellation.
* Expiration.
* Worker restart.
* Concurrent order creation.
* Transaction rollback.
* Outbox retries.
* Unauthorized order rejection.
* Portfolio integration contracts.

Run concurrency-critical tests against real PostgreSQL.
Preserve all previous tests.
11. SECURITY AND RESTRICTIONS
Absolutely no live order execution.
Do not:

* Connect to brokerage trading endpoints.
* Store real trading credentials.
* Implement live account access.
* Allow an LLM to submit orders.
* Allow a strategy to bypass reservations.
* Automatically enable real trading.

The only supported execution pathway is the isolated paper broker.
12. COMPLETION REPORT
Report:

1. Order-management architecture.
2. State machine and transitions.
3. Phase 6C integration.
4. Idempotency implementation.
5. Transactional event handling.
6. Paper broker interfaces.
7. Database migrations.
8. API endpoints.
9. Dashboard additions.
10. Files created and modified.
11. Unit-test results.
12. Integration-test results.
13. Concurrency-test results.
14. Known limitations.
15. Readiness for Phase 7B.

Implement incrementally. Preserve existing functionality.
FINAL INSTRUCTION: Implement Phase 7A only. Prioritize order-state integrity, transactional consistency, reservation enforcement, and strict paper-only isolation.
