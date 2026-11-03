ATLAS AI TRADER — PHASE 4A
STRATEGY LABORATORY FOUNDATION
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
Verify Phase 3A, 3B, and 3C acceptance criteria, tests, and database migrations. Stop if unresolved critical P0/P1 issues exist.
Preserve all working functionality.
OBJECTIVE
Build the Strategy Laboratory architecture that will support six independent trading strategy families.
Do not implement portfolio execution or brokerage functionality.
1. STRATEGY ARCHITECTURE
Create a standardized strategy interface and lifecycle.
Every strategy must support:

* Unique strategy ID.
* Immutable version.
* Description.
* Enabled/disabled status.
* Supported asset classes.
* Supported timeframes.
* Required features.
* Configurable parameters.
* Evaluation schedule.
* Signal expiration.
* Historical evaluation.

Strategies must operate independently and share a consistent interface.
Adding a new strategy should not require modifying unrelated strategy modules.
2. FEATURE INTEGRATION
Reuse existing Phase 3 features and indicators.
Inspect what already exists before implementing anything new.
Establish a centralized feature registry describing:

* Feature name.
* Calculation method.
* Parameters.
* Timeframe.
* Required data.
* Availability timestamp.
* Version.
* Validity status.

Support common technical measurements needed by the strategies, including EMA, SMA, RSI, ATR, ADX, MACD, Bollinger Bands, volume ratios, and returns.
Do not duplicate existing calculations.
Do not use unfinished candles where a strategy requires completed candles.
Prevent future data from entering historical decisions.
3. SIX STRATEGY MODULES
Establish independent modules for:

1. Momentum.
2. Trend following.
3. Mean reversion.
4. Breakout.
5. Volatility.
6. Event driven.

Create interfaces and configuration schemas for each.
Actual strategy logic will be implemented in Phase 4B.
The event-driven module must be capable of consuming Phase 3C news and event features, but must not invent missing news information.
4. STANDARDIZED SIGNAL PROPOSALS
Create a shared proposal schema.
Each proposal must contain:

* Proposal ID.
* Strategy ID and version.
* Asset ID.
* Market.
* Direction: LONG or SHORT.
* Timeframe.
* Signal timestamp.
* Data cutoff timestamp.
* Feature snapshot identifiers.
* Proposed reference entry.
* Optional invalidation level.
* Optional target reference.
* Expected holding horizon.
* Signal strength.
* Supporting evidence.
* Reasoning summary.
* Expiration timestamp.

Signal strength must not be represented as a calibrated probability unless separately validated.
A proposal is a hypothesis, not an approved trade.
Do not calculate authoritative position sizes or submit orders.
5. EVALUATION ENGINE
Build an event-driven strategy evaluation system.
It should:

* Receive validated market and feature updates.
* Identify strategies requiring reevaluation.
* Evaluate applicable strategies.
* Record proposals.
* Prevent duplicate evaluations.
* Handle expired signals.
* Record failures.
* Support controlled concurrent execution.

One strategy failure must not crash unrelated strategies.
Use idempotency and appropriate database transaction handling.
6. STRATEGY VERSIONING AND STORAGE
Implement or extend database models for:

* Strategies.
* Strategy versions.
* Strategy configurations.
* Feature definitions.
* Feature snapshots.
* Strategy evaluations.
* Strategy proposals.
* Evaluation errors.

Preserve historical versions.
Changing a strategy's parameters must create a new immutable version.
Never overwrite historical proposals or evaluation results.
Use Alembic migrations and PostgreSQL as the source of truth.
7. API AND DASHBOARD
Expose:

* Available strategies.
* Strategy details.
* Strategy versions.
* Strategy configurations.
* Evaluation history.
* Generated proposals.
* Strategy enable/disable status.

Add a Strategy Laboratory page to the existing dashboard.
Display:

* Strategy catalog.
* Description.
* Version.
* Supported markets.
* Supported timeframes.
* Status.
* Recent proposals.
* Evaluation history.

Do not show fabricated performance statistics.
Do not introduce buy/sell execution controls.
8. TESTING
Test:

* Strategy interfaces.
* Configuration validation.
* Feature dependencies.
* Timestamp correctness.
* Proposal schemas.
* Strategy version immutability.
* Duplicate evaluations.
* Concurrent evaluations.
* Expired proposals.
* Worker failures.
* API responses.
* Dashboard rendering.
* Historical look-ahead prevention.

Preserve every existing test.
9. SCOPE RESTRICTIONS
Implement Phase 4A only.
Do not implement:

* Actual strategy trading logic beyond shared framework functionality.
* AI trade coordination.
* ML predictions.
* Portfolio management.
* Position sizing.
* Risk approval.
* Order execution.
* Brokerage integration.

These belong to later phases.
10. COMPLETION REPORT
Report:

* Architecture.
* Strategy interfaces.
* Feature registry.
* Proposal schema.
* Database changes.
* API endpoints.
* Dashboard additions.
* Files created and modified.
* Test results.
* Remaining limitations.
* Readiness for Phase 4B.

Implement incrementally. Avoid rewriting working systems.
FINAL INSTRUCTION: Build the Strategy Laboratory foundation only. It must be modular, versioned, reproducible, and ready for six independent strategy implementations.
