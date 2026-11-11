"""Creating, executing, storing and reproducing backtest runs (Phase 8A).

* :meth:`BacktestService.create` resolves a request into a :class:`ResolvedConfig` (pinning
  strategy versions, the risk configuration, the execution model, the dataset knowledge
  time and the code version) and stores the run as QUEUED with its strategy versions.
* :meth:`BacktestService.execute` loads the run's dataset, replays it, and stores every
  result in one transaction together with the COMPLETED status and the result hash; replay
  progress is committed session by session (``backtest_event_cursors``) so it can be
  watched. A failure stores the non-fatal issues and the fatal error and marks the run
  FAILED; partial results are not stored.
* :meth:`BacktestService.reproduce` re-executes a completed run from its stored resolved
  configuration (nothing re-resolved) as a new run and records whether its result hash
  equals the original's.

Only ``backtest_*`` tables (and the content-addressed ``execution_models``) are written.
"""

from __future__ import annotations

import os
import subprocess  # read-only git query for the code version
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.enums import UniverseLayer
from app.backtesting import metrics as perf
from app.backtesting.broker import backtest_model
from app.backtesting.config import (
    BacktestRequest,
    ResolvedAsset,
    ResolvedConfig,
    ResolvedStrategy,
    sha256_of,
)
from app.backtesting.data import (
    CALENDAR,
    HistoricalDataset,
    layer_candidates,
    load_dataset,
    sessions_back,
    sessions_between,
)
from app.backtesting.engine import (
    BacktestEngine,
    BacktestError,
    BacktestResult,
    Checkpoint,
    fill_json,
    order_json,
)
from app.backtesting.enums import FeatureAdjustment, RunStatus, UniverseMode
from app.core.clocks import Clock
from app.core.exceptions import AtlasError, InvalidRequestError, NotFoundError
from app.core.logging import get_logger
from app.database.models import (
    Asset,
    AssetClass,
    BacktestBenchmarkResultRecord,
    BacktestEquitySnapshotRecord,
    BacktestEvaluationCountRecord,
    BacktestEventCursorRecord,
    BacktestFillRecord,
    BacktestJournalEntryRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestProposalRecord,
    BacktestRejectionRecord,
    BacktestRunErrorRecord,
    BacktestRunEventRecord,
    BacktestRunRecord,
    BacktestStrategyVersionRecord,
    BacktestTradeRecord,
    StrategyVersionRecord,
)
from app.database.models.ml import MLModelVersionRecord
from app.database.sessions import session_scope
from app.feature_engine.registry import FEATURE_REGISTRY_VERSION, FeatureSource
from app.ml_engine.enums import ValidationStatus
from app.risk.config import HardLimits, Preferences, RiskConfiguration
from app.risk.governance import active_configuration
from app.strategies import catalog
from app.strategies.base import StrategyDefinitionError
from app.strategies.periods import sessions_for
from app.strategies.registry import StrategyRegistry, default_registry

logger = get_logger(__name__)
RISK_ENGINE_VERSION = "6C.1"
REPO_ROOT = Path(__file__).resolve().parents[3]
STALE_AFTER = timedelta(minutes=30)
LIQUIDITY_LOOKBACK = 60


class BacktestRequestError(InvalidRequestError):
    """The request cannot be resolved into a runnable configuration."""


def code_version() -> str:
    """The commit the code runs from (``ATLAS_CODE_VERSION`` overrides; '-dirty' when the
    working tree has uncommitted changes; 'unknown' without git)."""
    configured = os.environ.get("ATLAS_CODE_VERSION")
    if configured:
        return configured[:80]
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return f"{sha}{'-dirty' if dirty else ''}"[:80]


def _asset(row: Asset) -> ResolvedAsset:
    return ResolvedAsset(
        asset_id=str(row.id),
        symbol=row.symbol,
        asset_class=row.asset_class.value,
        exchange_code=row.exchange_code,
        quote_currency=row.quote_currency,
    )


def _find_symbol(session: Session, symbol: str) -> Asset:
    rows = session.scalars(
        select(Asset).where(
            Asset.symbol == symbol, Asset.asset_class.in_([AssetClass.EQUITY, AssetClass.ETF])
        )
    ).all()
    if not rows:
        raise BacktestRequestError(f"{symbol} is not an Atlas US equity or ETF")
    if len(rows) > 1:
        raise BacktestRequestError(f"{symbol} is ambiguous ({len(rows)} assets)")
    return rows[0]


@dataclass
class _Stored:
    run_id: uuid.UUID
    config: ResolvedConfig


class BacktestService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        *,
        registry: StrategyRegistry | None = None,
        version: Callable[[], str] = code_version,
    ) -> None:
        self._sessions = session_factory
        self.clock = clock
        self.registry = registry or default_registry()
        self._version = version

    # ------------------------------------------------------------------ resolve
    def resolve(self, session: Session, request: BacktestRequest) -> ResolvedConfig:
        now = self.clock.now()
        known_at = request.data.data_known_at or now
        if known_at > now:
            raise BacktestRequestError("data_known_at cannot be in the future")
        sessions = sessions_between(request.start_date, request.end_date)
        if not sessions:
            raise BacktestRequestError("the date range contains no NYSE session")
        settle = timedelta(seconds=request.data.settle_seconds)
        if sessions[-1].regular_close + settle > known_at:
            raise BacktestRequestError(
                f"the last session {sessions[-1].day} had not completed by the dataset's "
                f"knowledge time {known_at.isoformat()}"
            )
        strategies = tuple(self._strategies(session, request))
        risk, risk_source = self._risk(session, request)
        model = backtest_model(request.execution.equity_overrides)
        if request.universe.mode is UniverseMode.EXPLICIT:
            assets = [_asset(_find_symbol(session, s)) for s in request.universe.symbols]
        else:
            layer = request.universe.layer
            assert layer is not None  # noqa: S101  # validated
            ids = layer_candidates(
                session,
                UniverseLayer(layer),
                until=sessions[-1].regular_close + settle,
                known_at=known_at,
            )
            rows = session.scalars(select(Asset).where(Asset.id.in_(ids))).all()
            assets = [
                _asset(r)
                for r in sorted(rows, key=lambda r: r.symbol)
                if r.asset_class in (AssetClass.EQUITY, AssetClass.ETF)
            ]
            if not assets:
                raise BacktestRequestError(
                    f"no asset was ever recorded in {layer.value} by {known_at.isoformat()}"
                )
        benchmark = (
            _asset(_find_symbol(session, request.benchmark_symbol.strip().upper()))
            if request.benchmark_symbol
            else None
        )
        references: dict[str, ResolvedAsset] = {}
        if benchmark is not None:
            references[benchmark.symbol] = benchmark
        for symbol in sorted(self._feature_benchmarks(strategies)):
            if symbol not in references:
                references[symbol] = _asset(_find_symbol(session, symbol))
        models = session.scalars(
            select(MLModelVersionRecord)
            .where(
                MLModelVersionRecord.validation_status == ValidationStatus.PASSED,
                MLModelVersionRecord.created_at <= known_at,
            )
            .order_by(MLModelVersionRecord.model_name, MLModelVersionRecord.version)
        ).all()
        return ResolvedConfig(
            code_version=self._version(),
            name=request.name,
            start_date=request.start_date,
            end_date=request.end_date,
            initial_capital=request.initial_capital,
            universe=request.universe,
            assets=tuple(assets),
            strategies=strategies,
            risk=risk.model_dump(mode="json"),
            risk_source=risk_source,
            risk_sha256=sha256_of(risk.model_dump(mode="json")),
            execution_model=model.definition(),
            execution_model_sha256=model.definition_sha256(),
            execution=request.execution,
            data=request.data.model_copy(update={"data_known_at": known_at}),
            benchmark=benchmark,
            reference_assets=tuple(references[s] for s in sorted(references)),
            breaker_reset_policy=request.breaker_reset_policy,
            seed=request.seed,
            feature_registry_version=FEATURE_REGISTRY_VERSION,
            risk_engine_version=RISK_ENGINE_VERSION,
            model_versions=tuple(
                {
                    "id": str(m.id),
                    "model_name": m.model_name,
                    "version": m.version,
                    "validation_status": m.validation_status.value,
                }
                for m in models
            ),
        )

    def _strategies(self, session: Session, request: BacktestRequest) -> list[ResolvedStrategy]:
        out: list[ResolvedStrategy] = []
        for selection in sorted(request.strategies, key=lambda s: s.strategy_id):
            if selection.strategy_id not in self.registry:
                raise BacktestRequestError(f"unknown strategy {selection.strategy_id}")
            strategy = self.registry.get(selection.strategy_id)
            if selection.version is None:
                version = catalog.latest_version(session, selection.strategy_id)
            else:
                version = session.scalar(
                    select(StrategyVersionRecord).where(
                        StrategyVersionRecord.strategy_id == selection.strategy_id,
                        StrategyVersionRecord.version == selection.version,
                    )
                )
            if version is None:
                raise BacktestRequestError(
                    f"{selection.strategy_id} has no stored version"
                    + (f" {selection.version}" if selection.version else "")
                    + ": sync the strategy catalog first"
                )
            if not version.implemented or version.logic_version != strategy.logic_version:
                raise BacktestRequestError(
                    f"{selection.strategy_id} version {version.version} (logic "
                    f"{version.logic_version}) cannot be replayed by this code (logic "
                    f"{strategy.logic_version})"
                )
            stored = dict(catalog.configuration(session, version).parameters)
            merged = {**stored, **(selection.parameters or {})}
            if request.timeframes is not None:
                merged["timeframes"] = [
                    t for t in request.timeframes if t in strategy.supported_timeframes
                ]
            if request.directions is not None:
                merged["directions"] = [d.value for d in request.directions]
            try:
                base = catalog.parameters_json(strategy.parse_parameters(stored))
                params = catalog.parameters_json(strategy.parse_parameters(merged))
            except StrategyDefinitionError as exc:
                raise BacktestRequestError(f"{selection.strategy_id}: {exc}") from None
            out.append(
                ResolvedStrategy(
                    strategy_id=strategy.strategy_id,
                    family=strategy.family.value,
                    version_id=str(version.id),
                    version=version.version,
                    logic_version=version.logic_version,
                    configuration_id=str(version.configuration_id),
                    stored_parameters=base,
                    parameters=params,
                    parameters_sha256=sha256_of(params),
                    parameters_source="STORED_VERSION" if params == base else "BACKTEST_OVERRIDE",
                    timeframes=tuple(params["timeframes"]),
                )
            )
        return out

    def _risk(self, session: Session, request: BacktestRequest) -> tuple[RiskConfiguration, str]:
        base: RiskConfiguration | None = None
        try:
            from app.portfolio.queries import resolve_account

            account = resolve_account(session, None)
            base = active_configuration(session, account.id)
        except AtlasError:
            base = None
        source = (
            f"STORED_VERSION:{base.version_id} (v{base.version})"
            if base is not None
            else "ENGINE_DEFAULTS"
        )
        base = base or RiskConfiguration()
        hard, prefs = base.hard_limits, base.preferences
        try:
            if request.risk.hard_limits:
                hard = HardLimits.model_validate(
                    {**hard.model_dump(mode="json"), **request.risk.hard_limits}
                )
            if request.risk.preferences:
                prefs = Preferences.model_validate(
                    {**prefs.model_dump(mode="json"), **request.risk.preferences}
                )
        except ValueError as exc:
            raise BacktestRequestError(f"risk configuration: {exc}") from None
        if request.risk.hard_limits or request.risk.preferences:
            source += "+BACKTEST_OVERRIDES"
        return (
            RiskConfiguration(
                version_id=base.version_id,
                version=base.version,
                hard_limits=hard,
                preferences=prefs,
            ),
            source,
        )

    def _feature_benchmarks(self, strategies: tuple[ResolvedStrategy, ...]) -> set[str]:
        out: set[str] = set()
        for rs in strategies:
            strategy = self.registry.get(rs.strategy_id)
            params = strategy.parse_parameters(rs.parameters)
            for tf in rs.timeframes:
                for f in self.registry.features.resolve_all(strategy.required_features(params, tf)):
                    if f.definition.source is FeatureSource.BENCHMARK:
                        out.add(str(f.param_map["benchmark"]).upper())
        return out

    # ------------------------------------------------------------------ create
    def create(
        self,
        request: BacktestRequest,
        *,
        actor: str,
        resolved: ResolvedConfig | None = None,
        reproduction_of: uuid.UUID | None = None,
    ) -> uuid.UUID:
        with session_scope(self._sessions) as session:
            config = resolved or self.resolve(session, request)
            model = config.model()
            model_id = model.register(session)
            run = BacktestRunRecord(
                id=uuid.uuid4(),
                name=config.name,
                status=RunStatus.QUEUED,
                request=request.model_dump(mode="json"),
                config=config.as_json(),
                config_sha256=config.sha256(),
                engine_version=config.engine_version,
                code_version=config.code_version,
                seed=config.seed,
                start_date=config.start_date,
                end_date=config.end_date,
                initial_capital=config.initial_capital,
                currency=config.currency,
                universe_mode=config.universe.mode.value,
                benchmark_symbol=None if config.benchmark is None else config.benchmark.symbol,
                data_provider=config.data.provider,
                data_feed=config.data.feed,
                data_known_at=config.known_at,
                execution_model_id=model_id,
                execution_model_sha256=config.execution_model_sha256,
                risk_sha256=config.risk_sha256,
                risk_source=config.risk_source[:80],
                reproduction_of=reproduction_of,
                requested_by=actor[:128],
                sessions_total=len(sessions_between(config.start_date, config.end_date)),
            )
            session.add(run)
            session.flush()
            for rs in config.strategies:
                session.add(
                    BacktestStrategyVersionRecord(
                        run_id=run.id,
                        strategy_id=rs.strategy_id,
                        strategy_version_id=uuid.UUID(rs.version_id),
                        version=rs.version,
                        logic_version=rs.logic_version,
                        parameters=rs.parameters,
                        parameters_sha256=rs.parameters_sha256,
                        parameters_source=rs.parameters_source,
                        timeframes=list(rs.timeframes),
                    )
                )
            logger.info("backtest.created", run_id=str(run.id), name=config.name)
            return run.id

    # ------------------------------------------------------------------ execute
    def _claim(self, run_id: uuid.UUID) -> _Stored:
        with session_scope(self._sessions) as session:
            run = session.get(BacktestRunRecord, run_id, with_for_update=True)
            if run is None:
                raise NotFoundError(f"backtest run {run_id} does not exist")
            if run.status is not RunStatus.QUEUED:
                raise InvalidRequestError(f"backtest run {run_id} is {run.status.value}")
            run.status = RunStatus.RUNNING
            run.started_at = self.clock.now()
            return _Stored(run.id, ResolvedConfig.model_validate(run.config))

    def load(self, session: Session, config: ResolvedConfig) -> HistoricalDataset:
        windows = [1]
        for rs in config.strategies:
            strategy = self.registry.get(rs.strategy_id)
            params = strategy.parse_parameters(rs.parameters)
            for tf in rs.timeframes:
                resolved = self.registry.features.resolve_all(
                    strategy.required_features(params, tf)
                )
                window = max((f.window for f in resolved if f.window), default=1)
                windows.append(sessions_for(tf, window))
        lookback = max(windows) + LIQUIDITY_LOOKBACK + 10
        first = sessions_back(config.start_date, lookback)
        ids = [uuid.UUID(a.asset_id) for a in (*config.assets, *config.reference_assets)]
        return load_dataset(
            session,
            ids,
            provider=config.data.provider,
            feed=config.data.feed,
            first=first,
            last=config.end_date,
            known_at=config.known_at,
            settle=timedelta(seconds=config.data.settle_seconds),
            split_adjusted=config.data.feature_adjustment is FeatureAdjustment.POINT_IN_TIME_SPLIT,
        )

    def _progress(self, run_id: uuid.UUID) -> Callable[[Checkpoint], None]:
        def write(cp: Checkpoint) -> None:
            with session_scope(self._sessions) as session:
                session.add(
                    BacktestEventCursorRecord(
                        run_id=run_id,
                        seq=cp.seq,
                        session=cp.session,
                        simulated_at=cp.simulated_at,
                        sessions_done=cp.sessions_done,
                        sessions_total=cp.sessions_total,
                        evaluations=cp.evaluations,
                        proposals=cp.proposals,
                        fills=cp.fills,
                        journal_entries=cp.journal_entries,
                        reconciliation=cp.reconciliation,
                        equity=cp.equity,
                    )
                )
                session.execute(
                    update(BacktestRunRecord)
                    .where(BacktestRunRecord.id == run_id)
                    .values(
                        sessions_done=cp.sessions_done,
                        sessions_total=cp.sessions_total,
                        simulated_through=cp.simulated_at,
                    )
                )

        return write

    def replay(
        self,
        session: Session,
        config: ResolvedConfig,
        *,
        progress: Callable[[Checkpoint], None] | None = None,
    ) -> tuple[BacktestResult, HistoricalDataset]:
        """Load the dataset and replay it (nothing written); metrics included."""
        dataset = self.load(session, config)
        engine = BacktestEngine(session, config, dataset, registry=self.registry, progress=progress)
        result = engine.run()
        benchmark = (
            None
            if config.benchmark is None
            else (uuid.UUID(config.benchmark.asset_id), config.benchmark.symbol)
        )
        perf.compute_all(
            result,
            dataset,
            config.initial_capital,
            benchmark=benchmark,
            universe=[uuid.UUID(a.asset_id) for a in config.assets],
        )
        return result, dataset

    def execute(self, run_id: uuid.UUID) -> RunStatus:
        stored = self._claim(run_id)
        config = stored.config
        issues: list[dict[str, Any]] = []
        try:
            with session_scope(self._sessions) as session:
                result, dataset = self.replay(session, config, progress=self._progress(run_id))
            self._store(run_id, result, dataset)
            logger.info("backtest.completed", run_id=str(run_id), result=result.sha256()[:12])
            return RunStatus.COMPLETED
        except Exception as exc:
            stage = exc.stage if isinstance(exc, BacktestError) else "internal"
            at = exc.at if isinstance(exc, BacktestError) else None
            logger.exception("backtest.failed", run_id=str(run_id), stage=stage)
            self._fail(run_id, stage, f"{type(exc).__name__}: {exc}", at, issues)
            return RunStatus.FAILED

    def _fail(
        self,
        run_id: uuid.UUID,
        stage: str,
        message: str,
        at: datetime | None,
        issues: list[dict[str, Any]],
    ) -> None:
        with session_scope(self._sessions) as session:
            run = session.get(BacktestRunRecord, run_id, with_for_update=True)
            if run is None or run.status is not RunStatus.RUNNING:
                return
            session.add(
                BacktestRunErrorRecord(
                    run_id=run_id,
                    seq=1,
                    at=at,
                    stage=stage[:32],
                    fatal=True,
                    message=message[:4000],
                    details={},
                )
            )
            session.flush()
            run.status = RunStatus.FAILED
            run.error_stage = stage[:32]
            run.error_message = message[:4000]
            run.finished_at = self.clock.now()

    def fail_stale(self, older_than: timedelta = STALE_AFTER) -> int:
        """RUNNING runs whose progress stopped (their process ended) are failed."""
        cutoff = self.clock.now() - older_than
        failed = 0
        with session_scope(self._sessions) as session:
            runs = session.scalars(
                select(BacktestRunRecord).where(BacktestRunRecord.status == RunStatus.RUNNING)
            ).all()
            stale = []
            for run in runs:
                last = session.scalar(
                    select(BacktestEventCursorRecord.recorded_at)
                    .where(BacktestEventCursorRecord.run_id == run.id)
                    .order_by(BacktestEventCursorRecord.seq.desc())
                    .limit(1)
                )
                if (last or run.started_at or run.created_at) < cutoff:
                    stale.append(run.id)
        for run_id in stale:
            self._fail(
                run_id,
                "interrupted",
                "the replay stopped making progress (its process ended); nothing was stored",
                None,
                [],
            )
            failed += 1
        return failed

    # ------------------------------------------------------------------ store
    def _store(self, run_id: uuid.UUID, result: BacktestResult, dataset: HistoricalDataset) -> None:
        r = run_id
        with session_scope(self._sessions) as session:
            run = session.get(BacktestRunRecord, run_id, with_for_update=True)
            assert run is not None  # noqa: S101  # claimed above
            rows: list[tuple[Any, list[dict[str, Any]]]] = [
                (BacktestProposalRecord, [_proposal_row(r, p.as_json()) for p in result.proposals]),
                (
                    BacktestRejectionRecord,
                    [{"run_id": r, **x.as_json()} for x in result.rejections],
                ),
                (BacktestOrderRecord, [_order_row(r, order_json(o)) for o in result.orders]),
                (BacktestFillRecord, [_fill_row(r, fill_json(f)) for f in result.fills]),
                (BacktestTradeRecord, [_trade_row(r, t.as_json()) for t in result.trades]),
                (
                    BacktestEquitySnapshotRecord,
                    [_snapshot_row(r, s.as_json()) for s in result.snapshots],
                ),
                (BacktestMetricRecord, [{"run_id": r, **m} for m in result.metrics]),
                (BacktestBenchmarkResultRecord, [_benchmark_row(r, b) for b in result.benchmarks]),
                (BacktestRunEventRecord, [{"run_id": r, **e.as_json()} for e in result.events]),
                (
                    BacktestRunErrorRecord,
                    [
                        {"run_id": r, **{k: v for k, v in i.as_json().items()}, "seq": i.seq}
                        for i in result.issues
                    ],
                ),
                (BacktestJournalEntryRecord, [_journal_row(r, j) for j in result.journal]),
                (
                    BacktestEvaluationCountRecord,
                    [
                        {
                            "run_id": r,
                            "strategy_id": k.split("|")[0],
                            "timeframe": k.split("|")[1],
                            "state": k.split("|")[2],
                            "count": v,
                        }
                        for k, v in result.evaluations.items()
                    ],
                ),
            ]
            for model, values in rows:
                if values:
                    session.execute(insert(model), [_typed(model, v) for v in values])
            result_sha = result.sha256()
            matches = None
            if run.reproduction_of is not None:
                original = session.get(BacktestRunRecord, run.reproduction_of)
                matches = original is not None and original.result_sha256 == result_sha
            final = result.snapshots[-1] if result.snapshots else None
            run.dataset_sha256 = result.dataset_sha256
            run.dataset_bars = dataset.bar_count
            run.result_sha256 = result_sha
            run.reproduction_matches = matches
            run.summary = {
                "final_equity": None if final is None else str(final.equity),
                "total_return": None
                if final is None
                else str((final.equity / run.initial_capital - 1).quantize(Decimal("0.00000001"))),
                "trades": len(result.trades),
                "proposals": len(result.proposals),
                "fills": len(result.fills),
                "issues": len(result.issues),
                "reconciliation": result.reconciliation,
                "replay_config_sha256": result.config_sha256,
            }
            run.status = RunStatus.COMPLETED
            run.finished_at = self.clock.now()

    # ------------------------------------------------------------------ reproduce
    def prepare_reproduction(self, run_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        """A new QUEUED run with the stored configuration of a completed run (nothing is
        re-resolved: same strategy versions, risk, model, dataset knowledge time, seed)."""
        with session_scope(self._sessions) as session:
            run = session.get(BacktestRunRecord, run_id)
            if run is None:
                raise NotFoundError(f"backtest run {run_id} does not exist")
            if run.status is not RunStatus.COMPLETED:
                raise InvalidRequestError("only a completed run can be reproduced")
            request = BacktestRequest.model_validate(run.request)
            stored = ResolvedConfig.model_validate(run.config)
        config = stored.model_copy(
            update={
                "code_version": self._version(),
                "name": f"Reproduction of {stored.name}"[:120],
            }
        )
        return self.create(request, actor=actor, resolved=config, reproduction_of=run_id)

    def reproduce(self, run_id: uuid.UUID, *, actor: str) -> uuid.UUID:
        new_id = self.prepare_reproduction(run_id, actor=actor)
        self.execute(new_id)
        return new_id


# ---------------------------------------------------------------------- row mapping
def _proposal_row(run_id: uuid.UUID, p: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "seq",
        "decided_at",
        "session",
        "strategy_id",
        "strategy_version",
        "timeframe",
        "asset_id",
        "symbol",
        "direction",
        "reference_entry",
        "invalidation_level",
        "target_reference",
        "signal_strength",
        "expires_at",
        "risk_result",
        "permitted_quantity",
        "binding_constraint",
        "outcome",
        "order_seq",
        "trade_seq",
    )
    return {
        "run_id": run_id,
        **{k: p[k] for k in keep},
        "details": {k: v for k, v in p.items() if k not in keep},
    }


def _order_row(run_id: uuid.UUID, o: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "seq",
        "role",
        "trade_seq",
        "proposal_seq",
        "asset_id",
        "symbol",
        "side",
        "order_type",
        "quantity",
        "filled_quantity",
        "average_price",
        "status",
        "arrival_at",
        "expires_at",
        "commission",
        "fees",
    )
    return {
        "run_id": run_id,
        **{k: o[k] for k in keep},
        "details": {k: v for k, v in o.items() if k not in keep},
    }


def _fill_row(run_id: uuid.UUID, f: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "seq",
        "order_seq",
        "trade_seq",
        "asset_id",
        "symbol",
        "side",
        "role",
        "session",
        "filled_at",
        "known_at",
        "quantity",
        "price",
        "notional",
        "commission",
        "fees",
        "basis",
        "journal_sequence",
    )
    return {
        "run_id": run_id,
        **{k: f[k] for k in keep},
        "details": {k: v for k, v in f.items() if k not in keep},
    }


def _trade_row(run_id: uuid.UUID, t: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "seq",
        "proposal_seq",
        "strategy_id",
        "timeframe",
        "asset_id",
        "symbol",
        "direction",
        "status",
        "opened_at",
        "closed_at",
        "entry_quantity",
        "entry_price",
        "exit_quantity",
        "exit_price",
        "exit_reason",
        "realized_gross",
        "unrealized",
        "net_pnl",
        "holding_sessions",
    )
    costs = Decimal(t["entry_costs"]) + Decimal(t["exit_costs"]) + Decimal(t["borrow_costs"])
    return {
        "run_id": run_id,
        **{k: t[k] for k in keep},
        "costs": str(costs),
        "details": {k: v for k, v in t.items() if k not in keep},
    }


def _snapshot_row(run_id: uuid.UUID, s: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "session",
        "at",
        "equity",
        "gross_equity",
        "cash",
        "long_exposure",
        "short_exposure",
        "realized_pnl",
        "unrealized_pnl",
        "total_costs",
        "drawdown",
        "drawdown_pct",
        "open_positions",
    )
    return {
        "run_id": run_id,
        **{k: s[k] for k in keep},
        "details": {k: v for k, v in s.items() if k not in keep},
    }


def _benchmark_row(run_id: uuid.UUID, b: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "kind": b["kind"],
        "label": b["label"],
        "available": b["available"],
        "total_return": b.get("total_return"),
        "annualized_return": b.get("annualized_return"),
        "max_drawdown_pct": b.get("max_drawdown_pct"),
        "excess_return": b.get("excess_return"),
        "details": {
            k: v
            for k, v in b.items()
            if k
            not in (
                "kind",
                "label",
                "available",
                "total_return",
                "annualized_return",
                "max_drawdown_pct",
                "excess_return",
            )
        },
    }


def _typed(model: Any, row: dict[str, Any]) -> dict[str, Any]:
    """JSON strings back to column types (decimals, timestamps, dates, ids)."""
    out: dict[str, Any] = {}
    columns = model.__table__.columns
    for key, value in row.items():
        column = columns.get(key)
        if column is None or not isinstance(value, str):
            out[key] = value
            continue
        try:
            kind = column.type.python_type
        except NotImplementedError:
            out[key] = value
            continue
        if kind is Decimal:
            out[key] = Decimal(value)
        elif kind is datetime:
            out[key] = datetime.fromisoformat(value)
        elif kind is date:
            out[key] = date.fromisoformat(value)
        elif kind is uuid.UUID:
            out[key] = uuid.UUID(value)
        else:
            out[key] = value
    return out


def _journal_row(run_id: uuid.UUID, j: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": run_id, **j}


__all__ = [
    "BacktestRequestError",
    "BacktestService",
    "code_version",
]

_ = CALENDAR  # the calendar every backtest uses (XNYS)
