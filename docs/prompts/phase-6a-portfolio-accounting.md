ATLAS AI TRADER — PHASE 6A
PORTFOLIO MANAGEMENT & DOUBLE-ENTRY ACCOUNTING FOUNDATION
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
Verify Phases 1–5B, existing tests, migrations, and the latest audit. Stop if unresolved P0/P1 issues exist.
OBJECTIVE
Build Atlas's portfolio-accounting foundation for its $100,000 simulated USD paper account.
Implement accurate portfolio state, cash management, asset quantities, financial records, and immutable double-entry accounting.
This is an accounting and portfolio foundation, NOT an execution implementation.
1. PORTFOLIO ACCOUNT
Implement:

* Portfolio accounts.
* Initial simulated funding of $100,000 USD.
* Cash balances.
* Asset holdings.
* Long and short positions.
* Realized P/L.
* Unrealized valuation changes.
* Accrued fees and financing costs.
* Equity.
* Available cash.
* Portfolio snapshots.

USD is the initial reporting currency, but design for multiple currencies.
Never mix different currencies without explicit conversion.
2. DOUBLE-ENTRY LEDGER
Implement immutable:

* Journal entries.
* Journal lines.
* Account chart.
* Posting templates.
* Reconciliation records.

Every posted financial transaction must balance according to documented accounting rules.
Use Decimal and PostgreSQL NUMERIC for financial amounts.
Posted journal records cannot be edited or deleted. Corrections require reversal entries.
Use unique event IDs and idempotency to prevent duplicate postings.
Document accounting conventions for:

* Cash.
* Long asset holdings.
* Short liabilities.
* Realized gains and losses.
* Unrealized valuation changes.
* Commissions.
* Fees.
* Borrow costs.
* Funding costs.
* FX conversions.

3. ASSET QUANTITY ACCOUNTING
Track asset quantities independently of cash balances.
Support:

* Long quantities.
* Short quantities.
* Cost basis.
* Average entry price where appropriate.
* Position valuation.
* Asset currency.
* Quantity precision.
* Instrument-specific lot sizes.

Ensure holdings reconcile with posted financial transactions.
Do not treat short-sale proceeds as ordinary freely available equity.
4. MULTI-CURRENCY FOUNDATION
Implement currency-aware accounting.
Support:

* Currency-specific cash balances.
* Currency conversion records.
* Timestamped FX rates.
* Reporting-currency valuation.
* FX gains and losses.
* Currency-specific reconciliation.

Preserve original transaction currency and functional USD values.
Document the accounting treatment for FX clearing and translation.
5. PORTFOLIO VALUATION
Calculate:

* Cash.
* Long asset value.
* Short liabilities.
* Realized P/L.
* Unrealized valuation changes.
* Accrued costs.
* Total equity.
* Available capital.

Do not calculate portfolio equity using a simplistic cash-plus-position-value formula that ignores short liabilities, financing, or multiple currencies.
Use a documented accounting equation.
Separate realized P/L from unrealized valuation changes.
Use timestamped market prices with clear provenance.
If prices are stale or unavailable, flag the valuation rather than silently substituting fabricated prices.
6. TRANSACTION PROCESSING
Build an accounting posting service that can eventually process simulated execution events.
For this phase, support controlled test transactions and opening funding.
Design extensible posting templates for future:

* Long purchases.
* Long sales.
* Short openings.
* Short covers.
* Fees.
* Financing costs.
* FX conversions.

Do not integrate an order execution engine yet.
7. DATABASE AND API
Reuse existing schemas where possible.
Create migrations and models for:

* Portfolio accounts.
* Chart of accounts.
* Journal entries.
* Journal lines.
* Asset quantity ledger.
* Currency balances.
* FX rates.
* Portfolio snapshots.
* Reconciliation records.

Expose endpoints for:

* Portfolio overview.
* Cash balances.
* Holdings.
* P/L breakdown.
* Journal history.
* Account balances.
* Reconciliation status.

Preserve immutable financial history.
PostgreSQL is authoritative.
8. RECONCILIATION
Build automatic accounting checks.
Verify:

* Debit/credit balance.
* Cash reconciliation.
* Asset quantity reconciliation.
* Position valuation.
* Short liabilities.
* Currency balances.
* Realized P/L.
* Unrealized valuation changes.
* Total equity.

Any unexplained discrepancy must generate a high-priority accounting error.
Never silently correct financial records.
9. DASHBOARD
Add a functional Portfolio and Accounting section.
Display:

* $100,000 initial simulated funding.
* Cash by currency.
* Holdings.
* Long and short exposure.
* Realized P/L.
* Unrealized P/L.
* Equity.
* Fees.
* Ledger activity.
* Reconciliation status.

Use actual backend data.
Clearly distinguish opening simulated funding from investment returns.
10. TESTING
Test:

* Double-entry balancing.
* Opening account funding.
* Long asset accounting.
* Short liability accounting.
* Fees.
* Realized and unrealized P/L.
* Multi-currency conversion.
* Decimal precision.
* Reversals.
* Duplicate posting prevention.
* Concurrent transactions.
* Portfolio reconciliation.
* Stale valuations.
* Database persistence.

Use deterministic financial test vectors.
Preserve all previous tests.
11. RESTRICTIONS
Implement Phase 6A only.
Do not implement:

* Risk reservations.
* Position-sizing authorization.
* Circuit breakers.
* Order execution.
* Paper broker integration.
* Live brokerage functionality.

These will be implemented separately.
12. COMPLETION REPORT
Report:

* Accounting architecture.
* Chart of accounts.
* Journal posting rules.
* Portfolio valuation methodology.
* Multi-currency design.
* Database migrations.
* API endpoints.
* Dashboard changes.
* Files created and modified.
* Test results.
* Reconciliation results.
* Known limitations.
* Readiness for Phase 6B.

Do not claim financial correctness without passing reconciliation tests.
Implement incrementally. Do not rewrite working systems.
FINAL INSTRUCTION: Implement Phase 6A only. Financial accuracy, immutable accounting, precise decimal arithmetic, and reconciliation take priority over speed or additional features.
