# Evidence program and readiness scorecards (Phase 11A)

Pre-registered promotion and rejection criteria, historical data audits, paper-trading
evidence and readiness scorecards. Decisions D-222 to D-228 in [`decisions.md`](decisions.md);
operations in [`../operations.md`](../operations.md#evidence-and-readiness-phase-11a).

**Evidence, not authority.** Nothing in `app/evidence` submits an order, changes a risk limit,
enables, disables or promotes anything. Its records are read by a human, and by governance as
one more gate before a strategy approval (D-224). Atlas is never declared ready for live
trading: the best portfolio verdict is READY_FOR_REVIEW, which asks a human to consider the
next phase.

## Modules

| Module | Role |
|---|---|
| `app/evidence/protocol.py` | `EvidenceCriteria`: sample minimums, promotion rules, rejection rules, portfolio rules; hashed |
| `app/evidence/classify.py` | Pure judgement of out-of-sample and paper evidence against a protocol; portfolio verdict |
| `app/evidence/history.py` | Out-of-sample and backtest evidence read from stored 8B runs; benchmark Sharpe and baselines |
| `app/evidence/paper.py` | Paper evidence from the live system's records: closed positions, funnel, execution quality, reconciliation, alerts |
| `app/evidence/data_audit.py` | Coverage, calendar, listing, corporate-action, outlier, survivorship and point-in-time audit |
| `app/evidence/service.py` | Register protocols, store audits, build and store scorecards; `latest_entry` for governance |
| `app/evidence/queries.py`, `cli.py` | Read models and the command line |
| `app/api/v1/evidence.py` | `/api/v1/evidence` |
| `app/database/models/evidence.py` | Four append-only tables (migration 0022) |
| `frontend/src/pages/EvidencePage.tsx` | `/evidence`: the latest scorecard and the registered protocols |

## Flow

1. **Register a protocol** (operator; in the paper environment a named credential). It is
   stored once with its SHA-256 digest; identical criteria are refused; a change is a new
   version. Register it *before* the evidence it will judge exists.
2. **Audit the data** behind the evaluation (`evidence.cli audit`). FAIL means the data cannot
   support conclusions; WARN lists limitations that must be reported (survivorship is always
   reported for a hand-picked universe).
3. **Validate** each strategy with the 8B walk-forward runner, read each run's holdout once.
4. **Collect paper evidence**: run the autonomous paper system (10A/10B) in a paper database.
5. **Build a scorecard** naming the protocol, the runs, the audit and the paper account.

## Three sections, never pooled

| Section | Source | Role |
|---|---|---|
| BACKTEST | the final selection's development (in-sample) replay | context and degradation only; passes no criterion |
| OUT_OF_SAMPLE | stitched walk-forward test windows; DSR, PBO, cost stress, regime and direction segments; the first holdout read; benchmark and baselines on the same windows | historical criteria |
| PAPER | closed paper positions opened after the protocol's registration; observed sessions; execution quality | paper criteria |

## Classification of a strategy

1. **REJECTED** when a section with a sufficient sample is negative: net expectancy not
   positive, profit factor below 1.0, drawdown above 15%, or likely overfit (PBO > 0.5) and
   unprofitable out of sample.
2. **INSUFFICIENT_EVIDENCE** below the out-of-sample minimum (30 trades, 250 sessions), or when a
   historical criterion cannot be evaluated (regimes not covered, DSR missing, holdout unread or
   already re-read, benchmark missing).
3. **NOT_DEMONSTRATED** when a historical criterion fails: profit factor 1.2, positive
   expectancy, DSR 0.95, PBO 0.5, drawdown 10%, top-five share 50%, single-asset share 50%,
   profitable at 2x costs, positive net return, Sharpe at least the benchmark's on the same
   sessions, two trend regimes with 10 trades each and none losing, each sufficiently traded
   direction profitable, positive first holdout read.
4. **CONTINUE_PAPER** when every historical criterion passes and paper evidence is short of its
   minimum (30 trades over 60 observed sessions).
5. **NOT_DEMONSTRATED** when a paper criterion fails: the same profit, drawdown and
   concentration rules, paper per-trade returns not significantly below out-of-sample ones
   (one-sided Welch, alpha 0.05), mean entry shortfall no more than 10 bps above the backtest's
   assumption.
6. **CANDIDATE_FOR_REVIEW** otherwise. A human decides; governance still requires the 8B gates.

Continue paper trading: every class except REJECTED. REJECTED recommends disabling the strategy
in paper trading; an operator does it.

## Portfolio verdict

READY_FOR_REVIEW only when: 200 out-of-sample trades (a combined EVALUATION run, or the sum of
the strategy runs, labelled), 200 closed paper trades over 60 observed sessions, paper account
drawdown within 10%, no open critical alert, the latest accounting, reservation and execution
reconciliations all passed, the validation runs reproduced identically, the data audit PASS or
WARN, no single asset above half of the paper profit, and at least one strategy
CANDIDATE_FOR_REVIEW. A profitable-looking aggregate never hides a failing strategy: each
strategy is classified on its own.

## Governance gate (D-224)

A strategy candidate's approval needs, besides the 8B evidence gates, the newest scorecard entry
for the strategy built after SHADOW began, classified CANDIDATE_FOR_REVIEW, under a protocol
registered no later than the start of SHADOW. No setting disables it.

## Limitations

* Out-of-sample per-trade returns exist only for runs stored after migration 0022 (D-222).
* Benchmark and equal-weight baselines are split-adjusted price returns; dividends are not
  modelled anywhere in Atlas.
* Statistical tests assume independent trades, which overlapping positions violate (optimistic).
* The survivorship bias of a hand-picked universe is reported, not measured.
* Paper fills are simulated; they do not reproduce real fills, impact, liquidity or brokerage
  behaviour.
