# Event-driven historical backtesting (Phase 8A)

Replays stored historical data through Atlas's own strategy, risk, execution and accounting
rules. Decisions D-173 to D-184 in [`decisions.md`](decisions.md); operations in
[`../operations.md`](../operations.md#backtesting-phase-8a).

**A simulation over past data, not a forecast.** Nothing here contacts a provider or a
broker, reads a live stream, or writes to a live trading table. Results go to the
`backtest_*` tables only. An in-sample backtest is not evidence of future performance.

## Modules

| Module | Role |
|---|---|
| `app/backtesting/config.py` | `BacktestRequest` (what to test) and `ResolvedConfig` (everything pinned: strategy versions and parameters, risk configuration, execution model, dataset knowledge time, policies, seed, code version) |
| `app/backtesting/data.py` | `HistoricalDataset` loaded once at the knowledge time; `PointInTimeBars` (the `DailyBarSource` given to `compute_features`); split inference; point-in-time reference data; dataset fingerprint |
| `app/backtesting/engine.py` | `BacktestEngine`: the session-by-session replay; proposals, reservations, orders, trades, snapshots, events |
| `app/backtesting/broker.py` | `SimOrder` (7A state machine) and `evaluate` (7B fill model on a session's bar, plus the gap rule) |
| `app/backtesting/portfolio.py` | `RiskBook`: the 6B `PortfolioView` of the in-memory ledger, loss baselines, breaker transitions, the simulated operator |
| `app/backtesting/ledger.py` | `SimulatedLedger`: 6A templates, validation and invariants on an in-memory ledger; independent reconciliation |
| `app/backtesting/metrics.py` | Performance metrics and benchmark comparisons |
| `app/backtesting/service.py` | Resolve, create, execute, store, reproduce, fail stale runs |
| `app/backtesting/queries.py`, `cli.py`, `runtime.py` | Read models, CLI, API wiring (one background thread per API process) |
| `app/api/v1/backtests.py` | The REST API |
| `app/database/models/backtesting.py` | Tables (migration 0018) |
| `frontend/src/pages/Backtest*.tsx` | Dashboard pages `/backtesting` and `/backtesting/:runId` |

## What is shared with paper trading

| Concern | Shared code | What the backtest supplies |
|---|---|---|
| Strategy decision | `strategies.engine.decide` (also called by `StrategyEngine.run`), `ineligibility`, `compute_features`, `validate_drafts`, `periods.expires_at` | Point-in-time bars (`PointInTimeBars`), eligibility from point-in-time reference data, mode HISTORICAL, basis RECONSTRUCTED |
| Risk | `risk.assessment.assess` (every 6B check, sizing, capacities), `periods`, `breakers`, `risk.data.correlations` | `PortfolioView` from the in-memory ledger; `InstrumentData`/`MarketData` at the decision time; liquidity recomputed with 3A `metrics.compute` |
| Reservations | 6C amounts (`calculations["reservation"]`), `ReservedExposure`, proportional consumption, release of the remainder | Held in memory per proposal |
| Orders | 7A `state_machine.next_status` | Immediate simulated acknowledgement and cancellation |
| Fills | 7B `fill_model.evaluate`, `costs`, `ExecutionModel` (content-addressed, registered in `execution_models`) | One completed daily bar per evaluation; the gap rule; brackets |
| Accounting | 6A `TEMPLATES`, `ledger.validate`, `state.invariant_violations`, `figures`, `valuation` | In-memory journal, stored with the run |

## Timeline of one session

```text
t = regular close + settle (default 1 h)        (a session's bar and period become known)
 1 corporate actions effective at the session's open (split factor change)
 2 execution against the session's bar, assets in symbol order:
      entries -> protective stops -> take-profits -> market exits
      (a bar is used once per order and only if it started after the order arrived)
 3 entries whose validity ended by the close expire; reservation remainders released
 4 valuation of open positions at the RAW close (6A valuation); short borrow accrued
 5 independent reconciliation of the ledger (a failure stops the run)
 6 equity snapshot; 6B breaker transitions
 7 holding-horizon exits (market orders for the next session)
 8 decisions: the universe at t; every strategy x asset x period completed at t
      -> features (point in time) -> decide() -> 6B assess() with reservations
      -> reservation -> entry order (+ bracket: stop now, take-profit after the fill bar)
cursor row committed (progress)
```

## Look-ahead and survivorship safeguards

| Rule | Where | Test |
|---|---|---|
| Only sessions completed and settled by the cutoff reach a feature | `completed_daily` (4A) + `PointInTimeBars` refuses later sessions | `test_the_source_refuses_sessions_not_completed_by_the_cutoff`, `test_every_bar_served_to_a_feature_was_completed_by_the_decision` |
| A bar's availability is close + settle, never its retrieval time | `data._load_bars` | as above |
| Rows ingested after the knowledge time are invisible | `ingested_at <= known_at` on every load | `test_a_completed_run_is_reproducible_from_its_stored_configuration` |
| Future prices cannot change earlier decisions | replay order | `test_future_observations_cannot_change_earlier_decisions` (two databases differing only after 14 Oct) |
| No future split in history | split factor of the decision's newest session | `test_history_before_a_split_is_served_at_the_level_known_then`, `test_a_split_while_holding_is_carried_without_a_fictitious_loss` |
| News published within `news_delay_minutes` of the cutoff does not count | `StoredContext.news_delay` | `test_historical_news_counts_only_after_the_conservative_delay` |
| Universe membership as recorded at the decision time | `membership.members_at(at=t, known_at=t)` | `test_point_in_time_universe_membership_is_respected_without_survivorship` |
| An asset delisted later still trades while it was a member | asset status from the presence of a bar, not today's status | same test |
| An entry never fills on its signal's bar | order arrival after `t` | `test_events_replay_chronologically_and_nothing_uses_a_later_observation` |

## Execution assumptions (daily bars)

The 7B model (`atlas-backtest-daily` v1) with `bar_timeframe = 1Day`. Costs: commission 0.005/share
(minimum 1.00 per order, cap 1% of notional); sell-side fees 0.00278% of notional plus
0.000166/share (maximum 8.30); short borrow 3%/year (360-day basis); slippage 2 bps; impact
10 bps at full participation; assumed half spread 5 bps when there is no quote. Liquidity:
5% of a bar's volume per order and session (passive limits and the bar fallback). These are
the 7B defaults, which are assumptions, not measurements.

* LIMIT entries (the default) at the worst entry price fill only on a later bar that trades
  strictly through them, at the limit. Touching is not filling.
* MARKET orders and triggered stops fill at the bar's close, widened by the assumed half
  spread, slippage and impact, and clamped to the bar's range.
* A triggered stop fills no better than its stop. After a gap it fills no better than the open.
* A take-profit is not filled by a bar that also reached the stop (stop first), and never in
  its entry's bar.
* Partial fills follow the participation caps; protective orders are resized with the position.

## Limitations

* **No intrabar path.** Fills use completed daily bars only. A stop hit intraday is not known
  until the session's bar is complete.
* **No historical quotes or trades.** IEX quotes are stored only from the live stream (late
  October 2026), so spreads are assumed. IEX volumes are a small share of consolidated volume,
  so participation caps understate liquidity.
* **Revisions are reconstructed.** The newest bar revision ingested by the knowledge time is used.
* **Reference data is reconstructed** for dates before Atlas first observed an asset (labelled
  `RECONSTRUCTED_REFERENCE`). An asset already delisted when Atlas first saw it cannot be
  shown tradable.
* **No dividends.** Long returns are understated and short returns overstated by dividends.
  Benchmarks are price returns for the same reason.
* **Correlation and sector data** rarely exist for past dates, so `ASSUME_CORRELATED` and the
  pooled UNCLASSIFIED sector bind (as they would live). This can restrict gross exposure
  sharply.
* **Weekly strategies** need long histories; with the stored SPLIT history (from September
  2025) weekly evaluations are mostly INSUFFICIENT_DATA.
* **No ML or LLM** in the decision path (Phase 8B).
