# ATLAS AI TRADER — PHASE 9: COMPLETE DASHBOARD & SYSTEM INTEGRATION

Read `ATLAS_MASTER_SPEC_V2.md` and inspect the existing codebase.

## 1. Preflight

* Verify you are working on `main` at or descended from commit `1eec53c`.
* Confirm Phases 1–8B are present.
* Review the latest integration audit.
* Run existing frontend tests, typecheck, and build.
* Verify relevant backend APIs and database models.
* Stop if unresolved P0/P1 issues exist.
* Do not switch worktrees or discard existing changes.

## 2. Objective

Complete Atlas's professional React/TypeScript trading dashboard.
Integrate existing backend functionality rather than rebuilding it.
Use a cohesive dark quantitative-trading-terminal design, optimized for desktop with responsive layouts.

## 3. Dashboard sections

Complete these sections using actual backend data:

1. Overview: Equity, P/L, market status, system health, portfolio summary.
2. Markets: Watchlists, asset details, live prices, interactive charts.
3. Asset Discovery: Discovery pool, qualified universe, rankings.
4. Market Intelligence: Trends, volatility, regimes, correlations.
5. News & Events: Headlines, classifications, asset associations.
6. Strategies: Six strategies, configurations, proposals, performance.
7. AI Intelligence: Decision history, evidence, model contributions.
8. Portfolio: Cash, holdings, positions, realized/unrealized P/L.
9. Risk Center: Limits, exposure, reservations, circuit breakers.
10. Orders & Execution: Order lifecycle, fills, open/closed positions.
11. Backtesting: Run history, equity curves, metrics, benchmarks.
12. Research & Validation: Walk-forward results, model registry, challengers, degradation.
13. System Health: Providers, workers, database, errors, recovery.
14. Settings: Existing safe configuration and system information.

Inspect existing pages first. Extend and improve them rather than duplicating functionality.

## 4. Integration requirements

* Connect every page to existing FastAPI endpoints.
* Add only genuinely missing endpoints.
* Use shared TypeScript API types.
* Display real persisted data, not fabricated examples.
* Provide loading, empty, error, and disconnected states.
* Clearly distinguish historical, live, simulated, and unavailable data.
* Preserve market-data feed identification.
* Display financial values with appropriate precision.
* Keep charts historically accurate.
* Prevent stale requests and duplicate subscriptions.
* Show actual backend/worker health.
* Make paper-trading status unmistakable.

If a subsystem is incomplete or unavailable, identify it honestly rather than presenting it as operational.

## 5. UI quality

Build reusable components, consistent navigation, tables, charts, status indicators, and responsive layouts.
Prioritize information density, readability, and professional trading-terminal styling.
Avoid unnecessary animations, decorative metrics, fake profitability figures, and redundant API polling.

## 6. Safety

This is dashboard development, not autonomous orchestration.

* Do not modify trading strategies, ML models, risk calculations, accounting, or execution logic.
* Do not implement live brokerage functionality.
* Do not add unprotected administrative controls.
* Keep the dashboard read-only by default.
* Preserve existing authentication and authorization.
* Do not expose credentials.
* Do not migrate or reset the currently running database.
* Do not alter the actual paper portfolio.

## 7. Testing

Test:

* All dashboard routes.
* API integration.
* Tables and charts.
* Financial displays.
* Error and disconnected states.
* Navigation.
* Type safety.
* Responsive behavior.
* Backend endpoints changed.
* Security and authorization boundaries.

Preserve existing tests. Run frontend tests, typecheck, production build, and relevant backend regression tests.

## 8. Completion report

Report:

* Pages completed.
* Components created or modified.
* API integrations.
* Backend changes.
* Files changed.
* Test results.
* Screenshots if available.
* Incomplete functionality.
* Remaining limitations.
* Readiness for Phase 10.

Implement Phase 9 only. Do not begin autonomous trading orchestration. Prioritize accurate information, complete integration, and a polished operational interface.
