# Portfolio accounting (Phase 6A)

Atlas keeps the simulated paper account in an immutable double-entry ledger in PostgreSQL.
Every balance, position, P/L figure and snapshot is derived from posted journal lines; the
ledger is authoritative (spec 16.6). This document is the reference for the accounting
conventions. Decisions D-125 to D-137 in [`decisions.md`](decisions.md) give the reasons.

**Paper only.** No order engine, broker or execution source exists. The primary paper account
receives only its simulated opening funding and valuation checkpoints. Trades, costs and
conversions are posted only as *controlled test transactions* on separate CONTROLLED_TEST
accounts, enforced by the posting service and by the database.

## Modules

| Module | Role |
|---|---|
| `app/accounting/money.py` | Decimal context, rounding rules, currencies |
| `app/accounting/chart.py` | Chart of accounts |
| `app/accounting/ledger.py` | Entry drafts, balancing rules R1/R2, line rules |
| `app/accounting/state.py` | Ledger state (balances, positions), average-cost relief, invariants |
| `app/accounting/templates.py` | Versioned posting templates (pure functions) |
| `app/accounting/reference.py` | FX rates, instrument specifications (lot sizes) |
| `app/accounting/posting.py` | Posting service: lock, idempotency, validation, persistence |
| `app/accounting/reconciliation.py` | Automatic reconciliation checks |
| `app/portfolio/accounts.py` | Account creation and opening funding |
| `app/portfolio/pricing.py` | Valuation prices with provenance and freshness |
| `app/portfolio/valuation.py` | Valuation checkpoints and snapshots |
| `app/portfolio/figures.py` | Figures from the accounting equation |
| `app/portfolio/queries.py`, `app/api/v1/portfolio.py` | Read-only API |
| `app/accounting/cli.py` | Operator CLI (the only write path) |

## Chart of accounts

Normal side: assets and expenses are debit-normal; liabilities, equity and income are
credit-normal. "Per asset" accounts carry the asset on every line.

| Code | Account | Type | Per asset | Lines |
|---|---|---|---|---|
| 1000 | Cash | Asset | no | amount |
| 1010 | Restricted cash: short-sale proceeds | Asset | yes | amount |
| 1100 | Long securities at cost | Asset | yes | amount |
| 1150 | Long securities valuation adjustment | Asset | yes | amount |
| 1900 | FX conversion clearing | Equity | no | amount |
| 1910 | FX translation adjustment | Asset | no | functional-only |
| 2100 | Short securities liability at proceeds | Liability | yes | amount |
| 2150 | Short securities valuation adjustment | Liability | yes | amount |
| 2200 | Accrued fees payable | Liability | no | amount |
| 2210 | Accrued financing payable | Liability | no | amount |
| 3000 | Contributed capital: simulated opening funding | Equity | no | amount |
| 4000 | Realized trading gains and losses | Income | optional | amount |
| 4100 | Unrealized valuation changes | Income | optional | amount |
| 4200 | Realized FX gains and losses | Income | optional | amount or functional-only |
| 4300 | Unrealized FX translation | Income | no | functional-only |
| 5000 | Commissions | Expense | optional | amount |
| 5100 | Fees | Expense | optional | amount |
| 5200 | Borrow costs | Expense | optional | amount |
| 5300 | Funding costs | Expense | optional | amount |

The chart is defined in code and seeded by migration 0013; a unit test keeps them identical.
Accounts are never edited or removed; a change is a new account in a new migration.

## Journal lines and balancing rules

A line has a ledger account, a **currency**, a side (debit or credit), an **amount** in that
currency, a **functional amount** in the account's reporting currency (USD), the **rate
basis** and rate used, an FX-rate reference where one was used, and the asset where the account
requires one.

Every posted entry satisfies, checked in Python before writing and again by the deferred
database trigger `atlas_journal_entry_balanced` at COMMIT:

* **R1**: for every currency, debit amounts = credit amounts. Currencies never mix: a
  conversion goes through the FX clearing account so each currency balances on its own.
* **R2**: functional debits = functional credits.
* at least two lines.

Line rules (Python and `atlas_journal_line_guard`):

* an ordinary line has a positive amount with at most the currency's minor units;
* a functional-only line has a zero amount and a positive functional value, is in a
  foreign currency, and is allowed only on 1910, 4200 and 4300;
* a line in the functional currency has functional amount = amount, rate 1, basis FUNCTIONAL;
* rate basis: FUNCTIONAL; SPOT (amount × a timestamped `fx_rates` row, referenced); CARRYING
  (the historical average carrying rate of the balance relieved); TRANSACTION (implied by a
  conversion's legs); NONE (functional-only FX difference lines).

Lines and quantity movements can only be added in their entry's own transaction; entries are
numbered 1..n per account without gaps; nothing is ever updated or deleted (append-only
triggers on every ledger table). **Corrections are reversal entries** that mirror every line
(same amounts and functional values, opposite sides).

## Rounding and precision

* Every value is a `Decimal`; floats are refused. Arithmetic runs in a 60-digit context
  that traps invalid operations, division by zero and overflow.
* Rounding is always **half-even** and only at these points: a currency amount computed from
  quantity × price or from a proportional relief (to the currency's minor units: USD 2,
  JPY 0); a functional value (to the functional currency's minor units, cents for USD); an
  inverted FX rate (12 places).
* Inputs are never rounded: an amount with too many decimals, a quantity that is not a
  multiple of the instrument's lot size, or one below its minimum is rejected.
* Relieving a whole balance always relieves all of it (amount and functional value), so a
  closed position or an emptied currency never keeps a residue.
* Columns: amounts `NUMERIC(28,8)`, quantities `NUMERIC(38,18)`, prices `NUMERIC(30,10)`,
  rates `NUMERIC(30,12)`.

## Posting rules (templates, version 1)

Notation: *c* is the asset's currency; values in the functional currency use the rate
basis given. Commissions and fees of trades are expensed (5000, 5100), paid from 1000 Cash,
never capitalised into cost.

**opening_funding**: Dr 1000 Cash / Cr 3000 Contributed capital, in the functional currency,
only as the account's first entry, once per account (unique index). It is simulated capital,
never a return.

**long_purchase** (*q* at *p*): N = round_c(q × p). Dr 1100 [asset] N / Cr 1000 N, at the
cash's carrying value. Quantity +q, cost +N. Refused when the asset is held short or when
cash would go negative (there is no margin loan in the chart).

**long_sale** (*q* at *p*, position Q with cost C): proceeds R = round_c(q × p); cost relieved
C·q/Q (average cost; all of it when closing). Dr 1000 R at spot / Cr 1100 cost relieved at
carrying; 4000 realized = R − cost relieved (at spot); the remaining functional difference is
the FX component of the cost relieved, a functional-only line on 4200. The valuation
adjustment 1150 is released pro rata against 4100, so 4100 only ever describes open
positions. Quantity −q. Selling more than held is refused (close, then open a short).

**short_open** (*q* at *p*): P = round_c(q × p). Dr 1010 Restricted cash [asset] / Cr 2100
Short liability [asset], both P at spot. **The proceeds are collateral: they are not
available cash and add nothing to equity** (asset and liability rise together). Quantity −q.

**short_cover** (*q* at *p*, short Q with proceeds P): proceeds relieved P·q/Q; buy-back cost
K = round_c(q × p). Dr 1000 / Cr 1010 the proceeds relieved (collateral released); Dr 2100
the proceeds relieved (liability extinguished); Cr 1000 K at carrying; 4000 realized =
proceeds relieved − K; FX difference to 4200; 2150 released pro rata against 4100.

**fee** (commission or fee): Dr 5000/5100 / Cr 1000 (paid, at carrying) or Cr 2200 (accrued,
at spot).

**financing_cost** (borrow or funding): Dr 5200/5300 / Cr 2210 (accrued, default) or Cr 1000.

**accrual_settlement**: Dr 2200/2210 at the payable's carrying value / Cr 1000 at the cash's
carrying value; a difference (foreign currency) is realized FX on 4200.

**fx_conversion** (sell *a* of S, buy *b* of B): bought legs Dr 1000[B] / Cr 1900[B] *b* at
the exchange's functional value V (the functional-currency leg when one side is functional,
otherwise *b* at a spot rate); sold legs Dr 1900[S] / Cr 1000[S] *a* at S's carrying value.
V − carrying is realized FX: Dr/Cr 1900 / 4200 in the functional currency, which brings the
clearing account's functional value back to zero. The sold currency's translation adjustment
1910 is released pro rata against 4300. The executed rate is recorded in `fx_rates` with
source CONVERSION (documentation only: it never values anything).

**valuation** (checkpoint): for each position with a fresh price (and rate): 1150 (long)
re-targeted to market value − cost, 2150 (short) to buy-back value − carrying proceeds,
against 4100, the change at the checkpoint's spot rate. Then per foreign currency with a
fresh rate: 1910[c] re-targeted to *net balance-sheet amount in c × spot − its historical
functional value*, against 4300 (functional-only lines). Nothing is posted when nothing
changed.

**reversal**: mirrors every line; negates the quantity movements. Refused for opening
funding, for reversals, for an entry already reversed, and when a later entry touches the
same assets (reverse those first).

## Asset quantity ledger

`asset_quantity_ledger` holds one row per position change: signed quantity delta (long
positive, short negative), quantity after, cost basis delta and after (long cost or short
proceeds, in the asset currency and in functional value), the price, and the realized P/L of
the movement. Average entry price = cost after / |quantity after|. Lot size, minimum and
multiplier come from `instrument_specs` (effective-dated; a posting without one is refused).
Holdings reconcile to the ledger: cost after = 1100 (long) or 2100 (short) per asset.

## Multi-currency treatment

* Balances are kept per ledger account **and currency**; the original currency and amount of
  every line are preserved together with its functional (USD) value.
* **Historical carrying value.** Foreign currency is carried at what it cost in functional
  terms. Spending or converting it relieves the average carrying value.
* **FX clearing (1900).** A conversion's two currencies each balance against 1900; 1900's
  functional value is always zero, its per-currency balances show currency exchanged and
  still held.
* **Realized FX (4200)**: functional value received minus historical carrying value given up,
  on conversions, on foreign-currency sales and covers (the FX component), and on settling a
  foreign payable.
* **Translation (1910 / 4300)**: at each checkpoint the net balance-sheet position in each
  foreign currency is revalued at a fresh spot rate; the difference from its historical
  functional value is the unrealized FX translation. It is released when the currency is
  converted.
* **Rates** come from `fx_rates` (1 base = rate quote, observed and recorded timestamps,
  source, provenance). A posting needs a rate no older than `ACCOUNTING_FX_RATE_MAX_AGE_HOURS`
  (refused otherwise); a checkpoint flags a stale or missing rate and keeps the last
  translation. TEST_FIXTURE rates are usable only by CONTROLLED_TEST accounts.

## Accounting equation and portfolio figures

In the functional currency:

```text
total assets      = 1000 cash + 1010 restricted proceeds + 1100 long cost + 1150 long adjustment
                    + 1910 FX translation
total liabilities = 2100 short proceeds + 2150 short adjustment + 2200 + 2210 accrued costs
equity            = total assets - total liabilities
                  = contributed capital + realized P/L + unrealized valuation
                    + realized FX + unrealized FX - commissions - fees - borrow - funding
```

Both sides are computed independently from the lines; reconciliation checks they agree.
Equity is not "cash + position value": a short position is an asset (restricted proceeds)
and a liability carried at its buy-back value, and accrued costs and FX are explicit.

* **Investment return** = equity − contributed capital (funding is never a return).
* **Available cash** = unrestricted cash (1000) only.
* **Available capital** = unrestricted cash − accrued unpaid costs, at carrying value. Margin
  and risk reservations do not exist yet (Phase 6B).
* **Exposure**: long = 1100 + 1150; short = 2100 + 2150 (as a liability); gross = long +
  short; net = long − short.

## Valuation and stale prices

A checkpoint (`python -m app.accounting.cli checkpoint`) chooses for each open position the
newest of: last VALID live trade, midpoint of the last VALID quote, close of the last complete
VALID **unadjusted** bar, all observed and available at or before `as_of`. Freshness:
equities/ETFs must be at most 30 minutes old during the NYSE regular session, otherwise from
the latest session that has opened; crypto at most 60 minutes. A stale or missing price, rate
or instrument specification is **flagged** (snapshot STALE or INCOMPLETE, issue listed); the
position keeps its last posted valuation. No price is substituted, interpolated or invented.
The snapshot records every position's price, source, observation time and provenance.

## Reconciliation

`python -m app.accounting.cli reconcile` (also after `init` and every `checkpoint`) runs, under
a share lock: entry balance (R1/R2), trial balance per currency and functional, sequence
continuity, the balance cache against recomputed balances, non-negative cash, quantity-ledger
running totals, positions against ledger cost, restricted proceeds against short liabilities,
4000 against recorded realized P/L, 4100 against valuation adjustments, FX clearing at zero,
1910 against 4300, the accounting equation, and the latest snapshot against the ledger as of
its sequence (including quantity × price for marked positions). Every check is stored. A
failure is severity HIGH, logged as `accounting.reconciliation_failed`, written to the audit
log and shown on the dashboard. **Nothing is ever corrected automatically.**
