ATLAS AI TRADER — PHASE 7B
REALISTIC PAPER EXECUTION & AUTOMATED POSITION MANAGEMENT
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the existing codebase.
First verify Phase 7A, Phase 6A–6C, accounting reconciliation, order state transitions, and atomic risk reservations.
Run existing tests. Stop if unresolved P0/P1 issues, accounting discrepancies, or reservation-integrity failures exist.
Preserve all working functionality.
OBJECTIVE
Complete Atlas's isolated paper execution engine.
Convert approved and reserved orders into realistic simulated fills, maintain open positions, execute protective orders, and automatically close positions while keeping the portfolio ledger accurate.
This system must use actual market-data inputs but must never submit real brokerage orders.
1. MARKET FILL SIMULATION
Implement configurable execution models for:

* Market orders.
* Limit orders.
* Stop orders.
* Stop-limit orders.
* Take-profit orders.
* Trailing stops.

Use actual available market data.
For market orders:

* Buy orders should normally execute against the ask.
* Sell orders should normally execute against the bid.
* Apply configurable additional slippage.
* Respect available liquidity where measurable.

For limit orders:

* Never fill a buy above its limit.
* Never fill a sell below its limit.
* Do not assume a candle touching the limit guarantees a fill.
* Use conservative execution assumptions when quote or trade data is insufficient.

For stop orders:

* Trigger according to documented stop-price rules.
* Account for gaps.
* Do not guarantee execution at the stop price.

For stop-limit orders:

* Separate stop triggering from limit execution.
* Allow triggered orders to remain unfilled.

Do not use future data to determine whether an order would have filled.
2. EXECUTION COSTS
Model configurable:

* Bid/ask spread.
* Slippage.
* Commissions.
* Exchange fees where applicable.
* Partial fills.
* Market impact approximations.
* Borrow fees.
* Financing costs where applicable.

Keep execution assumptions versioned.
Do not claim the simulator perfectly replicates real-world market execution.
If the configured IEX feed lacks sufficient information for a reliable fill, explicitly identify the limitation.
Do not fabricate quotes or liquidity.
Use conservative, configurable fallback behavior or reject/defer execution when required data is unavailable.
3. PARTIAL FILLS AND LIQUIDITY
Implement partial fills based on configurable liquidity and participation assumptions.
Track:

* Requested quantity.
* Filled quantity.
* Remaining quantity.
* Fill price.
* Execution time.
* Slippage.
* Fees.

Respect instrument quantity precision and lot requirements.
Prevent simulated fills from exceeding permitted liquidity assumptions.
Update order states correctly after every partial or complete fill.
4. POSITION MANAGEMENT
Connect fills to the existing Phase 6 portfolio and accounting systems.
Support:

* Long positions.
* Short positions.
* Partial position closes.
* Full position closes.
* Average entry cost.
* Realized P/L.
* Unrealized P/L.
* Accrued fees.
* Borrow and financing costs where applicable.

Position quantity and cash movements must reconcile with the double-entry ledger.
Never create or close a position without corresponding auditable financial events.
5. AUTOMATED EXITS
Implement automatic position management using:

* Stop losses.
* Take-profit targets.
* Trailing stops.
* Strategy-defined invalidation levels.
* Explicit position expiration where configured.

Support linked protective orders and appropriate OCO behavior where applicable.
Ensure a stop and target cannot both close the same remaining quantity.
Handle simultaneous trigger events deterministically.
When a protective exit partially fills, update remaining quantities and reservations correctly.
A risk-reducing exit must remain possible when new-entry circuit breakers are active.
6. POSITION MONITORING
Create a position-monitoring worker.
It should:

* Monitor current prices.
* Evaluate protective orders.
* Update unrealized valuation.
* Detect stop/target triggers.
* Detect expired positions.
* Process close requests.
* Record all position events.

Use appropriate market-data update schedules.
Avoid excessive polling.
Handle stale market data safely.
Do not execute exits using invalid or fabricated prices.
7. RISK RESERVATION INTEGRATION
Integrate directly with Phase 6C.
On each fill:

* Atomically consume the corresponding reservation.
* Convert filled exposure into actual position exposure.
* Retain reservation for unfilled quantities where applicable.
* Prevent double counting.

On cancellation, rejection, or expiration:

* Release only the valid unfilled reservation.
* Preserve filled exposure.

Never release a reservation while its associated order or fill state is uncertain.
Ensure the portfolio and risk engine remain consistent after partial fills and restarts.
8. ACCOUNTING INTEGRATION
Use Phase 6A's immutable double-entry ledger.
Post transactions for:

* Long purchases.
* Long sales.
* Short openings.
* Short covers.
* Realized gains and losses.
* Commissions.
* Fees.
* Borrow costs.
* Financing costs.
* Other supported execution expenses.

Each fill must have a unique financial event identifier.
Retries must not post duplicate journal entries.
Corrections must use reversals.
Every fill must reconcile to its corresponding cash and asset-quantity movements.
9. CONSERVATIVE BAR-BASED EXECUTION
If bid/ask or tick-level data is unavailable, a candle-based simulator may be used only with explicitly documented limitations.
Never assume the candle's high and low establish the chronological order of intrabar events.
If both stop and target are touched within the same candle and finer-grained information cannot establish which occurred first, use a conservative stop-first assumption.
Do not invent an intrabar price path.
10. ORDER AND POSITION PERSISTENCE
Extend existing models where necessary for:

* Execution attempts.
* Fill records.
* Execution costs.
* Position events.
* Protective orders.
* Trailing-stop state.
* Close requests.
* Execution-model versions.

Use immutable event history, database constraints, idempotency, and UTC timestamps.
PostgreSQL remains authoritative.
Worker restarts must not duplicate fills, closes, or accounting entries.
11. DASHBOARD
Extend Atlas with functional pages for:
Paper Orders

* Open orders.
* Partially filled orders.
* Filled orders.
* Cancellations.
* Rejections.
* Execution details.

Open Positions

* Asset.
* Direction.
* Quantity.
* Average entry.
* Current price.
* Unrealized P/L.
* Stop.
* Target.
* Holding duration.

Closed Positions

* Entry and exit.
* Realized P/L.
* Fees.
* Net return.
* Exit reason.
* Strategy origin.

Execution Analytics

* Simulated slippage.
* Fill rates.
* Partial fills.
* Estimated costs.
* Execution-model assumptions.

Clearly label everything as simulated.
Use actual backend data.
12. TESTING
Test:

* Market-order fills.
* Limit-price restrictions.
* Stop triggers.
* Stop gaps.
* Stop-limit behavior.
* Trailing stops.
* Take-profit behavior.
* Partial fills.
* Liquidity constraints.
* Slippage and fees.
* Long and short accounting.
* Partial closes.
* OCO behavior.
* Simultaneous stop/target triggers.
* Reservation consumption.
* Reservation release.
* Duplicate fill prevention.
* Worker restart.
* Database interruption.
* Ledger reconciliation.
* Circuit-breaker interaction.
* Stale-data behavior.
* Concurrent order processing.

Use real PostgreSQL for transaction and concurrency tests.
Use deterministic market-data fixtures for ordinary simulation tests.
Preserve every existing test.
13. STRICT RESTRICTIONS

* No live brokerage connectivity.
* No real-money order submission.
* No external brokerage execution SDK.
* No automatic live-trading mode.
* No AI bypass of risk controls.
* No unapproved changes to risk limits.

All execution must remain inside the isolated Atlas paper broker.
14. ACCEPTANCE CRITERIA
Phase 7B is complete only when:

1. Approved orders produce simulated executions.
2. Multiple order types operate correctly.
3. Bid/ask and slippage assumptions are applied.
4. Partial fills work.
5. Protective exits operate correctly.
6. Positions open and close automatically.
7. Long and short accounting reconciles.
8. Reservations are consumed and released correctly.
9. Duplicate fills are impossible under tested retry conditions.
10. Worker restarts preserve execution integrity.
11. The dashboard displays actual simulated positions and fills.
12. Existing regression tests pass.
13. Concurrency and financial correctness tests pass.
14. The paper broker remains completely isolated from live trading.

15. COMPLETION REPORT
Report:

* Fill simulation methodology.
* Order-type behavior.
* Slippage and fee assumptions.
* Partial-fill methodology.
* Position lifecycle.
* Protective order behavior.
* Accounting integration.
* Reservation integration.
* Database changes.
* Dashboard additions.
* Files created and modified.
* Test results.
* Concurrency results.
* Financial reconciliation results.
* Known execution limitations.
* Readiness for Phase 8.

Do not claim realistic fills are guaranteed to match real-world execution.
Do not claim the simulator is profitable or that its assumptions are unbiased.
Implement incrementally and preserve all existing functionality.
FINAL INSTRUCTION: Implement Phase 7B only. Prioritize realistic execution assumptions, correct accounting, reliable position management, and strict paper-trading isolation.
