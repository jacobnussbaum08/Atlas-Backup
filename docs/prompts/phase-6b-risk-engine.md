ATLAS AI TRADER — PHASE 6B
DETERMINISTIC RISK MANAGEMENT ENGINE
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the complete existing codebase.
Verify Phase 6A accounting, portfolio valuation, ledger balancing, and reconciliation. Run existing tests. Stop if unresolved P0/P1 issues or financial discrepancies exist.
Preserve all working functionality.
OBJECTIVE
Build Atlas's deterministic risk engine. It must evaluate proposed trades, calculate permissible position sizes, enforce portfolio restrictions, and activate circuit breakers.
The risk engine is the final authority over whether a proposal is eligible for future execution.
AI models and strategies must never override it.
1. INITIAL RISK CONFIGURATION
Implement versioned, configurable defaults:

* Risk per trade: 0.5% of current equity.
* Maximum simultaneous positions: 8.
* Daily loss limit: 2%.
* Weekly loss limit: 5%.
* Maximum portfolio drawdown: 10%.
* Maximum leverage: 1x.
* Maximum gross exposure: 100% of equity.

Do not describe these values as optimal.
Changes require explicit authorized user action and immutable audit records. Autonomous models cannot modify hard limits.
2. POSITION SIZING
Implement deterministic position-sizing calculations.
Initial calculation:
Risk budget = Current equity × permitted risk percentage.
Raw quantity = Risk budget ÷ absolute distance between entry and stop.
Apply additional restrictions for:

* Available capital.
* Gross exposure.
* Net exposure.
* Asset-class concentration.
* Sector concentration.
* Correlated exposure.
* Liquidity.
* Spread and estimated slippage.
* Transaction costs.
* Instrument tick size and lot size.
* Currency conversion.
* Margin requirements.

Support long and short positions.
Implement instrument-specific sizing for stocks, ETFs, and extensible interfaces for forex and crypto.
Reject missing, invalid, or incorrectly positioned stop levels when required by policy.
Never allow rounding to increase risk beyond the permitted budget.
3. PORTFOLIO EXPOSURE
Calculate:

* Gross exposure.
* Net exposure.
* Long exposure.
* Short exposure.
* Asset exposure.
* Asset-class exposure.
* Sector exposure.
* Correlated-cluster exposure.
* Margin usage.
* Available risk budget.

Use actual portfolio and ledger information.
Account for pending exposure where available, but do not invent reservations before Phase 6C.
Do not assume offsetting positions eliminate correlated or directional risk.
4. TRADE VALIDATION
Create a standardized deterministic risk assessment.
Evaluate:

* Portfolio equity.
* Proposed direction.
* Entry and stop.
* Position quantity.
* Potential loss.
* Expected costs.
* Current exposure.
* Position count.
* Liquidity.
* Data freshness.
* Short eligibility.
* Borrow availability where required.
* Margin restrictions.
* Active circuit breakers.

Return explicit results:

* PASS_FOR_RESERVATION.
* REJECTED.
* INSUFFICIENT_DATA.
* RISK_HALTED.

A passing assessment is NOT an execution approval. Atomic reservations and final order authorization belong to Phase 6C.
Record every decision and its exact reasons.
5. CIRCUIT BREAKERS
Implement:

* Daily loss limit.
* Weekly loss limit.
* Maximum drawdown.
* Excessive exposure.
* Stale or invalid market data.
* Accounting reconciliation failure.
* Execution-related safety interface.
* Manual kill switch.

Calculate daily and weekly performance using clearly defined starting-of-period equity, including realized and unrealized P/L and applicable costs.
Define trading-day boundaries using the appropriate market calendar and documented timezone policy.
Track portfolio peak equity for drawdown calculations.
Default response to a loss-limit breach:

* Block new trade proposals from passing.
* Continue monitoring existing positions.
* Permit risk-reducing operations in future execution phases.
* Record the circuit-breaker event.
* Require the configured recovery or human review process before resuming.

Do not automatically liquidate positions.
Do not silently reset circuit breakers.
6. SHORT-SELLING SAFEGUARDS
Evaluate:

* Shortability.
* Borrow availability.
* Borrow costs.
* Margin requirements.
* Short exposure.
* Potentially unlimited theoretical losses.

Missing borrow or margin information must result in rejection or an explicitly restricted assessment according to policy.
Never assume all assets can be shorted.
7. RISK CONFIGURATION GOVERNANCE
Implement immutable configuration versions.
Record:

* Previous configuration.
* New configuration.
* Authorized actor.
* Timestamp.
* Reason.
* Configuration version.

Separate hard limits from adjustable analytical preferences.
No strategy, LLM, or ML model may alter hard risk parameters.
8. DATABASE AND API
Reuse existing portfolio and accounting models.
Implement or extend:

* Risk configurations.
* Configuration versions.
* Risk assessments.
* Risk events.
* Circuit-breaker states.
* Portfolio exposure snapshots.
* Risk rejection reasons.

Expose APIs for:

* Current risk state.
* Active limits.
* Exposure.
* Risk assessments.
* Circuit-breaker history.
* Configuration history.

Add the corresponding Risk Center dashboard functionality.
9. TESTING
Prioritize deterministic financial test vectors.
Test:

* Position-sizing math.
* Long and short calculations.
* Rounding.
* Transaction costs.
* Exposure limits.
* Correlation limits.
* Daily and weekly loss.
* Drawdown.
* Multiple currencies.
* Missing prices.
* Stale data.
* Missing stops.
* Short eligibility.
* Circuit-breaker activation and recovery.
* Configuration immutability.
* Concurrent assessments.
* Accounting integration.

Test boundary conditions, not just ordinary successful trades.
Preserve all previous tests.
10. STRICT RESTRICTIONS
Implement Phase 6B only.
Do not implement:

* Atomic risk reservations.
* Order submission.
* Paper broker.
* Trade execution.
* Live brokerage.
* Automatic liquidation.

Atomic reservations and their transactional integration belong to Phase 6C.
11. COMPLETION REPORT
Report:

* Risk architecture.
* Position-sizing methodology.
* Exposure calculations.
* Circuit-breaker rules.
* Default configuration.
* Database changes.
* API endpoints.
* Dashboard additions.
* Files created and modified.
* Test results.
* Financial correctness results.
* Known limitations.
* Readiness for Phase 6C.

Do not claim risk enforcement is impossible to bypass without testing the actual interfaces and database constraints.
FINAL INSTRUCTION: Implement Phase 6B only. Prioritize deterministic calculations, conservative failure handling, auditable decisions, and absolute separation between AI recommendations and risk authority.
