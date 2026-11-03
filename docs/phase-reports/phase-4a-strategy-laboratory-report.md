# Phase 4A — Strategy Laboratory Foundation: completion report

Date: 2026-11-03. Prompt: [`docs/prompts/phase-4a-strategy-laboratory-foundation.md`](../prompts/phase-4a-strategy-laboratory-foundation.md).
Decisions: D-082 to D-089 in [`docs/architecture/decisions.md`](../architecture/decisions.md).

## Summary

Phase 4A delivers the framework for six independent strategy families:

* a standard strategy interface and lifecycle;
* a central feature registry;
* point-in-time feature snapshots;
* immutable strategy versions;
* an idempotent, concurrent evaluation engine and worker;
* a standardized proposal schema;
* an API and a Strategy Laboratory dashboard page.

The six families have their interfaces, parameter schemas and feature dependencies. They have
**no trading logic** (Phase 4B), so they are never evaluated and cannot be enabled. The
engine was tested end to end with TEST FIXTURE strategies only.

Nothing in this phase sizes a position, approves a trade, or talks to a broker. A proposal has
no quantity and no approval state.

## Pre-flight verification

* Phase 3A, 3B and 3C baseline: 989 backend tests passed, 1 skipped (no test Redis).
  Migrations were at head 0008.
* Audits: the four P1s from the 3A audit were fixed in `25ab296` and `c868d5b`. No audit has
  run since, and there was no open P0 or P1, so work proceeded.

## Architecture

```text
triggers (bar close / news)  ->  engine.plan  ->  engine.claim (advisory lock, idempotency key)
     -> inputs.compute_features (completed periods, point in time)  ->  snapshot (content hash)
     -> strategy.evaluate(ctx)  [only if every required feature is VALID]
     -> proposals.validate_drafts  ->  engine._finalize (one transaction, ownership check)
```

| Module | Role |
|---|---|
| `strategies/base.py` | `Strategy` interface, `StrategyParameters`, `EvaluationContext`, `ProposalDraft`, `Evidence` |
| `strategies/families/*.py` | Six modules, each exposing `STRATEGY` (interfaces only) |
| `strategies/registry.py` | Discovery (`pkgutil`) and validation at registration |
| `strategies/catalog.py` | Catalog sync, immutable versions and configurations, enable/disable |
| `strategies/inputs.py`, `periods.py` | Point-in-time inputs, completed periods, calendar expiry |
| `strategies/engine.py` | Claims, evaluation, failure isolation, concurrency, expiry, supersession |
| `strategies/triggers.py` | Detection of completed periods and news events |
| `strategies/queries.py` | Read models; derived proposal status |
| `feature_engine/registry.py`, `bar_features.py`, `stored.py` | Feature registry and its computation |
| `workers/strategy_evaluation.py` | Evaluation worker (compose `strategy-worker`, off by default) |
| `api/v1/strategies.py` | REST API |

## Strategy interfaces

Every strategy declares:

* a unique `strategy_id`;
* a `family`, `name` and `description`;
* an immutable `logic_version`;
* `implemented`;
* supported asset classes and timeframes;
* an evaluation `schedule` (BAR_CLOSE and/or NEWS_EVENT);
* a frozen `Parameters` model (extra keys rejected), which includes the timeframes, directions, signal expiry in periods (`signal_ttl_periods`), holding horizon and minimum strength.

It implements:

* `required_features(params, timeframe)`;
* `evaluate(ctx, params)`.

**Enabled/disabled:** status is an event log; a strategy without events is disabled.

**Historical evaluation:** use mode `HISTORICAL` with a past cutoff and basis `RECORDED` or
`RECONSTRUCTED`, through the CLI or the engine. Strategy code is the same in both modes.

**Validation at registration:**

* id format;
* metadata;
* default parameters;
* timeframes the data layer serves (`1Day`, `1Week`);
* every required feature resolving on every supported timeframe.

Adding a strategy means adding one module under `families/`; no other module changes.

| Family | Parameters (beyond the shared ones) | Key features |
|---|---|---|
| Momentum | lookback, acceleration, persistence, RSI, trend EMA, volume windows, min volume ratio | returns (incl. lagged), RSI, EMA, volume ratio, optional ADX and market trend regime |
| Trend following | fast/slow EMA, long SMA, ADX length, min ADX, ATR, pullback and trailing ATR multiples | EMAs, SMA, ADX, ±DI, ATR, optional asset trend regime |
| Mean reversion | RSI bounds, Bollinger length/k, z-score, min abs z-score, ATR, trend-filter SMA, reversal confirmation | RSI, Bollinger bands/%B, z-score, ATR, SMA(200), return |
| Breakout | channel length, contraction history, max width percentile, volume, confirmation and failed-breakout ATR | prior high/low, band-width percentile, volume ratio, ATR |
| Volatility | ATR short/long, expansion/compression ratios, band-width history, volatility length, stop ATR | ATR ratio, ATR, band-width percentile, realized volatility, optional volatility regime |
| Event driven | categories, look-back hours, min relevance, confirmation return, ATR, volume | news counts per category (**required**), return, ATR, volume ratio |

The event-driven module consumes the Phase 3C news features. When news coverage is missing,
those features are UNAVAILABLE and the evaluation is blocked; missing coverage is never
counted as "no news". It is scheduled on news events as well as completed bars.

## Feature registry

There are 30 definitions (`feature-registry-v1`). Each one records:

* name and version;
* source;
* calculation method;
* typed parameters with bounds;
* timeframes;
* required data;
* availability rule;
* value kind;
* window rule;
* a sha256 of the definition.

Each resolved feature gets a canonical key, such as `rsi.v1(length=14)@1Day`.

**Bar features** (26): close, return (with lag), log return, SMA, EMA, RSI, ATR, ATR%, ATR
ratio, ADX, ±DI, MACD line/signal/histogram, Bollinger middle/upper/lower/%B/width/width
percentile, z-score, volume ratio, realized volatility, prior high/low.

**Stored features** (4):

* `regime_label` and `regime_confidence`, read from the Phase 3B classifications;
* `news_event_count` and `news_hours_since_latest`, read from the Phase 3C events.

**Reuse:** calculations call the existing Phase 3B `indicators`. New indicators were added only where missing: RSI, MACD, Bollinger bands, volume ratio and prior extremes. Phase 3B's private volume-ratio helper now delegates to the shared function. The formula is identical and all Phase 3B tests still pass.

**Completed candles only:** a period counts only once its close plus `STRATEGY_SETTLE_SECONDS` has passed. Weekly periods are complete ISO weeks with every session's bar present.

**No look-ahead:**

* RECORDED inputs are bars, regimes and news Atlas had stored by the cutoff;
* RECONSTRUCTED re-evaluations are labelled as such;
* database CHECKs reject RECORDED snapshots whose inputs became available after the cutoff, and periods after the cutoff.

**Validity statuses:** VALID, INSUFFICIENT_DATA, MISSING_INPUTS, UNDEFINED, UNAVAILABLE. Nothing is filled with zeros.

**Fixed windows:** each value uses exactly its window of periods, so storing more history later cannot change it.

## Proposal schema (`strategy_proposals`)

Each proposal records:

* proposal id;
* evaluation id;
* strategy id and version;
* asset id;
* market (asset class);
* direction (LONG/SHORT);
* timeframe;
* mode and basis;
* signal timestamp and data cutoff;
* feature snapshot ids;
* reference entry;
* optional invalidation level and target reference, CHECKed to the correct side of the entry;
* holding horizon in periods;
* signal strength in [0, 1] with `strength_kind = UNCALIBRATED_SCORE`;
* evidence, where each item must name a VALID feature in the snapshot (value and availability are copied in);
* reasoning summary;
* expiry (the close of the n-th period after the signal, on the NYSE calendar);
* creation time.

There is no quantity, order type or approval state. Status (ACTIVE / EXPIRED / SUPERSEDED)
is derived at read time; expiry and supersession are recorded as append-only events.

## Evaluation engine

* **Idempotency:** the key is the hash of (version, asset, timeframe, mode, basis, trigger, trigger reference). A claim takes an advisory lock, and a partial unique index allows one non-failed evaluation per key.
* **Duplicates:** a repeated update returns DUPLICATE or IN_PROGRESS without doing any work.
* **Failures:** each is recorded with its stage (INPUTS, STRATEGY, VALIDATION, TIMEOUT, PERSISTENCE, INTERRUPTED). Messages are redacted. Retries stop at `STRATEGY_MAX_ATTEMPTS`.
* **Worker crashes:** an expired lease is recorded as INTERRUPTED and the update is retried. A late result from the dead worker is discarded.
* **Concurrency:** `evaluate_many` is bounded by a semaphore and a per-evaluation timeout. One failing or slow strategy never affects the others.
* **Transactions:**
  * the claim commits on its own;
  * the snapshot is stored (idempotently, by content hash);
  * proposals, supersession and the outcome are written in one transaction, guarded by a row lock and an ownership check;
  * a trigger allows only the single RUNNING → outcome change.
* **Expired signals:** `expire_due` records them idempotently.

## Database changes (migration 0009)

| Table | Contents |
|---|---|
| `feature_definitions` | the registry as registered (append-only) |
| `strategies` | identity and family (append-only) |
| `strategy_configurations` | content-addressed parameter sets (append-only) |
| `strategy_versions` | logic version, configuration and declared metadata (append-only) |
| `strategy_status_events` | enable/disable history (append-only) |
| `strategy_feature_snapshots` | point-in-time feature values (append-only, CHECKs) |
| `strategy_evaluations` | one row per attempt; outcome written once (guard trigger) |
| `strategy_evaluation_snapshots` | evaluation → snapshots (append-only) |
| `strategy_evaluation_errors` | failures by stage (append-only) |
| `strategy_proposals` | standardized proposals (append-only, CHECKs) |
| `strategy_proposal_events` | expiry and supersession (append-only) |

The upgrade → downgrade → upgrade round trip passes, and the models match the migration (no
drift). The dev database is at 0009.

## API endpoints (`/api/v1/strategies`)

**Reads:**

* `GET ""`: the catalog;
* `GET /features`: the feature registry;
* `GET /evaluations` and `/evaluations/{id}`, with errors and snapshots;
* `GET /feature-snapshots/{id}`;
* `GET /proposals` (filters: strategy, symbol, status, direction) and `/proposals/{id}`, with events and snapshots;
* `GET /{id}`: detail, parameters, JSON schema, versions and status history;
* `GET /{id}/versions` and `/{id}/configurations`.

**Writes** (operator token, audited):

* `POST /{id}/versions`: a new immutable version; identical parameters return the current version with `created: false`;
* `POST /{id}/status`: enable or disable; strategies without logic are refused with 409.

No endpoint approves, sizes or submits anything, and none returns performance figures.

## Dashboard additions

A new **Strategy Laboratory** entry in the main navigation (it replaces the "Strategies" placeholder).

**`/strategies`:**

* catalog cards: description, family, version and logic version, markets, configured and supported timeframes, schedule, required and optional feature counts, enabled/interface-only status, activity counts;
* a Phase 4A notice;
* recent proposals, filterable by status, with uncalibrated strength labelled as such;
* evaluation history with skip reasons;
* the feature registry, shown on request.

**`/strategies/:id`:**

* identity;
* current parameters, with schema descriptions;
* required feature keys;
* versions;
* status history;
* proposals and evaluations for that strategy.

The page shows no performance statistics and has no enable, parameter or buy/sell controls.

## Files

**Created:**

* Backend:
  * `alembic/versions/0009_strategy_laboratory_foundation.py`
  * `api/v1/strategies.py`
  * `database/models/strategy.py`
  * `feature_engine/{registry,bar_features,stored}.py`
  * `strategies/{base,catalog,cli,config,engine,enums,inputs,periods,proposals,queries,registry,runtime,triggers}.py`
  * `strategies/families/{__init__,momentum,trend_following,mean_reversion,breakout,volatility,event_driven}.py`
  * `workers/strategy_evaluation.py`
* Tests:
  * `integration/strategy_support.py`
  * `integration/test_strategy_{engine,catalog,stored_features,api,worker}.py`
  * `unit/test_feature_registry.py`
  * `unit/test_strategy_interface.py`
* Frontend:
  * `types/strategies.ts`
  * `services/strategies.ts`
  * `components/strategies.tsx`
  * `pages/{StrategiesPage,StrategyDetailPage}.tsx`
  * `pages/strategies.test.tsx`
  * `test/strategyFixtures.ts`
* Docs:
  * this report
  * `docs/prompts/phase-4a-strategy-laboratory-foundation.md`

**Modified:**

* Backend:
  * `feature_engine/indicators.py`: new functions only
  * `feature_engine/features.py`: the volume-ratio helper delegates to the shared function
  * `database/models/__init__.py`
  * `api/v1/__init__.py`
  * `main.py`: strategy context factory
* Tests:
  * `conftest.py`: `STRATEGY_` prefix and settings cache
  * `unit/test_api.py`: route inventory, writes
  * `unit/test_models.py`
  * `unit/test_health_checks.py`: head 0009
* Frontend:
  * `App.tsx`
  * `navigation.ts`
* Config and docs:
  * `.env.example`: 11 `STRATEGY_*` keys
  * `docker-compose.yml`: `strategy-worker`
  * `decisions.md`
  * `operations.md`
  * `overview.md`
  * `README.md`

No dependencies were added.

## Commands executed

* `pytest` (baseline and final);
* `ruff check`, `ruff format`, `mypy`;
* `alembic revision --autogenerate`, `alembic upgrade head` (dev DB);
* `python -m app.strategies.cli sync | features`;
* `pnpm typecheck`, `pnpm exec vitest run`, `pnpm build`.

## Test results

* **Backend:** 1133 passed, 1 skipped (no test Redis). The `.env.example` settings test, added afterwards, also passed.
* **Strategy Lab tests:**
  * 63 unit: registry/indicators 17, interface/proposals/expiry/settings 46;
  * 49 integration: engine 17, catalog 13, stored features 5, API 7, worker 7.
* **Lint and types:** ruff, ruff format and mypy (strict) are clean.
* **Frontend:** 75 passed (7 new); the production build succeeds.

**Coverage against the prompt:**

| Area | Covered by |
|---|---|
| Interfaces | six families plus registration validation |
| Configuration validation | 10 parameter cases plus API 422 |
| Feature dependencies | every family on every timeframe |
| Timestamps | snapshot availability, cutoffs, expiry on the calendar (holidays, early close, weekly) |
| Proposal schema | 11 invalid-draft cases plus field checks |
| Version immutability | triggers, re-sync, logic change, invalid stored configuration |
| Duplicates | engine and worker |
| Concurrency | 6 threads on one key; mixed batch |
| Expiry and supersession | — |
| Worker failures | raising strategy, timeout, lease reclaim, retries, give-up |
| API | — |
| Dashboard | rendering, empty states, no controls or performance text |
| Look-ahead | late bar excluded (RECORDED), unfinished session excluded, news retrieved later excluded, regimes point in time |

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]


## Remaining limitations

* **No strategy logic.** Real proposals and real enable/disable await Phase 4B.
* **Daily and weekly only.** Intraday timeframes need a completed-bar policy for minute bars.
* **No benchmark-relative strength feature.** It needs a cross-asset input.
* **Expected transaction costs** (spec 11.7) are not on the proposal. They belong to the cost models of the risk and execution phases.
* **News coverage is inferred** from job ranges and stream sessions (D-086).
* **Timeouts don't stop work.** A timed-out strategy thread keeps running until it returns; its result is discarded.
* **Update detection polls** (D-088). It is fine for daily data, but adds latency of up to `STRATEGY_WORKER_INTERVAL_SECONDS`.
* **Not verified in the running stack.** The dev images were **not rebuilt**: the C: drive is full, which made Docker Desktop's VM filesystem read-only. The dev database is at migration 0009 while the running backend container still expects 0008, so its readiness check reports a migration mismatch until the image is rebuilt. The dashboard page was verified only by the frontend tests.

## Readiness for Phase 4B

Phase 4B needs to:

1. Implement `evaluate` in each family module.
2. Set `implemented = True` and bump `logic_version`. Catalog sync then creates version 2 with the same parameters.
3. Enable the strategy through the API.

The engine, worker, snapshots, proposal validation, storage, API and page need no changes. The
test fixtures already show the full path from completed bar to stored, traceable proposal.

## How to run

See [`docs/operations.md`](../operations.md#strategy-laboratory-phase-4a). Once disk space
is available on C:, rebuild and restart:

```bash
docker compose build migrate && docker compose up -d migrate backend frontend
```
