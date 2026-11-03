"""Point-in-time dataset building (Phase 5A).

For every asset, timeframe and decision period of a :class:`DatasetSpec`, the builder

1. computes the requested registry features with ``app.strategies.inputs.compute_features``,
   the exact code path strategies use: only periods completed and settled by the decision
   time, bars as Atlas knew them at the dataset's ``data_known_at`` (RECONSTRUCTED basis,
   D-068), missing data left missing;
2. decides which observations the decision time produces (every direction for
   PERIOD_CLOSE; what a strategy proposes for STRATEGY_REPLAY; recorded proposals for
   STRATEGY_PROPOSALS);
3. labels each observation from the bars *after* the decision (``labels.make_label``);
   labels whose horizon had not settled by ``data_known_at`` are IMMATURE.

Features never read a bar of a period that had not completed by the decision time
(``last_input_period_at <= decision_at`` is also a database constraint), and a label is
never an input. Observations whose required features are not VALID are rejected here
(counted by reason); optional features may be missing.

Nothing is written by :func:`build_dataset`; ``store.save_dataset`` persists the result.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.asset_discovery.enums import UniverseLayer
from app.asset_discovery.membership import members_at
from app.asset_discovery.sessions import (
    ExchangeCalendar,
    SessionDay,
    completed_sessions,
    local_date,
    next_trading_day,
    previous_trading_day,
)
from app.database.models import (
    Asset,
    StrategyProposalRecord,
    StrategyVersionRecord,
)
from app.feature_engine.registry import (
    FEATURE_REGISTRY_VERSION,
    REGISTRY,
    FeatureRegistry,
    FeatureRequest,
    FeatureValue,
    ResolvedFeature,
    ValueKind,
    sha256_json,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.ml_engine.datasets.bars import (
    DecisionPeriod,
    PreloadedBars,
    decision_periods,
    forward_sessions,
    sessions_before,
)
from app.ml_engine.datasets.labels import Label, LabelInput, PathBar, make_label
from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.enums import (
    BarrierSource,
    DataOrigin,
    LabelStatus,
    ObservationSource,
    TargetKind,
    UniversePolicy,
)
from app.ml_engine.versioning import code_version
from app.strategies import catalog
from app.strategies.base import AssetView, EvaluationContext, Strategy, StrategyParameters
from app.strategies.eligibility import load_eligibility
from app.strategies.enums import Direction, EvaluationMode, TriggerKind
from app.strategies.inputs import BarSeries, FeatureInputs, compute_features
from app.strategies.periods import sessions_for, week_key
from app.strategies.registry import StrategyRegistry, default_registry

SEGMENT_VERSION = "segment-v1"
DEFAULT_MAX_OBSERVATIONS = 250_000  # ML_MAX_OBSERVATIONS
SEGMENT_DOCUMENTATION = (
    "market_trend: UP when the benchmark's simple return over market_trend_periods "
    "completed periods (benchmark_return) is >= 0, DOWN otherwise, UNKNOWN when unavailable. "
    "volatility: the asset's realized-volatility percentile (volatility_length returns, "
    "ranked among volatility_history values): LOW below 1/3, HIGH above 2/3, NORMAL "
    "between, UNKNOWN when unavailable. Descriptive evaluation segments computed point in "
    "time from completed periods; they are not Phase 3B regime classifications."
)


class DatasetBuildError(ValueError):
    """The specification cannot be built (unknown assets, a future snapshot, ...)."""


@dataclass(frozen=True)
class SchemaItem:
    """One model input column: a registry feature on every dataset timeframe."""

    column: str  # canonical key without the timeframe, e.g. ``rsi.v1(length=14)``
    name: str
    version: str
    source: str
    params: dict[str, Any]
    value_kind: ValueKind
    optional: bool
    definition_sha256: str
    keys: dict[str, str]  # timeframe -> full feature key

    def to_json(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "params": self.params,
            "value_kind": self.value_kind.value,
            "optional": self.optional,
            "definition_sha256": self.definition_sha256,
            "keys": self.keys,
        }


@dataclass(frozen=True)
class Observation:
    asset_id: uuid.UUID
    symbol: str
    timeframe: str
    direction: Direction
    decision_at: datetime
    data_known_at: datetime
    source: ObservationSource
    strategy_id: str | None
    strategy_version: str | None
    proposal_id: uuid.UUID | None
    horizon_periods: int
    features: dict[str, Any]
    features_available_at: datetime | None
    last_input_period_at: datetime | None
    segments: dict[str, Any]
    label: Label

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.decision_at,
            self.symbol,
            str(self.asset_id),
            self.timeframe,
            self.direction.value,
            self.strategy_id or "",
            str(self.proposal_id or ""),
        )

    def to_json(self, row_index: int) -> dict[str, Any]:
        """The stored content of the row (the input of the dataset's content hash)."""
        return {
            "row_index": row_index,
            "asset_id": str(self.asset_id),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction.value,
            "decision_at": self.decision_at.isoformat(),
            "source": self.source.value,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "proposal_id": None if self.proposal_id is None else str(self.proposal_id),
            "horizon_periods": self.horizon_periods,
            "features": self.features,
            "features_available_at": _iso(self.features_available_at),
            "last_input_period_at": _iso(self.last_input_period_at),
            "segments": self.segments,
            "label_status": self.label.status.value,
            "label_value": None if self.label.value is None else repr(self.label.value),
            "label_class": self.label.cls,
            "label_end_at": _iso(self.label.end_at),
            "outcome": self.label.outcome,
        }


@dataclass
class BuiltDataset:
    spec: DatasetSpec  # with data_known_at fixed
    origin: DataOrigin
    feature_schema: list[SchemaItem]
    observations: list[Observation]
    statistics: dict[str, Any]
    warnings: list[str]
    asset_ids: list[uuid.UUID]
    code: dict[str, Any] = field(default_factory=code_version)

    @property
    def target_definition(self) -> dict[str, Any]:
        return self.spec.target.definition()

    def content_sha256(self) -> str:
        return sha256_json(
            {
                "spec": self.spec.stored(),
                "schema": [s.to_json() for s in self.feature_schema],
                "rows": [o.to_json(i) for i, o in enumerate(self.observations)],
            }
        )

    def schema_sha256(self) -> str:
        return sha256_json([s.to_json() for s in self.feature_schema])


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _column(feature: ResolvedFeature) -> str:
    return feature.key.rsplit("@", 1)[0]


def _number(value: FeatureValue | None) -> float | None:
    if value is None or not value.valid or not isinstance(value.value, float):
        return None
    return value.value


@dataclass(frozen=True)
class _StrategyPlan:
    strategy: Strategy
    params: StrategyParameters
    version: str
    parameters_source: str
    resolved: dict[str, list[ResolvedFeature]]  # timeframe -> required features


class DatasetBuilder:
    def __init__(
        self,
        session: Session,
        spec: DatasetSpec,
        *,
        provider: str,
        feed: str,
        calendar: ExchangeCalendar,
        now: datetime,
        features: FeatureRegistry = REGISTRY,
        strategies: StrategyRegistry | None = None,
        max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    ) -> None:
        known_at = spec.data_known_at or now
        if known_at > now:
            raise DatasetBuildError("data_known_at cannot be in the future")
        if spec.decision_end > known_at:
            raise DatasetBuildError("decision_end cannot be after data_known_at")
        self.session = session
        self.spec = spec.frozen(known_at)
        self.known_at = known_at
        self.registry = features
        self.series = BarSeries(
            provider=provider,
            feed=feed,
            adjustment=spec.adjustment,
            calendar=calendar,
            settle=timedelta(seconds=spec.settle_seconds),
        )
        self.calendar = calendar
        self.max_observations = max_observations
        self.stats: Counter[str] = Counter()
        self.warnings: list[str] = []
        self._strategy_registry = strategies
        self._eligibility: dict[uuid.UUID, Any] = {}
        self._membership: dict[datetime, set[uuid.UUID]] = {}

    # --- Features ---------------------------------------------------------------------

    def _requests(self, timeframe: str) -> list[FeatureRequest]:
        return [FeatureRequest(f.name, timeframe, f.params, f.optional) for f in self.spec.features]

    def _aux(self, timeframe: str) -> dict[str, ResolvedFeature]:
        spec = self.spec
        return {
            "market": self.registry.resolve(
                FeatureRequest(
                    "benchmark_return",
                    timeframe,
                    {"periods": spec.market_trend_periods, "benchmark": spec.market_benchmark},
                    optional=True,
                )
            ),
            "volatility": self.registry.resolve(
                FeatureRequest(
                    "realized_volatility_percentile",
                    timeframe,
                    {"length": spec.volatility_length, "history": spec.volatility_history},
                    optional=True,
                )
            ),
            "atr_pct": self.registry.resolve(
                FeatureRequest(
                    "atr_pct", timeframe, {"length": spec.target.atr_length}, optional=True
                )
            ),
        }

    def schema(self) -> list[SchemaItem]:
        by_column: dict[str, SchemaItem] = {}
        for timeframe in self.spec.timeframes:
            for request in self._requests(timeframe):
                try:
                    resolved = self.registry.resolve(request)
                except ValueError as exc:
                    raise DatasetBuildError(str(exc)) from exc
                column = _column(resolved)
                item = by_column.get(column)
                keys = {**(item.keys if item else {}), timeframe: resolved.key}
                d = resolved.definition
                by_column[column] = SchemaItem(
                    column=column,
                    name=d.name,
                    version=d.version,
                    source=d.source.value,
                    params=resolved.param_map,
                    value_kind=d.value_kind,
                    optional=request.optional,
                    definition_sha256=d.definition_sha256,
                    keys=keys,
                )
        return [by_column[c] for c in sorted(by_column)]

    # --- Strategies -------------------------------------------------------------------

    def _strategy_plans(self) -> list[_StrategyPlan]:
        registry = self._strategy_registry or default_registry()
        wanted = self.spec.strategies or tuple(s.strategy_id for s in registry if s.implemented)
        plans = []
        for strategy_id in wanted:
            if strategy_id not in registry:
                raise DatasetBuildError(f"unknown strategy {strategy_id}")
            strategy = registry.get(strategy_id)
            if not strategy.implemented:
                raise DatasetBuildError(f"{strategy_id} has no evaluation logic")
            # The newest version registered by data_known_at: a rebuild is not changed by
            # versions registered afterwards.
            version = self.session.scalar(
                select(StrategyVersionRecord)
                .where(
                    StrategyVersionRecord.strategy_id == strategy_id,
                    StrategyVersionRecord.created_at <= self.known_at,
                )
                .order_by(StrategyVersionRecord.version.desc())
                .limit(1)
            )
            if version is not None and version.logic_version == strategy.logic_version:
                raw = dict(catalog.configuration(self.session, version).parameters)
                params = strategy.parse_parameters(raw)
                source = f"registered version {version.version}"
            else:
                params = strategy.default_parameters()
                source = "code defaults (no registered version with this logic)"
            stored = params.model_dump(mode="json")
            label = f"{strategy_id}@{strategy.logic_version}#{sha256_json(stored)[:12]}"
            resolved = {
                tf: registry.features.resolve_all(strategy.required_features(params, tf))
                for tf in self.spec.timeframes
                if tf in params.timeframes and tf in strategy.supported_timeframes
            }
            plans.append(_StrategyPlan(strategy, params, label, source, resolved))
        return plans

    # --- Universe and data ------------------------------------------------------------

    def _assets(self) -> list[Asset]:
        found: list[Asset] = []
        missing: list[str] = []
        for symbol in self.spec.symbols:
            rows = self.session.scalars(select(Asset).where(Asset.symbol == symbol)).all()
            if len(rows) > 1:
                raise DatasetBuildError(f"symbol {symbol} is ambiguous")
            if not rows:
                missing.append(symbol)
                continue
            found.append(rows[0])
        if missing:
            self.warnings.append(f"symbols not found in Atlas and skipped: {', '.join(missing)}")
        if not found:
            raise DatasetBuildError("none of the symbols is an Atlas asset")
        return found

    def _member(self, asset_id: uuid.UUID, at: datetime) -> bool:
        if self.spec.universe is UniversePolicy.FIXED_ASSETS:
            return True
        members = self._membership.get(at)
        if members is None:
            members = set(
                members_at(self.session, UniverseLayer.QUALIFIED_UNIVERSE, at, known_at=at)
            )
            self._membership[at] = members
        return asset_id in members

    def _inputs(
        self,
        asset_id: uuid.UUID,
        timeframe: str,
        decision_at: datetime,
        features: Sequence[ResolvedFeature],
        bars: PreloadedBars,
    ) -> FeatureInputs:
        return compute_features(
            self.session,
            features,
            asset_id=asset_id,
            timeframe=timeframe,
            data_cutoff=decision_at,
            known_at=self.known_at,
            basis=KnowledgeBasis.RECONSTRUCTED,
            series=self.series,
            bar_source=bars,
        )

    def _segments(
        self, values: dict[str, FeatureValue], aux: dict[str, ResolvedFeature]
    ) -> dict[str, Any]:
        market = _number(values.get(aux["market"].key))
        vol = _number(values.get(aux["volatility"].key))
        if vol is None:
            volatility = "UNKNOWN"
        else:
            volatility = "LOW" if vol < 1 / 3 else "HIGH" if vol > 2 / 3 else "NORMAL"
        return {
            "version": SEGMENT_VERSION,
            "market_trend": "UNKNOWN" if market is None else ("UP" if market >= 0 else "DOWN"),
            "volatility": volatility,
            "benchmark_return": market,
            "volatility_percentile": vol,
        }

    def _label(
        self,
        bars: PreloadedBars,
        asset_id: uuid.UUID,
        period: DecisionPeriod,
        direction: Direction,
        horizon: int,
        decision_close: float,
        atr_pct: float | None,
        stop: float | None = None,
        target: float | None = None,
    ) -> Label:
        forward = forward_sessions(self.calendar, period, horizon)
        settle = self.series.settle
        end_at = forward[-1].regular_close + settle
        path: list[PathBar | None] = []
        for s in forward:
            if s.regular_close + settle > self.known_at:
                path.append(None)
                continue
            bar = bars.bar(asset_id, s.day)
            if bar is None or bar.quality_status != "VALID":
                path.append(None)
            else:
                path.append(PathBar(bar.event_time, bar.open, bar.high, bar.low, bar.close))
        return make_label(
            self.spec.target,
            LabelInput(
                direction=direction,
                horizon_periods=horizon,
                decision_close=decision_close,
                path=path,
                label_end_at=end_at,
                data_known_at=self.known_at,
                atr_pct=atr_pct,
                stop_level=stop,
                target_level=target,
            ),
        )

    # --- Build ------------------------------------------------------------------------

    def build(self) -> BuiltDataset:
        spec = self.spec
        schema = self.schema()
        assets = self._assets()
        plans = self._strategy_plans() if spec.source is ObservationSource.STRATEGY_REPLAY else []
        windows = [
            sessions_for(tf, f.window)
            for tf in spec.timeframes
            for f in (
                *self.registry.resolve_all(self._requests(tf)),
                *self._aux(tf).values(),
                *(r for p in plans for r in p.resolved.get(tf, [])),
            )
        ]
        start_day = local_date(self.calendar, spec.decision_start)
        first = sessions_before(self.calendar, start_day, max(windows, default=1) + 10)
        last = local_date(self.calendar, self.known_at)
        bars = PreloadedBars(self.session, self.series, self.known_at, first, last)

        observations: list[Observation] = []
        if spec.source is ObservationSource.STRATEGY_PROPOSALS:
            observations = self._from_proposals(assets, bars)
        else:
            for asset in assets:
                for timeframe in spec.timeframes:
                    observations.extend(self._asset_timeframe(asset, timeframe, bars, plans))
        observations.sort(key=Observation.sort_key)
        if len(observations) > self.max_observations:
            raise DatasetBuildError(
                f"{len(observations)} observations exceed the limit of {self.max_observations} "
                "(ML_MAX_OBSERVATIONS)"
            )
        return BuiltDataset(
            spec=spec,
            origin=DataOrigin.TEST_FIXTURE if spec.fixture else DataOrigin.REAL_HISTORICAL,
            feature_schema=schema,
            observations=observations,
            statistics=self._statistics(observations, plans),
            warnings=self._warnings(observations, plans),
            asset_ids=[a.id for a in assets],
        )

    def _asset_timeframe(
        self,
        asset: Asset,
        timeframe: str,
        bars: PreloadedBars,
        plans: Sequence[_StrategyPlan],
    ) -> Iterable[Observation]:
        spec = self.spec
        requested = self.registry.resolve_all(self._requests(timeframe))
        aux = self._aux(timeframe)
        active = [p for p in plans if timeframe in p.resolved]
        union: dict[str, ResolvedFeature] = {f.key: f for f in requested}
        for f in (*aux.values(), *(r for p in active for r in p.resolved[timeframe])):
            union.setdefault(f.key, f)
        features = [union[k] for k in sorted(union)]
        periods = decision_periods(
            self.calendar, timeframe, spec.decision_start, spec.decision_end, self.series.settle
        )
        for period in periods:
            self.stats["decision_points"] += 1
            if not self._member(asset.id, period.decision_at):
                self.stats["skipped_not_in_universe"] += 1
                continue
            decision_bar = bars.bar(asset.id, period.last.day)
            if decision_bar is None or decision_bar.quality_status != "VALID":
                self.stats["skipped_no_decision_bar"] += 1
                continue
            inputs = self._inputs(asset.id, timeframe, period.decision_at, features, bars)
            values = inputs.values
            invalid = [f for f in requested if not f.optional and not values[f.key].valid]
            if invalid:
                self.stats[f"rejected_required_feature:{values[invalid[0].key].status.value}"] += 1
                continue
            stored = {_column(f): _stored(values[f.key]) for f in requested}
            available = _newest(values[f.key].available_at for f in requested)
            segments = self._segments(values, aux)
            atr = _number(values.get(aux["atr_pct"].key))
            close = decision_bar.close
            common: dict[str, Any] = {
                "asset_id": asset.id,
                "symbol": asset.symbol,
                "timeframe": timeframe,
                "decision_at": period.decision_at,
                "data_known_at": self.known_at,
                "features": stored,
                "features_available_at": available,
                "last_input_period_at": inputs.last_period_event_at,
                "segments": segments,
            }
            if spec.source is ObservationSource.PERIOD_CLOSE:
                horizon = spec.target.horizon_periods or 1
                for direction in spec.directions:
                    yield Observation(
                        **common,
                        direction=direction,
                        source=spec.source,
                        strategy_id=None,
                        strategy_version=None,
                        proposal_id=None,
                        horizon_periods=horizon,
                        label=self._label(bars, asset.id, period, direction, horizon, close, atr),
                    )
                continue
            for plan in active:
                yield from self._replay(
                    plan, asset, timeframe, period, values, common, bars, atr, close
                )

    def _replay(
        self,
        plan: _StrategyPlan,
        asset: Asset,
        timeframe: str,
        period: DecisionPeriod,
        values: dict[str, FeatureValue],
        common: dict[str, Any],
        bars: PreloadedBars,
        atr: float | None,
        close: float,
    ) -> Iterable[Observation]:
        sid = plan.strategy.strategy_id
        if asset.asset_class not in plan.strategy.supported_asset_classes:
            self.stats[f"replay:{sid}:unsupported_asset"] += 1
            return
        eligibility = self._eligibility.get(asset.id)
        if eligibility is None:
            eligibility = load_eligibility(
                self.session, asset.id, provider=self.series.provider, known_at=self.known_at
            )
            self._eligibility[asset.id] = eligibility
        required = [f for f in plan.resolved[timeframe] if not f.optional]
        if any(not values[f.key].valid for f in required):
            self.stats[f"replay:{sid}:inputs_not_valid"] += 1
            return
        view = AssetView(
            asset.id, asset.symbol, asset.asset_class, asset.exchange_code, eligibility
        )
        ctx = EvaluationContext(
            asset=view,
            timeframe=timeframe,
            mode=EvaluationMode.HISTORICAL,
            basis=KnowledgeBasis.RECONSTRUCTED,
            data_cutoff=period.decision_at,
            features={f.key: values[f.key] for f in plan.resolved[timeframe]},
            trigger=TriggerKind.MANUAL,
            registry=self.registry,
        )
        outcome = plan.strategy.evaluate(ctx, plan.params)
        self.stats[f"replay:{sid}:evaluated"] += 1
        if outcome.invalid_data is not None:
            self.stats[f"replay:{sid}:invalid_data"] += 1
            return
        horizon = self.spec.target.horizon_periods or plan.params.holding_horizon_periods
        for draft in outcome.proposals:
            if draft.direction not in self.spec.directions:
                continue
            if eligibility.direction_reason(draft.direction) is not None:
                self.stats[f"replay:{sid}:ineligible_direction"] += 1
                continue
            self.stats[f"replay:{sid}:proposals"] += 1
            yield Observation(
                **common,
                direction=draft.direction,
                source=ObservationSource.STRATEGY_REPLAY,
                strategy_id=sid,
                strategy_version=plan.version,
                proposal_id=None,
                horizon_periods=horizon,
                label=self._label(
                    bars,
                    asset.id,
                    period,
                    draft.direction,
                    horizon,
                    close,
                    atr,
                    _float(draft.invalidation_level),
                    _float(draft.target_reference),
                ),
            )

    def _period_at(self, timeframe: str, cutoff: datetime) -> DecisionPeriod | None:
        """The newest period completed and settled by ``cutoff``."""
        settle = self.series.settle
        newest = completed_sessions(self.calendar, cutoff, 1, settle)[0]
        if timeframe == "1Day":
            return DecisionPeriod(timeframe, (newest,), newest.regular_close + settle)
        session: SessionDay = newest
        for _ in range(10):  # the newest week whose last session completed
            nxt = next_trading_day(self.calendar, session.day)
            if week_key(nxt.day) != week_key(session.day):
                return DecisionPeriod(timeframe, (session,), session.regular_close + settle)
            session = previous_trading_day(self.calendar, session.day)
        return None

    def _from_proposals(self, assets: Sequence[Asset], bars: PreloadedBars) -> list[Observation]:
        spec = self.spec
        p, v = StrategyProposalRecord, StrategyVersionRecord
        by_id = {a.id: a for a in assets}
        stmt = (
            select(p, v.logic_version, v.configuration_id)
            .join(v, v.id == p.strategy_version_id)
            .where(
                p.asset_id.in_(list(by_id)),
                p.timeframe.in_(list(spec.timeframes)),
                p.direction.in_(list(spec.directions)),
                p.data_cutoff >= spec.decision_start,
                p.data_cutoff <= spec.decision_end,
                p.created_at <= self.known_at,
            )
            .order_by(p.data_cutoff, p.id)
        )
        if spec.strategies:
            stmt = stmt.where(p.strategy_id.in_(list(spec.strategies)))
        found: list[Observation] = []
        for proposal, logic, configuration in self.session.execute(stmt).all():
            self.stats["proposals_found"] += 1
            asset = by_id[proposal.asset_id]
            tf = proposal.timeframe
            period = self._period_at(tf, proposal.data_cutoff)
            if period is None:
                self.stats["proposals_without_period"] += 1
                continue
            decision_bar = bars.bar(asset.id, period.last.day)
            if decision_bar is None or decision_bar.quality_status != "VALID":
                self.stats["skipped_no_decision_bar"] += 1
                continue
            requested = self.registry.resolve_all(self._requests(tf))
            aux = self._aux(tf)
            union = {f.key: f for f in (*requested, *aux.values())}
            inputs = self._inputs(
                asset.id, tf, proposal.data_cutoff, [union[k] for k in sorted(union)], bars
            )
            values = inputs.values
            if any(not f.optional and not values[f.key].valid for f in requested):
                self.stats["rejected_required_feature"] += 1
                continue
            horizon = spec.target.horizon_periods or proposal.holding_horizon_periods
            atr = _number(values.get(aux["atr_pct"].key))
            found.append(
                Observation(
                    asset_id=asset.id,
                    symbol=asset.symbol,
                    timeframe=tf,
                    direction=proposal.direction,
                    decision_at=proposal.data_cutoff,
                    data_known_at=self.known_at,
                    source=ObservationSource.STRATEGY_PROPOSALS,
                    strategy_id=proposal.strategy_id,
                    strategy_version=f"{proposal.strategy_id}@{logic}#{str(configuration)[:12]}",
                    proposal_id=proposal.id,
                    horizon_periods=horizon,
                    features={_column(f): _stored(values[f.key]) for f in requested},
                    features_available_at=_newest(values[f.key].available_at for f in requested),
                    last_input_period_at=inputs.last_period_event_at,
                    segments=self._segments(values, aux),
                    label=self._label(
                        bars,
                        asset.id,
                        period,
                        proposal.direction,
                        horizon,
                        decision_bar.close,
                        atr,
                        _float(proposal.invalidation_level),
                        _float(proposal.target_reference),
                    ),
                )
            )
        return found

    # --- Reporting --------------------------------------------------------------------

    def _statistics(
        self, observations: Sequence[Observation], plans: Sequence[_StrategyPlan]
    ) -> dict[str, Any]:
        labels = Counter(o.label.status.value for o in observations)
        matured = [o for o in observations if o.label.status is LabelStatus.MATURED]
        classes = Counter(o.label.cls for o in matured if o.label.cls is not None)
        undefined = Counter(
            str(o.label.outcome.get("reason"))
            for o in observations
            if o.label.status is LabelStatus.UNDEFINED
        )
        net = [float(o.label.outcome["net_return"]) for o in matured]
        exits = Counter(str(o.label.outcome.get("exit_reason")) for o in matured)
        return {
            "observations": len(observations),
            "label_status": dict(labels),
            "undefined_reasons": dict(undefined),
            "class_balance": {str(k): v for k, v in sorted(classes.items())},
            "directions": dict(Counter(o.direction.value for o in observations)),
            "timeframes": dict(Counter(o.timeframe for o in observations)),
            "strategies": dict(Counter(o.strategy_id or "none" for o in observations)),
            "exit_reasons": dict(exits),
            "mean_net_return": sum(net) / len(net) if net else None,
            "mean_label_concurrency": _mean_concurrency(matured),
            "build_counts": dict(sorted(self.stats.items())),
            "strategy_versions": {p.strategy.strategy_id: p.version for p in plans},
            "strategy_parameter_sources": {
                p.strategy.strategy_id: p.parameters_source for p in plans
            },
            "segment_definition": SEGMENT_DOCUMENTATION,
            "feature_registry_version": FEATURE_REGISTRY_VERSION,
        }

    def _warnings(
        self, observations: Sequence[Observation], plans: Sequence[_StrategyPlan]
    ) -> list[str]:
        spec = self.spec
        warnings = list(self.warnings)
        if spec.fixture:
            warnings.append("TEST FIXTURE: the bars are synthetic test data, not market data.")
        if spec.universe is UniversePolicy.FIXED_ASSETS:
            warnings.append(
                "Survivorship bias possible: the asset list was chosen when the dataset was "
                "specified, not from the universe known at each decision time."
            )
        warnings.append(
            "RECONSTRUCTED basis: features use bars as stored at data_known_at (possibly "
            "retrieved after the decision), but only periods completed by each decision time."
        )
        warnings.append(
            f"Transaction costs are an assumption ({spec.target.round_trip_cost_bps:g} bps "
            "round trip); short borrow fees are not modelled."
        )
        horizon = spec.target.horizon_periods
        if horizon is None or horizon > 1:
            warnings.append(
                "Labels overlap in time (horizon > 1 period): splits must purge and embargo "
                "overlapping observations."
            )
        if plans:
            warnings.append(
                "Strategy replay: eligibility flags are those known at data_known_at, not at "
                "each decision time; strategies were evaluated in process and nothing was "
                "recorded as a proposal."
            )
        if any(o.timeframe == "1Week" for o in observations):
            warnings.append("Weekly barrier paths are followed on daily bars.")
        matured = sum(1 for o in observations if o.label.status is LabelStatus.MATURED)
        if matured < 500:
            warnings.append(
                f"Only {matured} matured observations: too few for reliable model evaluation."
            )
        if (
            spec.target.kind is TargetKind.BARRIER
            and spec.target.barrier is not None
            and spec.target.barrier.source is BarrierSource.STRATEGY_LEVELS
        ):
            warnings.append("Barrier levels are the strategies' own invalidation and target.")
        return warnings


def _newest(times: Iterable[datetime | None]) -> datetime | None:
    return max((t for t in times if t is not None), default=None)


def _float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _stored(value: FeatureValue) -> dict[str, Any]:
    raw: Any = value.value
    if isinstance(raw, float):
        raw = repr(raw)
    return {
        "value": raw,
        "status": value.status.value,
        "available_at": _iso(value.available_at),
    }


def _mean_concurrency(observations: Sequence[Observation]) -> float | None:
    """Mean number of labels (same asset, timeframe and direction) whose windows overlap
    each label's window, itself included: 1 means no overlap."""
    groups: dict[tuple[Any, ...], list[tuple[datetime, datetime]]] = {}
    for o in observations:
        if o.label.end_at is not None:
            key = (o.asset_id, o.timeframe, o.direction, o.strategy_id)
            groups.setdefault(key, []).append((o.decision_at, o.label.end_at))
    counts: list[int] = []
    for spans in groups.values():
        spans.sort()
        for i, (start, end) in enumerate(spans):
            n = 1
            for other_start, _ in spans[i + 1 :]:
                if other_start >= end:
                    break
                n += 1
            for _, other_end in spans[:i]:
                if other_end > start:
                    n += 1
            counts.append(n)
    return sum(counts) / len(counts) if counts else None


def build_dataset(
    session: Session,
    spec: DatasetSpec,
    *,
    provider: str,
    feed: str,
    calendar: ExchangeCalendar,
    now: datetime,
    strategies: StrategyRegistry | None = None,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
) -> BuiltDataset:
    return DatasetBuilder(
        session,
        spec,
        provider=provider,
        feed=feed,
        calendar=calendar,
        now=now,
        strategies=strategies,
        max_observations=max_observations,
    ).build()


__all__ = [
    "SEGMENT_DOCUMENTATION",
    "SEGMENT_VERSION",
    "BuiltDataset",
    "DatasetBuildError",
    "DatasetBuilder",
    "Observation",
    "SchemaItem",
    "build_dataset",
]
