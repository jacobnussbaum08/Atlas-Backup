# Deterministic risk engine (Phase 6B)

The risk engine decides whether a proposed trade is eligible to be *offered for
reservation*, how large it may be, and whether new entries are halted. It is deterministic,
reads only stored data, and is independent of strategies, models and LLMs. Decisions
D-138 to D-147 in [`decisions.md`](decisions.md) give the reasons; operations are in
[`../operations.md`](../operations.md#risk-engine-phase-6b).

**A PASS_FOR_RESERVATION is not an execution approval.** Nothing in Phase 6B reserves,
submits, executes or liquidates anything. Phase 6C adds atomic reservations
([`reservations.md`](reservations.md)): the assessment is re-run inside the reservation
transaction, with active reservations counted against every limit (engine 6C.1).

## Modules

| Module | Role |
|---|---|
| `app/risk/enums.py` | Results, check outcomes, breaker types, bases, policies |
| `app/risk/config.py` | `HardLimits`, `Preferences` (validated, immutable, Decimal-only), diff and hash |
| `app/risk/governance.py` | Operator-only configuration versions and audit records |
| `app/risk/models.py` | Assessment inputs and outputs (JSON round-trippable) |
| `app/risk/sizing.py` | Sizing arithmetic, rounding, instrument sizers (shares, crypto, forex stub) |
| `app/risk/assessment.py` | The pure assessment: every check, in a fixed order |
| `app/risk/periods.py` | NYSE risk days and weeks, baselines, loss, drawdown, loss allowance |
| `app/risk/breakers.py` | Breaker conditions, transitions, reset rules |
| `app/risk/data.py` | Point-in-time instrument, market, liquidity, FX and correlation inputs |
| `app/risk/state.py` | The ledger marked in memory; exposure; breaker events; snapshots |
| `app/risk/proposals.py` | Levels from strategy proposals and AI decisions (no scores) |
| `app/risk/service.py` | Assess, monitor, kill switch, resets, execution-safety interface, replay |
| `app/risk/queries.py`, `app/api/v1/risk.py` | Read models and the Risk Center API |
| `app/risk/cli.py` | Operator CLI (`python -m app.risk.cli`) |

```text
strategy proposal / AI decision / operator levels
          |  (entry, stop, target, horizon, expiry, ids: no scores)
          v
RiskService.assess --lock portfolio_accounts row FOR UPDATE------------------------+
   | configuration (latest version)       state.evaluate(): 6A ledger -> in-memory |
   | data.*: spec, reference, price,         valuation -> exposure, margin, loss,  |
   |         quote, liquidity, FX, corr       baselines, breaker conditions/events |
   v                                                                               |
assess(inputs)  (pure) -> result + every check + sizing calculations               |
   v                                                                               |
risk_assessments + risk_assessment_reasons + risk_exposure_snapshots + risk_events <+
   (guards: no pass while a breaker is tripped, latest config, budget <= limit,
    result consistent with checks, append-only)
```

## Configuration

Two separate parts per immutable version (`risk_configuration_versions`):

**Hard limits** (defaults are the Atlas starting values: spec 14.1 plus conservative
framework values; **not optimal or recommended settings**):

| Limit | Default |
|---|---:|
| Risk per trade (of current equity) | 0.5% |
| Maximum open positions | 8 |
| Daily loss limit | 2% of start-of-day equity |
| Weekly loss limit | 5% of start-of-week equity |
| Maximum drawdown | 10% of peak equity |
| Maximum leverage | 1x (the ledger has no margin loan; > 1 is refused) |
| Maximum gross exposure | 100% of equity |
| Maximum net exposure | 100% |
| Single asset | 20% |
| Asset class (gross) | EQUITY 100%, ETF 100%, CRYPTO 0% (disabled), FOREX 0% (disabled) |
| Sector (gross) | 30% |
| Correlated cluster (gross) | 40% |
| Short exposure | 30% |
| Margin usage (short collateral beyond proceeds) | 50% |
| Shorts enabled; short budget fraction | yes; 0.5 of the per-trade budget |
| Minimum short collateral rate | 50% (Reg T 150% initial = proceeds + 50%) |
| Missing margin data | REJECT |
| Stop required; minimum stop distance | yes; 10 bps |
| Entry within this distance of the fresh price | 2% |
| Liquidity: max participation of median daily dollar volume | 1% |
| Maximum spread | 50 bps |
| Missing correlation | ASSUME_CORRELATED |
| Unclassified sector | POOL (one UNCLASSIFIED bucket) |
| Reconciliation must be current | yes |

**Preferences** (ASSUMPTIONS, recorded with each assessment; they never relax a hard limit):
commission 0.005 per unit, minimum 1.00 per leg; fees 1 bp per leg; entry slippage 5 bps;
stop slippage 10 bps; spread 25 bps when no fresh quote (quotes older than 30 minutes are
not fresh); FX conversion 10 bps per leg; short borrow 5% a year; holding period 10 days
when the proposal has none; assumed stop 10% (only if `require_stop` is off); correlation
threshold 0.70, correlation and liquidity measurements at most 7 days old; assessments valid
for 300 seconds.

Changes: `governance.change` with an `operator:<name>` actor and a reason; the version, the
field diff, previous and new configuration go to `risk_configuration_versions` and the
audit log. A database CHECK refuses any other actor; triggers keep versions gapless and
append-only. No API endpoint and no environment variable can change a limit.

## Position sizing

```text
risk budget    = floor_cents(current equity x risk per trade [x 0.5 for a short])
                 capped by the loss allowance left (see Loss limits)
entry', stop'  = entry moved adversely, stop moved away from entry, on the tick grid
entry fill     = entry' x (1 + half spread + entry slippage)     (short: x (1 - ...))
exit fill      = stop'  x (1 - half spread - stop slippage)      (short: x (1 + ...)), >= 0
loss per unit  = |entry fill - exit fill| + fees x (entry fill + exit fill)
                 + FX cost x (entry fill + exit fill)  [foreign instruments]
                 + borrow rate x entry fill x days / 365  [shorts]          (x multiplier)
loss(q)        = q x loss per unit + 2 x max(min commission, q x commission per unit)
q_risk         = min(B / (unit + 2c), (B - 2 min) / unit)   in functional terms
q              = floor_to_lot(min(q_risk, every capacity, requested quantity))
verify         : ceil_functional(ceil_quote(loss(q)) x rate) <= B, else q -= lot
```

Capacities (units that fit; the smallest binds and is named in the result):

* available capital: unrestricted cash − accrued costs − short collateral; a long needs
  its fill price plus entry costs, a short needs its collateral plus entry costs;
* gross exposure and leverage: limit x equity − gross − pending;
* net exposure: limit x equity ∓ net (direction-aware);
* single asset, asset class, sector, correlated cluster: limit x equity − current gross
  exposure of that group;
* short exposure and margin usage (shorts);
* liquidity: participation x median daily dollar volume.

A result below the instrument's minimum quantity is REJECTED with the binding constraint.
Tick size: the provider's increment, else SEC Rule 612 for US equities (0.01 at or above
1.00, 0.0001 below); crypto requires a reported increment. Lot size: `instrument_specs`
(never guessed). Forex: refused until contract specifications and forex positions exist.

## Exposure

The ledger (6A `load_state`) is marked to market **in memory** with the 6A valuation
template and fresh prices/rates (6A pricing rules); nothing is posted. Per asset: long =
1100 + 1150, short = 2100 + 2150 (functional). Gross = long + short; net = long − short;
class, sector and cluster exposures are gross (never netted). Margin used = Σ short exposure
x collateral rate. Pending exposure = the gross exposure of active reservations (Phase 6C).

Correlated with the candidate: same asset, same operator cluster tag, |correlation| ≥ 0.70
(newest OK Phase 3B PAIR measurement), or unmeasured under ASSUME_CORRELATED. Sector and
cluster tags come from `risk_asset_classifications` (operator or fixture; effective-dated).

## Assessment

Checks run in a fixed order and are all recorded (`risk_assessment_reasons`, outcome PASS,
FAIL, UNKNOWN, HALT, RESTRICT or INFO, with reason code, measured value and limit):
breakers; configuration; mark; equity; reconciliation; loss state; loss allowance;
expiry; entry; stop (present, side, distance); instrument (sizer, class enabled, tradable,
currency, lot size); short (enabled, supported, shortable, borrow, margin); price
freshness; entry vs market; stop already breached; FX rate; spread; liquidity data;
opposite position; position count; sector classification; then sizing and capacities.

Result: any HALT → **RISK_HALTED**; else any FAIL → **REJECTED**; else any UNKNOWN →
**INSUFFICIENT_DATA**; else **PASS_FOR_RESERVATION** with the permitted quantity, budget,
potential loss, notional, estimated costs and exposure after. Valid for the TTL.

## Loss limits and circuit breakers

Calendar: XNYS (America/New_York) for the whole portfolio; a risk day ends at the regular
close; a risk week is the ISO week of the risk day. Baseline: the latest COMPLETE equity
observation during the boundary session, else the earliest after the close and before the
next open, else the opening funding if funded in the period, else the first observation in
the period (flagged). Loss = fall in investment return since the baseline. Peak equity =
highest observed investment return (≥ 0) + capital.

| Breaker | Trips when | Recovery |
|---|---|---|
| DAILY_LOSS | daily loss ≥ 2% of baseline | operator reset once clear |
| WEEKLY_LOSS | weekly loss ≥ 5% of baseline | operator reset once clear |
| MAX_DRAWDOWN | drawdown ≥ 10% of peak | operator reset once clear |
| EXCESSIVE_EXPOSURE | gross, leverage, net, short, margin or count above limit; equity ≤ 0 | operator reset once clear |
| DATA_QUALITY | a position or currency cannot be marked to fresh inputs | automatic, recorded |
| RECONCILIATION_FAILURE | latest reconciliation FAILED | operator reset once a run passes |
| EXECUTION_SAFETY | reported via `report_execution_safety` (Phase 7 producer) | operator reset |
| KILL_SWITCH | engaged by an operator (CLI or API) | operator release (CLI) |

Response to every trip: new entries blocked; monitoring continues; risk-reducing operations
remain permitted for the execution phases; the event is recorded with measurements; nothing
is liquidated; no silent reset.

## Database (migration 0014)

`risk_configurations`, `risk_configuration_versions`, `risk_asset_classifications`,
`risk_exposure_snapshots`, `risk_period_baselines`, `risk_assessments`,
`risk_assessment_reasons`, `risk_circuit_breaker_events`, `risk_events`, and the view
`risk_circuit_breaker_states`. All tables append-only. Guards: configuration versions
(operator actor, gapless, owner), breaker events (gapless sequence, valid transition,
operator-only resets except DATA_QUALITY, operator-only kill switch), assessments (a pass:
latest configuration, budget ≤ equity × configured risk, no tripped breaker, sized within
budget; result consistent with its checks at COMMIT; checks only in the same transaction).

## What is and is not guaranteed

Tested: the interfaces refuse non-operator actors; the database refuses non-operator
configuration versions, out-of-sequence breaker events, system resets of manual breakers,
passes while a breaker is tripped, passes against an old configuration or over the
configured budget, passes whose checks failed, late checks, and any UPDATE/DELETE/TRUNCATE
of risk records. Concurrent assessments and breaker trips serialize on the account row.

Not guaranteed by an assessment alone: a pass reserves nothing; two passes may together
exceed a limit. Phase 6C reservations close this (see [`reservations.md`](reservations.md)). The
database cannot recompute a sizing, so a direct insert by someone with database write
access could still record a pass that satisfies the guards but not every limit; a
reservation, however, is approved only from an assessment recorded in its own transaction
under the lock (Phase 6C).
