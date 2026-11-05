# Phase 6A — Portfolio management and double-entry accounting: completion report

Prompt: [`docs/prompts/phase-6a-portfolio-accounting.md`](../prompts/phase-6a-portfolio-accounting.md).
Conventions: [`docs/architecture/accounting.md`](../architecture/accounting.md). Decisions:
D-125 to D-137 in [`decisions.md`](../architecture/decisions.md).

**Paper only.** No order engine, paper broker, risk reservation, sizing or circuit breaker
was built. There is no execution source in the ledger and no endpoint that writes anything.

## 1. Preflight

* **Base.** `main` was at Phase 4B; Phase 5A/5B (`abf1bca`) and the discovery `created_at`
  fix (`cee3fde`, D-124) were sibling branches, neither merged. Phase 6A was built on
  `abf1bca` with the D-124 fix merged in (one `decisions.md` conflict resolved by keeping both
  sections).
* **Audits.** No open P0/P1: the only audit (3A) had its P1s fixed (`25ab296`); the 5B
  report records no open findings.
* **Defect fixed first.** The committed Phase 5A Dockerfile lost the line continuations of
  its `RUN useradd ... && mkdir ...` (a line starting with `&&` is not a Dockerfile
  instruction), so `docker compose up --build` could not build the backend image. The fix,
  present uncommitted in the main checkout, is reproduced here (D-125).
* **Baseline** (5B + D-124, before any 6A code): backend `pytest` with PostgreSQL **1472
  passed, 1 skipped** (Redis integration, no `ATLAS_TEST_REDIS_URL`), 14 live deselected; the
  17 time-bomb failures of the 5B report are gone with D-124. `mypy`: 69 pre-existing errors in
  four Phase 4A test files (as reported in 5B).

## 2. Accounting architecture

```text
CLI (only write path) -> PostingService ----lock portfolio_accounts row FOR UPDATE----+
                          | idempotency (account, event_id) + request hash            |
                          | LedgerState from journal_lines + asset_quantity_ledger    |
                          | template (pure, versioned) -> draft                       |
                          | validate R1/R2 + line rules; no new invariant violation   |
                          v                                                           |
     journal_entries / journal_lines / asset_quantity_ledger (append-only) <-----------+
     currency_balances (cache, reconciled)    fx_rates   instrument_specs
ValuationService -> VALUATION entry + portfolio_snapshots(+positions)
ReconciliationService -> reconciliation_runs / reconciliation_results
API (GET only) and dashboard read everything from the ledger
```

Defence in depth in PostgreSQL: append-only triggers on every ledger table; entries stamped
with their transaction and numbered without gaps (`atlas_journal_entry_guard`); lines and
movements only in their entry's transaction, chart and precision rules
(`atlas_journal_line_guard`, `atlas_asset_quantity_guard`); R1/R2 and two-line minimum checked
at COMMIT (`atlas_journal_entry_balanced`, deferred constraint trigger); account identity
immutable and accounts never deleted (`atlas_portfolio_account_guard`); CONTROLLED_TEST
postings refused on the PRIMARY account.

## 3. Chart of accounts

19 accounts: 1000 Cash; 1010 Restricted cash: short-sale proceeds; 1100 Long securities at
cost; 1150 Long valuation adjustment; 1900 FX conversion clearing; 1910 FX translation
adjustment; 2100 Short liability at proceeds; 2150 Short valuation adjustment; 2200 Accrued
fees; 2210 Accrued financing; 3000 Contributed capital (simulated opening funding); 4000
Realized trading P/L; 4100 Unrealized valuation changes; 4200 Realized FX; 4300 Unrealized FX
translation; 5000 Commissions; 5100 Fees; 5200 Borrow costs; 5300 Funding costs. Types, normal
sides, per-asset rules and line modes: `accounting.md`.

## 4. Journal posting rules

* R1: every currency balances within every entry; R2: functional values balance.
* Templates (v1): `opening_funding`, `long_purchase`, `long_sale`, `short_open`,
  `short_cover`, `fee`, `financing_cost`, `accrual_settlement`, `fx_conversion`, `valuation`,
  `reversal`. Line-by-line rules in `accounting.md`; each is stored in `posting_templates` on
  first use with a definition hash that must match the code.
* Average-cost relief; commissions/fees expensed; short proceeds restricted; realized P/L
  separated from unrealized (valuation adjustments are released pro rata on closing).
* Idempotency: an event ID posts once; the same request returns the original entry, a
  different one is refused. Corrections are reversals, refused when a later entry touches the
  same assets.
* Sources: SYSTEM (funding, valuation), CONTROLLED_TEST (trades, costs, conversions on
  CONTROLLED_TEST accounts only), OPERATOR (reversals).

## 5. Portfolio valuation methodology

Accounting equation (functional currency): assets (1000 + 1010 + 1100 + 1150 + 1910) −
liabilities (2100 + 2150 + 2200 + 2210) = equity = contributed capital + realized P/L +
unrealized valuation + realized FX + unrealized FX − commissions − fees − borrow − funding.
Both sides are computed independently and reconciled; snapshots carry a database CHECK that
assets − liabilities = equity and equity − capital = investment return. Short positions are
an asset (restricted proceeds) plus a liability at buy-back value, never "cash + position
value".

Checkpoints post valuation from fresh, unadjusted prices (newest of live trade, quote
midpoint, complete bar close, each observed and available by `as_of`) with full provenance
in the snapshot. Stale or missing prices, rates or lot sizes are flagged (STALE /
INCOMPLETE) and the item keeps its last posted valuation; nothing is substituted.

## 6. Multi-currency design

Per-currency balances with the original amount and functional value on every line; FX
clearing (1900) for conversions (its functional value is always zero); foreign currency at
historical average carrying value; realized FX (4200) when it is realized; unrealized
translation (1910/4300) posted at checkpoints from fresh spot rates and released on
conversion; timestamped `fx_rates` with source and provenance; executed conversion rates
recorded but never used for valuation; TEST_FIXTURE rates only on test accounts. USD is the
functional currency; seven fiat currencies are registered (USD, EUR, GBP, JPY, CHF, CAD, AUD).

## 7. Database migration

`0013_portfolio_accounting`: `currencies` (seeded), `fx_rates`, `instrument_specs`,
`ledger_accounts` (seeded), `posting_templates`, `journal_entries`, `journal_lines`,
`asset_quantity_ledger`, `currency_balances`, `portfolio_snapshots`,
`portfolio_snapshot_positions`, `reconciliation_runs`, `reconciliation_results`; on
`portfolio_accounts`: `purpose` and a foreign key to `currencies`; all triggers above.
Verified: upgrade from 0012 with an existing account row, `alembic check` (no drift),
downgrade to 0012 and upgrade again; the migration round-trip test passes.

## 8. API endpoints (GET only)

`/api/v1/portfolio/accounts`, `/overview`, `/cash`, `/holdings`, `/pnl`, `/snapshots`;
`/api/v1/accounting/chart`, `/templates`, `/journal`, `/journal/{entry_id}`, `/balances`,
`/reconciliation`, `/reconciliation/{run_id}`, `/fx-rates`. `account_id` defaults to the
primary account; without one the API answers 404 with the `init` instruction. The OpenAPI
safety test lists them and asserts GET only (D-136).

CLI (`python -m app.accounting.cli`): `init`, `create-test-account`, `fx-rate`,
`instrument-spec`, `post`, `reverse`, `checkpoint`, `reconcile`, `show`.

## 9. Dashboard

New **Portfolio & Accounting** page (`/portfolio`, replacing the "not built" placeholder):
opening simulated funding shown as contributed capital apart from the investment return;
equity, available cash and capital; long/short exposure; realized and unrealized P/L; fees and
costs; the accounting equation with both sides and a balance badge; valuation status and
issues; cash by currency (restricted proceeds separate); holdings with price status and
provenance; P/L and costs by asset; the journal with expandable debit/credit lines; the trial
balance; reconciliation status, per-check results and accounting errors. An account selector
appears when CONTROLLED_TEST accounts exist (labelled "test"). Decimal strings are formatted
with BigInt half-even rounding, never floats (D-137). No trading controls.

Checked in the browser against this worktree's backend and a separate preview database
(`atlas_phase6a_preview`): the primary account shows 100,000.00 USD funding, equity
100,000.00, investment return 0.00, reconciliation passed (44 checks); the controlled test
account (USD→EUR conversion, commission, borrow accrual, checkpoint at a fixture rate) shows
equity 100,146.25 = 100,000 + 150.00 translation − 1.25 − 2.50, passed (64 checks).

## 10. Files

Created — backend: `app/accounting/{chart,cli,config,enums,ledger,money,posting,
reconciliation,reference,runtime,state,templates}.py`; `app/portfolio/{accounts,figures,
pricing,queries,valuation}.py`; `app/api/v1/portfolio.py`; `app/database/models/
{accounting,portfolio}.py`; `alembic/versions/0013_portfolio_accounting.py`; tests
`unit/test_accounting_templates.py`, `unit/test_accounting_reference.py`,
`integration/accounting_support.py`, `integration/test_accounting_ledger.py`,
`integration/test_accounting_api.py`. Frontend: `pages/PortfolioPage.tsx`,
`pages/portfolio.test.tsx`, `services/portfolio.ts`, `types/portfolio.ts`, `lib/money.ts`,
`lib/money.test.ts`. Docs: `architecture/accounting.md`, this report, the prompt.

Modified: `app/main.py`, `app/__main__.py`, `app/api/v1/__init__.py`,
`app/database/models/{__init__,portfolio_account}.py`, `app/asset_discovery/discovery.py`
(D-124), `tests/conftest.py`, `tests/unit/{test_api,test_models,test_health_checks}.py`,
`docker/backend.Dockerfile`, `.env.example` (`ACCOUNTING_*`, no credentials),
`frontend/src/{App.tsx,App.test.tsx,navigation.ts}`, `README.md`,
`docs/architecture/{decisions,overview}.md`, `docs/operations.md`.

No dependency added.

## 11. Test results

Deterministic vectors worked by hand: long round trip (buy 100 @ 150.25, mark 160.10, sell 40
@ 158, sell 60 @ 155), average-cost relief without residue, short round trip with valuation
and buy-back, short gain, paid and accrued costs and settlement, USD→EUR→USD conversion with
translation and realized FX, a EUR-quoted stock (trading P/L and FX component), cross-currency
conversion, foreign accrual settled at a different rate, JPY precision, half-even notional
rounding, lot sizes, float refusal, every validation rule, reversals. Twelve seeded random
sequences (60 steps of trades, costs, marks and conversions in USD and EUR) after every full
checkpoint equal an **independent cash-flow model** to the cent.

Integration (real PostgreSQL): funding once and idempotent; controlled tests refused on the
primary account by the service and the database; every ledger table immutable (UPDATE,
DELETE, TRUNCATE refused); unbalanced entries refused at COMMIT, late lines and sequence gaps
refused; the lifecycle vector reconciles after every step; checkpoint idempotency and order;
duplicate events and conflicting reuse; reversal history; 8 concurrent workers × 6 postings
(including a shared event) → 41 entries, sequence 1..42 gapless, reconciled; 10 concurrent
purchases against cash for 6 → exactly 6 posted, cash never negative; multi-currency persisted
and reconciled; executed conversion rates never value; stale and missing prices flagged with
the last mark kept; tampering (cache edit, a line inserted with triggers disabled) detected
as HIGH errors with an audit record and nothing corrected; template hashes. API and CLI
end to end.

| Check | Result |
|---|---|
| Backend `pytest` (full, with `ATLAS_TEST_DATABASE_URL`) | **1589 passed, 1 skipped** (Redis integration, no `ATLAS_TEST_REDIS_URL`), 14 live deselected (14.5 min); baseline 1472 + 78 new accounting tests + 39 per-table schema checks over the 13 new tables |
| New accounting tests | 45 template unit tests, 13 reference-data, settings and freshness unit tests, 15 ledger and 5 API/CLI integration tests: all pass |
| `ruff format --check`, `ruff check` | clean |
| `mypy` (strict) | application code clean; the same 69 pre-existing errors in four Phase 4A test files |
| Frontend `pnpm typecheck` / `pnpm test` / `pnpm build` | clean / 98 passed (14 files) / succeeds (pre-existing chunk-size warning) |
| `alembic upgrade`, `check`, `downgrade 0012`, `upgrade` | pass |

Two defects were found by these tests and fixed before the report: functional values at 8
places made sub-cent realized FX unpostable (D-126), and re-running `init` at a later time was
refused as a conflicting reuse of the funding event (D-129).

## 12. Reconciliation results

Every integration scenario reconciles PASSED after each posting step (15 check families,
up to 64 stored results per run). The preview primary account: PASSED, 44 checks; the preview
controlled test account: PASSED, 64 checks. The tampering tests show FAILED runs with HIGH
results for CURRENCY_BALANCES, ENTRY_BALANCE, TRIAL_BALANCE and EQUITY, and the tampered
values left in place. Financial correctness is claimed only to the extent these tests cover.

## 13. Known limitations

* No execution source: trades exist only as controlled test transactions on test accounts
  (Phase 7). No risk reservations, sizing authorization or circuit breakers (Phase 6B).
* Valuation is posted only at explicit checkpoints (CLI); there is no scheduled worker, so
  between checkpoints the books show the last posted valuation (the dashboard says how many
  entries were posted since).
* Equity price freshness uses the XNYS calendar for every equity/ETF; FOREX instruments are
  held as currency balances (conversions), not positions. No corporate-action postings
  (splits, dividends); a split would need a dedicated template before positions exist across one.
* FX translation is re-targeted at checkpoints and released pro rata on conversion; foreign
  costs relieved between checkpoints leave a stale translation until the next one.
* Available capital is cash minus accrued costs at carrying value; margin is not modelled
  (negative cash is refused instead).
* Contract multipliers other than 1 are refused. Seven fiat currencies; crypto is an
  instrument, not a cash currency.
* Display amounts are rounded to cents; the ledger keeps the exact values.
* The preview database `atlas_phase6a_preview` was created on the dev PostgreSQL server for
  the browser check; it can be dropped. The dev database itself was not touched (it is still
  at migration 0012 until `alembic upgrade head` is run there).

## 14. Readiness for Phase 6B

Ready. The portfolio account row is the lock target the posting service already uses, which
is the locking strategy spec 14.3 asks reservations to use; equity, exposure, available
capital and per-position values are derived from one accounting equation; positions,
quantities and lot sizes are authoritative and reconciled; idempotency, immutability and
reconciliation patterns are in place. Phase 6B can add `risk_configurations`,
`risk_reservations` and circuit breakers on top without changing the ledger.

## 15. How to run

```bash
uv run alembic upgrade head
uv run python -m app.accounting.cli init
uv run python -m app.accounting.cli checkpoint
```

Then open the dashboard at **Portfolio & Accounting**. Details in `docs/operations.md`.
