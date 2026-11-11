"""The event-driven replay (Phase 8A).

Time moves session by session over the XNYS calendar. A session's information becomes
known at one instant, ``t = regular close + settle``: the daily bar of that session
(execution, valuation) and the completion of the period (strategy decisions). At ``t`` the
replay processes, in this fixed order:

1. **corporate actions** that took effect at the session's open (splits);
2. **execution** of every working order against the session's bar, asset by asset in
   symbol order: entries, then protective stops, then take-profits, then market exits
   (stop first; a bar is used once per order);
3. **expiry** of entry orders whose validity ended at or before the session's close, and
   the release of their reservations' unconsumed remainder;
4. **valuation** of open positions at the session's RAW close (6A ``valuation``) and short
   borrow accrual (6A ``financing_cost``), then an independent **reconciliation**;
5. the **equity snapshot** and the **risk monitor** (6B breaker transitions; the
   simulated operator's resets);
6. **holding-horizon exits** (market orders that execute on the next session's bar);
7. **strategy decisions** for every period completed at ``t`` (daily; weekly at the last
   session of an ISO week): point-in-time features, the shared
   :func:`~app.strategies.engine.decide` step, the 6B assessment with the account and its
   reservations, a reservation and an entry order with its bracket.

Orders reach the simulator ``latency`` after ``t``; the next bar they may use starts at the
next session's open. Nothing decided at ``t`` can use a price observed after ``t``, and an
order can never execute against the bar that produced its signal.

Ties are broken by fixed keys (timeframe, strategy id, symbol, direction, sequence numbers),
so a replay of the same inputs produces the same results. No randomness is used; the run's
seed is recorded and seeds an unused generator so any future stochastic element is fixed.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.accounting.money import ZERO, currency
from app.accounting.templates import InstrumentInfo, PriceMark, TradeParams
from app.asset_discovery import metrics as discovery_metrics
from app.asset_discovery.enums import MetricStatus as DiscoveryMetricStatus
from app.asset_discovery.membership import members_at
from app.asset_discovery.sessions import (
    SessionDay,
    completed_sessions,
    local_date,
    next_trading_day,
    previous_trading_day,
)
from app.backtesting.broker import SimFill, SimOrder, evaluate
from app.backtesting.config import ResolvedAsset, ResolvedConfig, ResolvedStrategy, sha256_of
from app.backtesting.data import (
    CALENDAR,
    DayBar,
    HistoricalDataset,
    PointInTimeBars,
    SplitEvent,
    sessions_between,
)
from app.backtesting.enums import (
    EntryOrderType,
    FeatureAdjustment,
    OrderRole,
    ProposalOutcome,
    RejectionStage,
    RunEventKind,
    StopOrderType,
    TradeStatus,
    UniverseMode,
)
from app.backtesting.ledger import BacktestTemplateContext, LedgerError, SimulatedLedger
from app.backtesting.portfolio import PositionMark, RiskBook
from app.data_engine.enums import PriceAdjustment
from app.database.models import AssetClass
from app.execution.enums import AttemptOutcome, OrderSide, OrderStatus, OrderType
from app.execution.portfolio import TEMPLATES as FILL_TEMPLATES
from app.feature_engine.registry import DAILY, WEEKLY, ResolvedFeature
from app.market_intelligence.enums import KnowledgeBasis
from app.paper_broker.execution_model import AssetClassModel, ExecutionModel
from app.paper_broker.fill_model import cost_round
from app.risk import data as risk_data
from app.risk.assessment import assess
from app.risk.config import RiskConfiguration
from app.risk.enums import AssessmentResult, ProposalSource, TradeDirection
from app.risk.models import (
    AssessmentInputs,
    InstrumentData,
    MarketData,
    PortfolioView,
    Proposal,
    ReservedExposure,
)
from app.risk.sizing import floor_to_increment, sizer_for
from app.strategies.base import AssetView, Eligibility, Strategy, StrategyParameters
from app.strategies.engine import EvaluationStageError, decide, ineligibility
from app.strategies.enums import Direction, EvaluationMode, SignalState, TriggerKind
from app.strategies.inputs import BarSeries, compute_features
from app.strategies.periods import expires_at
from app.strategies.registry import StrategyRegistry, default_registry

ONE = Decimal(1)
PRICE_PLACES = Decimal("0.0000000001")
NAMESPACE = uuid.UUID("6f1c2a52-8a1e-4b7a-9a5e-0c8a4f8b2d31")
LIQUIDITY_SESSIONS = 60


class BacktestError(RuntimeError):
    """The replay cannot continue correctly (a refused posting, failed reconciliation)."""

    def __init__(self, stage: str, message: str, at: datetime | None = None):
        super().__init__(message)
        self.stage, self.at = stage, at


# ---------------------------------------------------------------------- records
@dataclass
class Reservation:
    """A 6C reservation's capacity as the backtest holds it (approval amounts, consumed
    in proportion by entry fills, the remainder released when the entry order ends)."""

    seq: int
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    direction: TradeDirection
    sector: str | None
    cluster: str | None
    quantity: Decimal
    remaining_quantity: Decimal
    remaining: dict[str, Decimal]
    active: bool = True

    def exposure(self) -> ReservedExposure:
        return ReservedExposure(
            reservation_id=uuid.uuid5(NAMESPACE, f"reservation:{self.seq}"),
            asset_id=self.asset_id,
            symbol=self.symbol,
            asset_class=self.asset_class,
            direction=self.direction,
            sector=self.sector,
            cluster=self.cluster,
            remaining_quantity=self.remaining_quantity,
            exposure=self.remaining["exposure"],
            capital=self.remaining["capital"],
            margin=self.remaining["margin"],
            risk=self.remaining["risk"],
        )

    def consume(self, quantity: Decimal) -> None:
        """6C partial consumption: frees floor(remaining x q / remaining_q) of each amount;
        the last consumption takes exactly what is left."""
        for key, amount in self.remaining.items():
            if quantity >= self.remaining_quantity:
                freed = amount
            else:
                freed = (amount * quantity / self.remaining_quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_FLOOR
                )
            self.remaining[key] = amount - freed
        self.remaining_quantity = max(self.remaining_quantity - quantity, ZERO)
        if self.remaining_quantity == 0:
            self.active = False

    def release(self) -> None:
        self.active = False


@dataclass
class ProposalRecord:
    seq: int
    decided_at: datetime
    session: date
    strategy_id: str
    strategy_version: int
    timeframe: str
    asset_id: uuid.UUID
    symbol: str
    direction: Direction
    reference_entry: Decimal
    invalidation_level: Decimal | None
    target_reference: Decimal | None
    signal_strength: Decimal
    holding_horizon_periods: int
    expires_at: datetime
    reasoning: str
    evidence: list[dict[str, Any]]
    levels: dict[str, Any]
    feature_sha256: str
    risk_result: str | None = None
    risk_reasons: list[dict[str, Any]] = field(default_factory=list)
    permitted_quantity: Decimal | None = None
    binding_constraint: str | None = None
    risk_budget: Decimal | None = None
    potential_loss: Decimal | None = None
    estimated_costs: Decimal | None = None
    reservation: dict[str, Any] | None = None
    outcome: ProposalOutcome | None = None
    order_seq: int | None = None
    trade_seq: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "decided_at": self.decided_at.isoformat(),
            "session": self.session.isoformat(),
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "timeframe": self.timeframe,
            "asset_id": str(self.asset_id),
            "symbol": self.symbol,
            "direction": self.direction.value,
            "reference_entry": str(self.reference_entry),
            "invalidation_level": _s(self.invalidation_level),
            "target_reference": _s(self.target_reference),
            "signal_strength": str(self.signal_strength),
            "holding_horizon_periods": self.holding_horizon_periods,
            "expires_at": self.expires_at.isoformat(),
            "reasoning": self.reasoning,
            "evidence": self.evidence,
            "levels": self.levels,
            "feature_sha256": self.feature_sha256,
            "risk_result": self.risk_result,
            "risk_reasons": self.risk_reasons,
            "permitted_quantity": _s(self.permitted_quantity),
            "binding_constraint": self.binding_constraint,
            "risk_budget": _s(self.risk_budget),
            "potential_loss": _s(self.potential_loss),
            "estimated_costs": _s(self.estimated_costs),
            "reservation": self.reservation,
            "outcome": None if self.outcome is None else self.outcome.value,
            "order_seq": self.order_seq,
            "trade_seq": self.trade_seq,
        }


@dataclass
class Rejection:
    seq: int
    at: datetime
    stage: RejectionStage
    proposal_seq: int | None
    strategy_id: str
    symbol: str
    direction: str | None
    code: str
    message: str

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "stage": self.stage.value,
            "proposal_seq": self.proposal_seq,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "code": self.code,
            "message": self.message[:2000],
        }


@dataclass
class Trade:
    seq: int
    proposal_seq: int
    strategy_id: str
    strategy_version: int
    timeframe: str
    asset_id: uuid.UUID
    symbol: str
    asset_class: str
    direction: TradeDirection
    entry_order_seq: int
    opened_at: datetime
    first_session: date
    stop_level: Decimal
    target_level: Decimal | None
    horizon_periods: int
    horizon_end: datetime | None = None
    stop_order_seq: int | None = None
    tp_order_seq: int | None = None
    exit_order_seqs: list[int] = field(default_factory=list)
    entry_quantity: Decimal = ZERO
    entry_value: Decimal = ZERO
    open_quantity: Decimal = ZERO
    cost_open: Decimal = ZERO  # entry cost (long) / proceeds (short) of the open quantity
    exit_quantity: Decimal = ZERO
    exit_value: Decimal = ZERO
    realized_gross: Decimal = ZERO
    entry_costs: Decimal = ZERO
    exit_costs: Decimal = ZERO
    borrow_accrued: Decimal = ZERO
    borrow_paid: Decimal = ZERO
    borrow_through: date | None = None
    split_factor: Decimal = ONE
    exit_reason: str | None = None
    closed_at: datetime | None = None
    last_session: date | None = None
    mark_price: Decimal | None = None
    status: TradeStatus = TradeStatus.OPEN_AT_END
    notes: list[str] = field(default_factory=list)

    @property
    def long(self) -> bool:
        return self.direction is TradeDirection.LONG

    @property
    def costs(self) -> Decimal:
        return self.entry_costs + self.exit_costs + self.borrow_accrued

    @property
    def unrealized(self) -> Decimal:
        if self.mark_price is None or self.open_quantity == 0:
            return ZERO
        value = self.open_quantity * self.mark_price
        return value - self.cost_open if self.long else self.cost_open - value

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_gross + self.unrealized - self.costs

    def as_json(self) -> dict[str, Any]:
        avg_entry = None if self.entry_quantity == 0 else self.entry_value / self.entry_quantity
        avg_exit = None if self.exit_quantity == 0 else self.exit_value / self.exit_quantity
        return {
            "seq": self.seq,
            "proposal_seq": self.proposal_seq,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "timeframe": self.timeframe,
            "asset_id": str(self.asset_id),
            "symbol": self.symbol,
            "direction": self.direction.value,
            "status": self.status.value,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": None if self.closed_at is None else self.closed_at.isoformat(),
            "first_session": self.first_session.isoformat(),
            "last_session": None if self.last_session is None else self.last_session.isoformat(),
            "entry_quantity": str(self.entry_quantity),
            "entry_price": _s(_q(avg_entry)),
            "exit_quantity": str(self.exit_quantity),
            "exit_price": _s(_q(avg_exit)),
            "open_quantity": str(self.open_quantity),
            "stop_level": str(self.stop_level),
            "target_level": _s(self.target_level),
            "horizon_end": None if self.horizon_end is None else self.horizon_end.isoformat(),
            "exit_reason": self.exit_reason,
            "realized_gross": str(self.realized_gross),
            "unrealized": str(self.unrealized),
            "entry_costs": str(self.entry_costs),
            "exit_costs": str(self.exit_costs),
            "borrow_costs": str(self.borrow_accrued),
            "net_pnl": str(self.net_pnl),
            "split_factor": str(self.split_factor),
            "mark_price": _s(self.mark_price),
            "holding_sessions": None
            if self.last_session is None
            else max(len(sessions_between(self.first_session, self.last_session)) - 1, 0),
            "entry_order_seq": self.entry_order_seq,
            "stop_order_seq": self.stop_order_seq,
            "tp_order_seq": self.tp_order_seq,
            "exit_order_seqs": self.exit_order_seqs,
            "notes": self.notes,
        }


@dataclass
class Snapshot:
    session: date
    at: datetime
    equity: Decimal
    cash: Decimal
    contributed_capital: Decimal
    long_exposure: Decimal
    short_exposure: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    commissions: Decimal
    fees: Decimal
    borrow_costs: Decimal
    open_positions: int
    open_orders: int
    reserved_exposure: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    mark_status: str
    active_breakers: list[str]

    @property
    def total_costs(self) -> Decimal:
        return self.commissions + self.fees + self.borrow_costs

    @property
    def drawdown_pct(self) -> Decimal:
        return ZERO if self.peak_equity <= 0 else self.drawdown / self.peak_equity

    def as_json(self) -> dict[str, Any]:
        return {
            "session": self.session.isoformat(),
            "at": self.at.isoformat(),
            "equity": str(self.equity),
            "gross_equity": str(self.equity + self.total_costs),
            "cash": str(self.cash),
            "contributed_capital": str(self.contributed_capital),
            "long_exposure": str(self.long_exposure),
            "short_exposure": str(self.short_exposure),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "commissions": str(self.commissions),
            "fees": str(self.fees),
            "borrow_costs": str(self.borrow_costs),
            "total_costs": str(self.total_costs),
            "open_positions": self.open_positions,
            "open_orders": self.open_orders,
            "reserved_exposure": str(self.reserved_exposure),
            "peak_equity": str(self.peak_equity),
            "drawdown": str(self.drawdown),
            "drawdown_pct": str(_q(self.drawdown_pct, Decimal("0.00000001"))),
            "mark_status": self.mark_status,
            "active_breakers": self.active_breakers,
        }


@dataclass
class RunEvent:
    seq: int
    at: datetime
    kind: RunEventKind
    symbol: str | None
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "kind": self.kind.value,
            "symbol": self.symbol,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class RunIssue:
    """A non-fatal problem (a strategy raised, a feature failed); the run continues."""

    seq: int
    at: datetime
    stage: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    fatal: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "stage": self.stage,
            "message": self.message[:2000],
            "details": self.details,
            "fatal": self.fatal,
        }


@dataclass
class Checkpoint:
    seq: int
    session: date
    simulated_at: datetime
    sessions_done: int
    sessions_total: int
    evaluations: int
    proposals: int
    fills: int
    journal_entries: int
    reconciliation: str
    equity: Decimal

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "session": self.session.isoformat(),
            "simulated_at": self.simulated_at.isoformat(),
            "sessions_done": self.sessions_done,
            "sessions_total": self.sessions_total,
            "evaluations": self.evaluations,
            "proposals": self.proposals,
            "fills": self.fills,
            "journal_entries": self.journal_entries,
            "reconciliation": self.reconciliation,
            "equity": str(self.equity),
        }


@dataclass
class BacktestResult:
    config_sha256: str
    dataset_sha256: str
    proposals: list[ProposalRecord]
    rejections: list[Rejection]
    orders: list[SimOrder]
    fills: list[SimFill]
    trades: list[Trade]
    snapshots: list[Snapshot]
    events: list[RunEvent]
    issues: list[RunIssue]
    checkpoints: list[Checkpoint]
    journal: list[dict[str, Any]]
    evaluations: dict[str, int]
    reconciliation: dict[str, Any]
    breakers: list[dict[str, Any]]
    sessions: list[date]
    benchmark_series: dict[str, list[tuple[date, Decimal]]]
    metrics: list[dict[str, Any]] = field(default_factory=list)
    benchmarks: list[dict[str, Any]] = field(default_factory=list)

    def content(self) -> dict[str, Any]:
        """Everything a reproduction must reproduce (no run id, no wall-clock time)."""
        return {
            "config_sha256": self.config_sha256,
            "dataset_sha256": self.dataset_sha256,
            "proposals": [p.as_json() for p in self.proposals],
            "rejections": [r.as_json() for r in self.rejections],
            "orders": [order_json(o) for o in self.orders],
            "fills": [fill_json(f) for f in self.fills],
            "trades": [t.as_json() for t in self.trades],
            "snapshots": [s.as_json() for s in self.snapshots],
            "events": [e.as_json() for e in self.events],
            "issues": [i.as_json() for i in self.issues],
            "journal": self.journal,
            "evaluations": self.evaluations,
            "breakers": self.breakers,
            "metrics": self.metrics,
            "benchmarks": self.benchmarks,
        }

    def sha256(self) -> str:
        return sha256_of(self.content())


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _q(value: Decimal | None, places: Decimal = PRICE_PLACES) -> Decimal | None:
    return None if value is None else value.quantize(places)


def order_json(o: SimOrder) -> dict[str, Any]:
    return {
        "seq": o.seq,
        "role": o.role.value,
        "trade_seq": o.trade_seq,
        "proposal_seq": o.proposal_seq,
        "asset_id": str(o.asset_id),
        "symbol": o.symbol,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "quantity": str(o.quantity),
        "filled_quantity": str(o.filled),
        "average_price": _s(_q(o.average_price)),
        "limit_price": _s(o.limit_price),
        "stop_price": _s(o.stop_price),
        "trail_percent": _s(o.trail_percent),
        "protection_price": _s(o.protection_price),
        "arrival_at": o.arrival_at.isoformat(),
        "expires_at": None if o.expires_at is None else o.expires_at.isoformat(),
        "status": None if o.status is None else o.status.value,
        "commission": str(o.commission),
        "fees": str(o.fees),
        "final_reason": o.final_reason,
        "last_outcome": o.last_outcome,
        "attempts": o.attempts,
        "events": [e.as_json() for e in o.events],
    }


def fill_json(f: SimFill) -> dict[str, Any]:
    return {
        "seq": f.seq,
        "order_seq": f.order_seq,
        "trade_seq": f.trade_seq,
        "asset_id": str(f.asset_id),
        "symbol": f.symbol,
        "side": f.side.value,
        "role": f.role.value,
        "session": f.session.isoformat(),
        "filled_at": f.filled_at.isoformat(),
        "known_at": f.known_at.isoformat(),
        "quantity": str(f.quantity),
        "price": str(f.price),
        "notional": str(f.notional),
        "commission": str(f.commission),
        "fees": str(f.fees),
        "basis": f.basis,
        "reference_price": _s(f.reference_price),
        "slippage_bps": _s(f.slippage_bps),
        "impact_bps": str(f.impact_bps),
        "participation": _s(f.participation),
        "liquidity": _s(f.liquidity),
        "gap": f.gap,
        "assumptions": list(f.assumptions),
        "journal_sequence": f.journal_sequence,
    }


# ---------------------------------------------------------------------- engine
@dataclass
class _PreparedStrategy:
    resolved: ResolvedStrategy
    strategy: Strategy
    params: StrategyParameters
    features: dict[str, list[ResolvedFeature]]


ProgressCallback = Callable[[Checkpoint], None]


class BacktestEngine:
    def __init__(
        self,
        session: Session,
        config: ResolvedConfig,
        dataset: HistoricalDataset,
        *,
        registry: StrategyRegistry | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.dataset = dataset
        self.registry = registry or default_registry()
        self.progress = progress
        self.random = random.Random(config.seed)  # noqa: S311  # recorded seed; unused today
        self.risk_config: RiskConfiguration = config.risk_configuration()
        self.model: ExecutionModel = config.model()
        self.settle = timedelta(seconds=config.data.settle_seconds)
        self.news_delay = timedelta(minutes=config.data.news_delay_minutes)
        self.account_id = uuid.uuid5(NAMESPACE, f"account:{config.replay_sha256()}")
        self.series = BarSeries(
            provider=config.data.provider,
            feed=config.data.feed,
            adjustment=(
                PriceAdjustment.SPLIT
                if config.data.feature_adjustment is FeatureAdjustment.POINT_IN_TIME_SPLIT
                else PriceAdjustment.RAW
            ),
            calendar=CALENDAR,
            settle=self.settle,
        )
        self.bars = PointInTimeBars(
            dataset,
            self.series,
            split_adjusted=config.data.feature_adjustment is FeatureAdjustment.POINT_IN_TIME_SPLIT,
        )
        self.assets: dict[uuid.UUID, ResolvedAsset] = {
            uuid.UUID(a.asset_id): a for a in config.assets
        }
        self.ctx = BacktestTemplateContext(config.currency, {})
        self.ledger = SimulatedLedger(config.currency, self.ctx)
        self.book = RiskBook(self.account_id, self.risk_config, config.breaker_reset_policy)
        self.proposals: list[ProposalRecord] = []
        self.rejections: list[Rejection] = []
        self.orders: list[SimOrder] = []
        self.fills: list[SimFill] = []
        self.trades: list[Trade] = []
        self.snapshots: list[Snapshot] = []
        self.events: list[RunEvent] = []
        self.issues: list[RunIssue] = []
        self.checkpoints: list[Checkpoint] = []
        self.reservations: dict[int, Reservation] = {}
        self.evaluations: Counter[str] = Counter()
        self.universe: set[uuid.UUID] = set()
        self._liquidity: dict[tuple[uuid.UUID, date], tuple[Decimal | None, str | None]] = {}
        self._prepared = self._prepare()
        self._last_reconciliation: dict[str, Any] = {}

    # ------------------------------------------------------------------ preparation
    def _prepare(self) -> list[_PreparedStrategy]:
        out: list[_PreparedStrategy] = []
        for rs in sorted(self.config.strategies, key=lambda s: s.strategy_id):
            strategy = self.registry.get(rs.strategy_id)
            if strategy.logic_version != rs.logic_version:
                raise BacktestError(
                    "configuration",
                    f"{rs.strategy_id} version {rs.version} has logic {rs.logic_version}; the "
                    f"code has {strategy.logic_version}: the stored version cannot be replayed",
                )
            params = strategy.parse_parameters(rs.parameters)
            features = {
                tf: self.registry.features.resolve_all(strategy.required_features(params, tf))
                for tf in rs.timeframes
            }
            out.append(_PreparedStrategy(rs, strategy, params, features))
        return out

    def _class_model(self, asset_class: str) -> AssetClassModel:
        return self.model.for_class(asset_class)

    # ------------------------------------------------------------------ main loop
    def run(self) -> BacktestResult:
        sessions = sessions_between(self.config.start_date, self.config.end_date)
        if not sessions:
            raise BacktestError("configuration", "the date range contains no trading session")
        first = sessions[0]
        funded_at = first.regular_open - timedelta(minutes=1)
        capital = self.config.initial_capital
        self._post(
            "opening_funding",
            {"amount": str(capital), "currency": self.config.currency},
            funded_at,
            "funding",
        )
        self.book.funded_at, self.book.funding = funded_at, capital
        self.book.observe(funded_at, capital, capital)
        self._reconcile(funded_at)
        for index, day in enumerate(sessions):
            t = day.regular_close + self.settle
            self._session(day, t)
            checkpoint = Checkpoint(
                seq=index + 1,
                session=day.day,
                simulated_at=t,
                sessions_done=index + 1,
                sessions_total=len(sessions),
                evaluations=sum(self.evaluations.values()),
                proposals=len(self.proposals),
                fills=len(self.fills),
                journal_entries=self.ledger.sequence,
                reconciliation="PASSED",
                equity=self.snapshots[-1].equity,
            )
            self.checkpoints.append(checkpoint)
            if self.progress is not None:
                self.progress(checkpoint)
        self._finish(sessions[-1])
        return BacktestResult(
            config_sha256=self.config.replay_sha256(),
            dataset_sha256=self.dataset.sha256(),
            proposals=self.proposals,
            rejections=self.rejections,
            orders=self.orders,
            fills=self.fills,
            trades=self.trades,
            snapshots=self.snapshots,
            events=self.events,
            issues=self.issues,
            checkpoints=self.checkpoints,
            journal=[e.as_json() for e in self.ledger.journal],
            evaluations=dict(sorted(self.evaluations.items())),
            reconciliation=self._last_reconciliation,
            breakers=[
                {
                    "at": b.at.isoformat(),
                    "breaker": b.breaker.value,
                    "event": b.event.value,
                    "actor": b.actor,
                    "reason": b.reason,
                }
                for b in self.book.history
            ],
            sessions=[s.day for s in sessions],
            benchmark_series={},
        )

    def _session(self, day: SessionDay, t: datetime) -> None:
        for event in self.dataset.splits_on(day.day):
            self._split(event, day, t)
        self._execute(day, t)
        self._expire(day, t)
        self._value(day, t)
        self._reconcile(t)
        evaluated = self._snapshot(day, t)
        for record in self.book.monitor(evaluated.conditions, t):
            self._event(
                t,
                RunEventKind.BREAKER_TRIPPED
                if record.event.value == "TRIPPED"
                else RunEventKind.BREAKER_RESET,
                None,
                record.reason,
                {"breaker": record.breaker.value, "actor": record.actor},
            )
        self._horizon_exits(day, t)
        self._decide(day, t)

    # ------------------------------------------------------------------ helpers
    def _post(self, template: str, params: Any, at: datetime, ref: str) -> int:
        try:
            entry = self.ledger.post(template, params, at=at, source_ref=ref)
        except LedgerError as exc:
            raise BacktestError("accounting", str(exc), at) from exc
        assert entry is not None  # noqa: S101  # only valuations may be empty
        return entry.sequence

    def _reconcile(self, at: datetime) -> None:
        result = self.ledger.reconcile()
        self._last_reconciliation = {
            "at": at.isoformat(),
            "passed": result.passed,
            "sequence": result.sequence,
            "checks": result.checks,
        }
        if not result.passed:
            self.book.reconciliation_status = "FAILED"
            raise BacktestError(
                "reconciliation",
                "the simulated books do not reconcile: "
                + "; ".join(f"{c['check']}: {c['detail']}" for c in result.failures()),
                at,
            )
        self.book.reconciliation_status = "PASSED"
        self.book.reconciled_sequence = result.sequence

    def _event(
        self,
        at: datetime,
        kind: RunEventKind,
        symbol: str | None,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(RunEvent(len(self.events) + 1, at, kind, symbol, message, details or {}))

    def _issue(self, at: datetime, stage: str, message: str, **details: Any) -> None:
        self.issues.append(RunIssue(len(self.issues) + 1, at, stage, message, details))

    def _reject(
        self,
        at: datetime,
        stage: RejectionStage,
        proposal: ProposalRecord | None,
        strategy_id: str,
        symbol: str,
        direction: str | None,
        code: str,
        message: str,
    ) -> None:
        self.rejections.append(
            Rejection(
                len(self.rejections) + 1,
                at,
                stage,
                None if proposal is None else proposal.seq,
                strategy_id,
                symbol,
                direction,
                code,
                message,
            )
        )

    def _order(self, seq_owner: SimOrder) -> SimOrder:
        self.orders.append(seq_owner)
        return seq_owner

    def _instrument(self, asset_id: uuid.UUID, at: datetime) -> InstrumentInfo | None:
        known = self.ctx.instruments.get(asset_id)
        if known is not None:
            return known
        asset = self.assets[asset_id]
        spec = self.dataset.instrument_spec(asset_id, at)
        if spec is not None:
            info = InstrumentInfo(
                asset_id,
                asset.symbol,
                asset.asset_class,
                spec.currency,
                spec.quantity_increment,
                spec.min_quantity,
                spec.multiplier,
                spec.id,
            )
        else:
            ref = self.dataset.reference(asset_id, at)
            if ref.quantity_increment is None or ref.min_quantity is None:
                return None
            info = InstrumentInfo(
                asset_id,
                asset.symbol,
                asset.asset_class,
                asset.quote_currency,
                ref.quantity_increment,
                ref.min_quantity,
                ONE,
                None,
            )
        self.ctx.instruments[asset_id] = info
        return info

    def _tick(self, asset_id: uuid.UUID, price: Decimal, at: datetime) -> Decimal | None:
        asset = self.assets[asset_id]
        sizer = sizer_for(asset.asset_class)
        ref = self.dataset.reference(asset_id, at)
        return None if sizer is None else sizer.tick_size(ref.price_increment, price)

    def _new_order(
        self,
        role: OrderRole,
        asset_id: uuid.UUID,
        side: OrderSide,
        order_type: OrderType,
        quantity: Decimal,
        arrival: datetime,
        at: datetime,
        reason: str,
        **kw: Any,
    ) -> SimOrder:
        asset = self.assets[asset_id]
        info = self.ctx.instruments[asset_id]
        reference = kw.pop("reference_price")
        order = SimOrder(
            seq=len(self.orders) + 1,
            role=role,
            asset_id=asset_id,
            symbol=asset.symbol,
            asset_class=asset.asset_class,
            side=side,
            order_type=order_type,
            quantity=quantity,
            arrival_at=arrival,
            tick=self._tick(asset_id, reference, at),
            increment=info.quantity_increment,
            min_quantity=info.min_quantity,
            minor_units=currency(asset.quote_currency).minor_units,
            **kw,
        )
        order.submit(at, reason)
        return self._order(order)

    # ------------------------------------------------------------------ 1. splits
    def _split(self, event: SplitEvent, day: SessionDay, t: datetime) -> None:
        asset = self.assets.get(event.asset_id)
        if asset is None:
            return
        ratio = event.ratio
        affected = [
            tr for tr in self.trades if tr.asset_id == event.asset_id and tr.open_quantity > 0
        ]
        working = [
            o
            for o in self.orders
            if o.asset_id == event.asset_id and o.role is OrderRole.ENTRY and o.working
        ]
        self._event(
            event.effective_at,
            RunEventKind.CORPORATE_ACTION,
            asset.symbol,
            f"split {ratio} new per old share effective {day.day} (inferred from the SPLIT "
            "and RAW series)",
            {
                "ratio": str(ratio),
                "factor_before": str(event.factor_before),
                "factor_after": str(event.factor_after),
                "open_trades": len(affected),
                "working_entries": len(working),
            },
        )
        for order in working:
            order.cancel(event.effective_at, f"corporate action: split {ratio} at {day.day}")
            self._entry_final(order, t)
        if not affected:
            return
        before = previous_session(day.day)
        pre = self.dataset.last_raw_on_or_before(event.asset_id, before)
        if pre is None:
            raise BacktestError(
                "corporate_action",
                f"{asset.symbol}: no pre-split close to carry the position",
                t,
            )
        info = self.ctx.instruments[event.asset_id]
        price_new = (pre.close / ratio).quantize(PRICE_PLACES, rounding=ROUND_CEILING)
        at = event.effective_at
        total_old = sum((tr.open_quantity for tr in affected), ZERO)
        new_quantities = {
            tr.seq: floor_to_increment(tr.open_quantity * ratio, info.quantity_increment)
            for tr in affected
        }
        total_new = sum(new_quantities.values(), ZERO)
        long = affected[0].long
        close_side, open_side = (
            (OrderSide.SELL, OrderSide.BUY)
            if long
            else (OrderSide.BUY_TO_COVER, OrderSide.SELL_SHORT)
        )
        ref = f"corporate-action:{asset.symbol}:{day.day}"
        self._post(
            FILL_TEMPLATES[close_side],
            TradeParams(asset_id=event.asset_id, quantity=total_old, price=pre.close),
            at,
            ref,
        )
        if total_new > 0:
            self._post(
                FILL_TEMPLATES[open_side],
                TradeParams(asset_id=event.asset_id, quantity=total_new, price=price_new),
                at,
                ref,
            )
        for tr in affected:
            old, new = tr.open_quantity, new_quantities[tr.seq]
            lieu_old = old - new / ratio
            if lieu_old > 0:
                share = tr.cost_open * lieu_old / old
                proceeds = lieu_old * pre.close
                tr.realized_gross += (proceeds - share) if tr.long else (share - proceeds)
                tr.cost_open -= share
                tr.notes.append(
                    f"{lieu_old} pre-split shares settled as cash in lieu at {pre.close}"
                )
            tr.open_quantity = new
            tr.split_factor *= ratio
            tr.stop_level = (tr.stop_level / ratio).quantize(PRICE_PLACES)
            if tr.target_level is not None:
                tr.target_level = (tr.target_level / ratio).quantize(PRICE_PLACES)
            tr.notes.append(
                f"split {ratio} at {day.day}: carried at the pre-split close {pre.close}"
            )
            for order in self._trade_exits(tr):
                if order.stop_price is not None:
                    order.stop_price = (order.stop_price / ratio).quantize(PRICE_PLACES)
                if order.limit_price is not None:
                    order.limit_price = (order.limit_price / ratio).quantize(PRICE_PLACES)
                for name in ("trail_reference", "current_stop", "trigger_level", "adverse_level"):
                    value = getattr(order, name)
                    if value is not None:
                        setattr(order, name, (value / ratio).quantize(PRICE_PLACES))
                order.quantity = order.filled + new
            if new == 0:
                self._close_trade(tr, at, "CORPORATE_ACTION")

    # ------------------------------------------------------------------ 2. execution
    def _trade_exits(self, trade: Trade) -> list[SimOrder]:
        seqs = [s for s in (trade.stop_order_seq, trade.tp_order_seq, *trade.exit_order_seqs) if s]
        return [self.orders[s - 1] for s in seqs if self.orders[s - 1].working]

    def _execute(self, day: SessionDay, t: datetime) -> None:
        by_asset: dict[uuid.UUID, list[SimOrder]] = {}
        for order in self.orders:
            if order.working:
                by_asset.setdefault(order.asset_id, []).append(order)
        for asset_id in sorted(by_asset, key=lambda a: self.assets[a].symbol):
            bar = self.dataset.bar(asset_id, day.day)
            if bar is None or not bar.valid:
                continue
            model = self._class_model(self.assets[asset_id].asset_class)
            entries = [o for o in by_asset[asset_id] if o.role is OrderRole.ENTRY]
            for order in sorted(entries, key=lambda o: o.seq):
                self._run_entry(order, bar, model, t)
            for role in (OrderRole.STOP, OrderRole.TAKE_PROFIT, OrderRole.EXIT):
                current = [
                    o
                    for o in self.orders
                    if o.asset_id == asset_id and o.role is role and o.working
                ]
                for order in sorted(current, key=lambda o: o.seq):
                    self._run_exit(order, bar, model, t)

    def _fill(self, order: SimOrder, ev: Any, bar: DayBar, t: datetime) -> SimFill:
        fill = ev.fill
        side = order.side
        sequence = self._post(
            FILL_TEMPLATES[side],
            TradeParams(
                asset_id=order.asset_id,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.costs.commission,
                fees=fill.costs.fees,
            ),
            t,
            f"order:{order.seq}",
        )
        record = SimFill(
            seq=len(self.fills) + 1,
            order_seq=order.seq,
            trade_seq=order.trade_seq,
            asset_id=order.asset_id,
            symbol=order.symbol,
            side=side,
            role=order.role,
            session=bar.session,
            filled_at=bar.end,
            known_at=t,
            quantity=fill.quantity,
            price=fill.price,
            commission=fill.costs.commission,
            fees=fill.costs.fees,
            basis=fill.basis.value,
            reference_price=fill.reference_price,
            slippage_bps=fill.slippage_bps,
            impact_bps=fill.impact_bps,
            participation=fill.participation,
            liquidity=fill.liquidity,
            gap=fill.gap,
            assumptions=fill.assumptions,
            journal_sequence=sequence,
        )
        self.fills.append(record)
        return record

    def _run_entry(self, order: SimOrder, bar: DayBar, model: AssetClassModel, t: datetime) -> None:
        ev = evaluate(order, bar, model, max_fill=order.remaining)
        if ev is None or ev.fill is None:
            return
        fill = self._fill(order, ev, bar, t)
        proposal = self.proposals[order.proposal_seq - 1] if order.proposal_seq else None
        reservation = self.reservations.get(order.proposal_seq or -1)
        if reservation is not None:
            reservation.consume(fill.quantity)
        trade = self.trades[order.trade_seq - 1] if order.trade_seq else None
        if trade is None:
            assert proposal is not None  # noqa: S101  # entries come from proposals
            trade = self._open_trade(order, proposal, bar, t)
        trade.entry_quantity += fill.quantity
        trade.entry_value += fill.notional
        trade.open_quantity += fill.quantity
        trade.cost_open += fill.notional
        trade.entry_costs += fill.commission + fill.fees
        for exit_order in self._trade_exits(trade):
            if exit_order.role in (OrderRole.STOP, OrderRole.TAKE_PROFIT):
                exit_order.resize(exit_order.filled + trade.open_quantity)
        if order.final:
            self._entry_final(order, t)

    def _open_trade(
        self, order: SimOrder, proposal: ProposalRecord, bar: DayBar, t: datetime
    ) -> Trade:
        direction = TradeDirection(proposal.direction.value)
        stop = Decimal(proposal.reservation["stop"]) if proposal.reservation else ZERO
        trade = Trade(
            seq=len(self.trades) + 1,
            proposal_seq=proposal.seq,
            strategy_id=proposal.strategy_id,
            strategy_version=proposal.strategy_version,
            timeframe=proposal.timeframe,
            asset_id=order.asset_id,
            symbol=order.symbol,
            asset_class=order.asset_class,
            direction=direction,
            entry_order_seq=order.seq,
            opened_at=t,
            first_session=bar.session,
            stop_level=stop,
            target_level=proposal.target_reference,
            horizon_periods=proposal.holding_horizon_periods,
        )
        if self.config.execution.exit_at_horizon:
            trade.horizon_end = expires_at(
                CALENDAR, proposal.timeframe, bar.available_at, proposal.holding_horizon_periods
            )
        self.trades.append(trade)
        order.trade_seq = trade.seq
        proposal.trade_seq = trade.seq
        exit_side = OrderSide.SELL if direction is TradeDirection.LONG else OrderSide.BUY_TO_COVER
        execution = self.config.execution
        trailing = execution.stop_order_type is StopOrderType.TRAILING_STOP
        stop_order = self._new_order(
            OrderRole.STOP,
            order.asset_id,
            exit_side,
            OrderType.TRAILING_STOP if trailing else OrderType.STOP,
            ZERO + 0,
            order.arrival_at,  # bracket: active from the entry's arrival (stop first)
            t,
            "protective stop attached to the entry (bracket)",
            reference_price=stop,
            stop_price=None if trailing else stop,
            trail_percent=execution.trail_percent if trailing else None,
            trade_seq=trade.seq,
            proposal_seq=proposal.seq,
        )
        trade.stop_order_seq = stop_order.seq
        if execution.use_take_profit and proposal.target_reference is not None:
            tp = self._new_order(
                OrderRole.TAKE_PROFIT,
                order.asset_id,
                exit_side,
                OrderType.TAKE_PROFIT,
                ZERO + 0,
                bar.end + self._latency(order.asset_class),  # never in the entry's bar
                t,
                "take-profit attached to the entry (bracket, OCO with the stop)",
                reference_price=proposal.target_reference,
                limit_price=proposal.target_reference,
                adverse_level=stop,
                trade_seq=trade.seq,
                proposal_seq=proposal.seq,
            )
            trade.tp_order_seq = tp.seq
        return trade

    def _latency(self, asset_class: str) -> timedelta:
        return timedelta(seconds=float(self._class_model(asset_class).latency_seconds))

    def _run_exit(self, order: SimOrder, bar: DayBar, model: AssetClassModel, t: datetime) -> None:
        trade = self.trades[order.trade_seq - 1] if order.trade_seq else None
        if trade is None or trade.open_quantity <= 0:
            order.cancel(t, "nothing left to exit")
            return
        if order.role in (OrderRole.STOP, OrderRole.TAKE_PROFIT):
            order.resize(order.filled + trade.open_quantity)
            if order.role is OrderRole.TAKE_PROFIT and trade.stop_order_seq:
                stop_order = self.orders[trade.stop_order_seq - 1]
                order.adverse_level = stop_order.current_stop or stop_order.stop_price
        ev = evaluate(order, bar, model, max_fill=trade.open_quantity)
        if ev is None:
            return
        if (
            ev.outcome
            in (AttemptOutcome.TRIGGERED, AttemptOutcome.FILLED, AttemptOutcome.PARTIALLY_FILLED)
            and order.role is not OrderRole.TAKE_PROFIT
        ):
            # Exiting: the entry's unfilled remainder is cancelled (7B).
            entry = self.orders[trade.entry_order_seq - 1]
            if entry.working:
                entry.cancel(t, "the position is being exited")
                self._entry_final(entry, t)
        if ev.fill is None:
            return
        fill = self._fill(order, ev, bar, t)
        share = trade.cost_open * fill.quantity / trade.open_quantity
        trade.realized_gross += (fill.notional - share) if trade.long else (share - fill.notional)
        trade.cost_open -= share
        trade.open_quantity -= fill.quantity
        trade.exit_quantity += fill.quantity
        trade.exit_value += fill.notional
        trade.exit_costs += fill.commission + fill.fees
        if trade.open_quantity == 0:
            reason = {
                OrderRole.STOP: "TRAILING_STOP"
                if order.order_type is OrderType.TRAILING_STOP
                else "STOP_LOSS",
                OrderRole.TAKE_PROFIT: "TAKE_PROFIT",
                OrderRole.EXIT: "EXPIRATION",
            }[order.role]
            self._close_trade(trade, t, reason)

    def _close_trade(self, trade: Trade, at: datetime, reason: str) -> None:
        trade.exit_reason = reason
        trade.closed_at = at
        trade.status = TradeStatus.CLOSED
        trade.last_session = local_day(at, self.settle)
        trade.cost_open = ZERO
        for order in self._trade_exits(trade):
            order.cancel(at, f"one-cancels-other: position closed ({reason})")
        entry = self.orders[trade.entry_order_seq - 1]
        if entry.working:
            entry.cancel(at, "the position is closed")
            self._entry_final(entry, at)
        due = trade.borrow_accrued - trade.borrow_paid
        if due > 0:
            self._post(
                "accrual_settlement",
                {"payable": "FINANCING", "currency": self.config.currency, "amount": str(due)},
                at,
                f"trade:{trade.seq}:borrow",
            )
            trade.borrow_paid = trade.borrow_accrued

    def _entry_final(self, order: SimOrder, at: datetime) -> None:
        reservation = self.reservations.get(order.proposal_seq or -1)
        if reservation is not None and reservation.active:
            reservation.release()
        if order.proposal_seq is None:
            return
        proposal = self.proposals[order.proposal_seq - 1]
        if order.status is OrderStatus.FILLED:
            proposal.outcome = ProposalOutcome.FILLED
            return
        if order.filled > 0:
            proposal.outcome = ProposalOutcome.PARTIALLY_FILLED
        elif order.status is OrderStatus.CANCELLED:
            proposal.outcome = ProposalOutcome.CANCELLED
        else:
            proposal.outcome = ProposalOutcome.EXPIRED
        if order.filled == 0:
            self._reject(
                at,
                RejectionStage.EXECUTION,
                proposal,
                proposal.strategy_id,
                proposal.symbol,
                proposal.direction.value,
                f"ENTRY_{order.status.value if order.status else 'UNKNOWN'}",
                (order.final_reason or "entry order ended unfilled")
                + (f" (last evaluation {order.last_outcome})" if order.last_outcome else ""),
            )

    # ------------------------------------------------------------------ 3. expiry
    def _expire(self, day: SessionDay, t: datetime) -> None:
        for order in self.orders:
            if (
                order.role is OrderRole.ENTRY
                and order.working
                and order.expires_at is not None
                and order.expires_at <= day.regular_close
            ):
                order.expire(t, f"entry validity ended at {order.expires_at.isoformat()}")
                self._entry_final(order, t)

    # ------------------------------------------------------------------ 4. valuation
    def _marks(self, day: SessionDay, at: datetime) -> dict[uuid.UUID, PositionMark]:
        marks: dict[uuid.UUID, PositionMark] = {}
        for asset_id, pos in self.ledger.state.positions.items():
            if pos.quantity == 0:
                continue
            asset = self.assets[asset_id]
            bar = self.dataset.bar(asset_id, day.day)
            ref = self.dataset.reference(asset_id, at)
            tags = self.dataset.classification(asset_id, at)
            fresh = bar is not None and bar.valid and bar.close > 0
            marks[asset_id] = PositionMark(
                asset_id=asset_id,
                symbol=asset.symbol,
                asset_class=asset.asset_class,
                price=bar.close if fresh and bar is not None else None,
                status="FRESH" if fresh else "MISSING",
                margin_requirement_short=ref.margin_requirement_short,
                sector=tags.sector,
                cluster=tags.cluster,
            )
        return marks

    def _value(self, day: SessionDay, t: datetime) -> None:
        marks = self._marks(day, t)
        price_marks = [
            PriceMark(asset_id=a, price=m.price, currency=self.config.currency)
            for a, m in sorted(marks.items(), key=lambda kv: str(kv[0]))
            if m.price is not None
        ]
        for m in marks.values():
            if m.price is None:
                self._event(
                    t,
                    RunEventKind.DATA_MISSING,
                    m.symbol,
                    f"no valid bar for {day.day}: the position keeps its last valuation",
                )
        try:
            self.ledger.value(t, price_marks)
        except LedgerError as exc:
            raise BacktestError("valuation", str(exc), t) from exc
        for trade in self.trades:
            if trade.open_quantity > 0:
                mark = marks.get(trade.asset_id)
                if mark is not None and mark.price is not None:
                    trade.mark_price = mark.price
                    trade.last_session = day.day
                if not trade.long:
                    self._accrue_borrow(trade, day, t)

    def _accrue_borrow(self, trade: Trade, day: SessionDay, t: datetime) -> None:
        rules = self._class_model(trade.asset_class)
        if rules.borrow_rate_annual <= 0 or trade.mark_price is None:
            return
        start = trade.borrow_through or trade.first_session
        days = (day.day - start).days
        if days <= 0:
            if trade.borrow_through is None:
                trade.borrow_through = day.day
            return
        value = trade.open_quantity * trade.mark_price
        amount = cost_round(
            value * rules.borrow_rate_annual * days / rules.day_count,
            currency(self.config.currency).minor_units,
        )
        trade.borrow_through = day.day
        if amount <= 0:
            return
        self._post(
            "financing_cost",
            {
                "kind": "BORROW",
                "currency": self.config.currency,
                "amount": str(amount),
                "accrue": True,
                "asset_id": str(trade.asset_id),
            },
            t,
            f"trade:{trade.seq}:borrow:{day.day}",
        )
        trade.borrow_accrued += amount

    # ------------------------------------------------------------------ 5. snapshot / risk
    def _active_reservations(self) -> list[ReservedExposure]:
        return [
            r.exposure()
            for _, r in sorted(self.reservations.items())
            if r.active and r.remaining_quantity > 0
        ]

    def _snapshot(self, day: SessionDay, t: datetime) -> Any:
        marks = self._marks(day, t)
        reserved = self._active_reservations()
        evaluated = self.book.evaluate(self.ledger, marks, reserved, t)
        view = evaluated.view
        totals = self.ledger.figures()
        complete = view.mark_status.value == "COMPLETE"
        if complete:
            self.book.observe(t, view.equity, view.contributed_capital)
        peak = max([s.equity for s in self.snapshots] + [view.equity, self.config.initial_capital])
        self.snapshots.append(
            Snapshot(
                session=day.day,
                at=t,
                equity=view.equity,
                cash=view.cash,
                contributed_capital=view.contributed_capital,
                long_exposure=view.long_exposure,
                short_exposure=view.short_exposure,
                realized_pnl=totals.realized_pnl,
                unrealized_pnl=totals.unrealized_pnl,
                commissions=totals.commissions,
                fees=totals.fees,
                borrow_costs=totals.borrow_costs,
                open_positions=len(view.positions),
                open_orders=sum(1 for o in self.orders if o.working),
                reserved_exposure=view.pending_exposure,
                peak_equity=peak,
                drawdown=peak - view.equity,
                mark_status=view.mark_status.value,
                active_breakers=[b.value for b in view.active_breakers],
            )
        )
        return evaluated

    # ------------------------------------------------------------------ 6. horizon exits
    def _horizon_exits(self, day: SessionDay, t: datetime) -> None:
        for trade in self.trades:
            if trade.open_quantity <= 0 or trade.horizon_end is None or trade.horizon_end > t:
                continue
            if any(self.orders[s - 1].working for s in trade.exit_order_seqs):
                continue
            if trade.tp_order_seq:
                tp = self.orders[trade.tp_order_seq - 1]
                tp.cancel(t, "holding horizon reached: the take-profit is replaced by an exit")
            side = OrderSide.SELL if trade.long else OrderSide.BUY_TO_COVER
            order = self._new_order(
                OrderRole.EXIT,
                trade.asset_id,
                side,
                OrderType.MARKET,
                trade.open_quantity,
                t + self._latency(trade.asset_class),
                t,
                f"holding horizon of {trade.horizon_periods} {trade.timeframe} period(s) ended",
                reference_price=trade.mark_price or trade.stop_level,
                trade_seq=trade.seq,
                proposal_seq=trade.proposal_seq,
            )
            trade.exit_order_seqs.append(order.seq)

    # ------------------------------------------------------------------ 7. decisions
    def _universe(self, day: SessionDay, t: datetime) -> list[uuid.UUID]:
        if self.config.universe.mode is UniverseMode.EXPLICIT:
            current = set(self.assets)
        else:
            assert self.config.universe.layer is not None  # noqa: S101  # validated
            members = members_at(self.session, self.config.universe.layer, t, known_at=t)
            current = {a for a in members if a in self.assets}
            for added in sorted(current - self.universe, key=lambda a: self.assets[a].symbol):
                self._event(
                    t,
                    RunEventKind.UNIVERSE_ADDED,
                    self.assets[added].symbol,
                    f"member of {self.config.universe.layer.value} at {t.isoformat()}",
                )
            for gone in sorted(self.universe - current, key=lambda a: self.assets[a].symbol):
                self._event(
                    t,
                    RunEventKind.UNIVERSE_REMOVED,
                    self.assets[gone].symbol,
                    f"no longer a member of {self.config.universe.layer.value}",
                )
        self.universe = current
        return sorted(current, key=lambda a: self.assets[a].symbol)

    def _decide(self, day: SessionDay, t: datetime) -> None:
        timeframes = [DAILY]
        if next_trading_day(CALENDAR, day.day).day.isocalendar()[:2] != day.day.isocalendar()[:2]:
            timeframes.append(WEEKLY)
        universe = self._universe(day, t)
        evaluated = self.book.evaluate(
            self.ledger, self._marks(day, t), self._active_reservations(), t
        )
        for record in self.book.simulated_operator(evaluated.conditions, t, day.regular_open):
            self._event(
                t,
                RunEventKind.BREAKER_RESET,
                None,
                record.reason,
                {"breaker": record.breaker.value, "actor": record.actor},
            )
        view = evaluated.view.model_copy(update={"active_breakers": self.book.active()})
        drafts: list[tuple[tuple[Any, ...], _PreparedStrategy, uuid.UUID, Any, Any]] = []
        for timeframe in timeframes:
            for prepared in self._prepared:
                if timeframe not in prepared.resolved.timeframes:
                    continue
                for asset_id in universe:
                    result = self._evaluate(prepared, asset_id, timeframe, day, t)
                    if result is None:
                        continue
                    for spec in sorted(result[0], key=lambda s: s.direction.value):
                        drafts.append(
                            (
                                (
                                    timeframe,
                                    prepared.resolved.strategy_id,
                                    self.assets[asset_id].symbol,
                                    spec.direction.value,
                                ),
                                prepared,
                                asset_id,
                                spec,
                                result[1],
                            )
                        )
        for _key, prepared, asset_id, spec, feature_sha in sorted(drafts, key=lambda d: d[0]):
            view = self._propose(prepared, asset_id, spec, feature_sha, day, t, view)

    def _evaluate(
        self,
        prepared: _PreparedStrategy,
        asset_id: uuid.UUID,
        timeframe: str,
        day: SessionDay,
        t: datetime,
    ) -> tuple[list[Any], str] | None:
        asset = self.assets[asset_id]
        sid = prepared.resolved.strategy_id
        bar = self.dataset.bar(asset_id, day.day)
        if bar is None or not bar.valid:
            self.evaluations[f"{sid}|{timeframe}|NO_SESSION_BAR"] += 1
            return None
        ref = self.dataset.reference(asset_id, t)
        eligibility = Eligibility(
            known=ref.version is not None,
            provider=self.config.data.provider,
            provider_status=ref.status,
            tradable=ref.tradable,
            shortable=ref.shortable,
            easy_to_borrow=ref.easy_to_borrow,
            reference_version=ref.version,
            observed_at=ref.observed_at,
        )
        view = AssetView(
            asset_id, asset.symbol, AssetClass(asset.asset_class), asset.exchange_code, eligibility
        )
        if ineligibility(prepared.strategy, prepared.params, view) is not None:
            self.evaluations[f"{sid}|{timeframe}|{SignalState.INELIGIBLE_ASSET.value}"] += 1
            return None
        try:
            inputs = compute_features(
                self.session,
                prepared.features[timeframe],
                asset_id=asset_id,
                timeframe=timeframe,
                data_cutoff=t,
                known_at=self.config.known_at,
                basis=KnowledgeBasis.RECONSTRUCTED,
                series=self.series,
                bar_source=self.bars,
                news_delay=self.news_delay,
            )
        except Exception as exc:  # recorded, the run continues (as the live engine does)
            self.evaluations[f"{sid}|{timeframe}|FAILED"] += 1
            self._issue(t, "INPUTS", f"{sid} {asset.symbol} {timeframe}: {exc}")
            return None
        try:
            decision = decide(
                prepared.strategy,
                prepared.params,
                prepared.features[timeframe],
                view,
                inputs,
                timeframe=timeframe,
                mode=EvaluationMode.HISTORICAL,
                basis=KnowledgeBasis.RECONSTRUCTED,
                data_cutoff=t,
                trigger=TriggerKind.BAR_CLOSE,
                trigger_ref=f"{timeframe}:{day.day.isoformat()}",
                registry=self.registry,
                calendar=CALENDAR,
                snapshot_ids=[],
            )
        except EvaluationStageError as exc:
            self.evaluations[f"{sid}|{timeframe}|FAILED"] += 1
            self._issue(t, exc.stage.value, f"{sid} {asset.symbol} {timeframe}: {exc.error}")
            return None
        self.evaluations[f"{sid}|{timeframe}|{decision.state.value}"] += 1
        for note in decision.notes:
            if "left out" in note:
                self._reject(
                    t,
                    RejectionStage.STRATEGY_VALIDATION,
                    None,
                    sid,
                    asset.symbol,
                    note.split(" ", 1)[0],
                    "DRAFT_LEFT_OUT",
                    note,
                )
        return decision.specs, inputs.content_sha256()

    def _liquidity_at(
        self, asset_id: uuid.UUID, day: SessionDay, t: datetime
    ) -> tuple[Decimal | None, str | None]:
        key = (asset_id, day.day)
        if key in self._liquidity:
            return self._liquidity[key]
        sessions = completed_sessions(CALENDAR, t, LIQUIDITY_SESSIONS, self.settle)
        bars = self.bars(
            self.session,
            asset_id,
            sessions,
            data_cutoff=t,
            known_at=self.config.known_at,
            series=self.series,
        )
        found: tuple[Decimal | None, str | None] = (None, None)
        if bars:
            measured = discovery_metrics.compute(bars, [asset_id], sessions)
            m = measured.get(asset_id)
            value = None if m is None else m.values.get("median_dollar_volume")
            if value is not None and value.status is DiscoveryMetricStatus.OK and value.value:
                found = (
                    Decimal(str(value.value)).quantize(Decimal("0.01")),
                    f"median_dollar_volume ({value.window}) recomputed point in time",
                )
        self._liquidity[key] = found
        return found

    def _propose(
        self,
        prepared: _PreparedStrategy,
        asset_id: uuid.UUID,
        spec: Any,
        feature_sha: str,
        day: SessionDay,
        t: datetime,
        view: PortfolioView,
    ) -> PortfolioView:
        asset = self.assets[asset_id]
        rs = prepared.resolved
        record = ProposalRecord(
            seq=len(self.proposals) + 1,
            decided_at=t,
            session=day.day,
            strategy_id=rs.strategy_id,
            strategy_version=rs.version,
            timeframe=spec.timeframe,
            asset_id=asset_id,
            symbol=asset.symbol,
            direction=spec.direction,
            reference_entry=spec.reference_entry,
            invalidation_level=spec.invalidation_level,
            target_reference=spec.target_reference,
            signal_strength=spec.signal_strength,
            holding_horizon_periods=spec.holding_horizon_periods,
            expires_at=spec.expires_at,
            reasoning=spec.reasoning,
            evidence=[dict(e) for e in spec.evidence],
            levels=dict(spec.levels),
            feature_sha256=feature_sha,
        )
        self.proposals.append(record)
        direction = TradeDirection(spec.direction.value)
        proposal = Proposal(
            source=ProposalSource.STRATEGY_PROPOSAL,
            asset_id=asset_id,
            direction=direction,
            entry=spec.reference_entry,
            stop=spec.invalidation_level,
            target=spec.target_reference,
            expected_holding_days=min(spec.holding_horizon_periods, 365)
            if spec.timeframe == DAILY
            else None,
            expires_at=spec.expires_at,
            strategy_proposal_id=uuid.uuid5(NAMESPACE, f"proposal:{record.seq}"),
        )
        bar = self.dataset.bar(asset_id, day.day)
        assert bar is not None  # noqa: S101  # evaluated assets traded this session
        ref = self.dataset.reference(asset_id, t)
        tags = self.dataset.classification(asset_id, t)
        spec_row = self.dataset.instrument_spec(asset_id, t)
        increment = spec_row.quantity_increment if spec_row else ref.quantity_increment
        minimum = spec_row.min_quantity if spec_row else ref.min_quantity
        instrument = InstrumentData(
            asset_id=asset_id,
            symbol=asset.symbol,
            asset_class=asset.asset_class,
            asset_status="ACTIVE" if bar.valid else "INACTIVE",
            quote_currency=asset.quote_currency,
            currency_supported=asset.quote_currency == self.config.currency,
            quantity_increment=increment,
            min_quantity=minimum,
            spec_id=None if spec_row is None else spec_row.id,
            provider_tick_size=ref.price_increment,
            reference_known=ref.version is not None,
            tradable=ref.tradable,
            shortable=ref.shortable,
            easy_to_borrow=ref.easy_to_borrow,
            margin_requirement_short=None
            if ref.margin_requirement_short is None or ref.margin_requirement_short < 0
            else ref.margin_requirement_short / Decimal(100),
            reference_version=ref.version,
            reference_observed_at=ref.observed_at,
            sector=tags.sector,
            cluster=tags.cluster,
            classification_id=tags.record_id,
        )
        adv, adv_metric = self._liquidity_at(asset_id, day, t)
        market = MarketData(
            price=bar.close,
            price_status="FRESH",
            price_source="BAR_CLOSE",
            price_observed_at=bar.start,
            price_provenance={
                "table": "market_bars",
                "id": bar.bar_id,
                "session": str(bar.session),
            },
            adv_quote=adv,
            adv_metric=adv_metric,
            adv_as_of=t if adv is not None else None,
            fx_rate=ONE,
            fx_status="FRESH",
        )
        held = list(
            dict.fromkeys(
                [p.asset_id for p in view.positions] + [r.asset_id for r in view.reservations]
            )
        )
        prefs = self.risk_config.preferences
        correlations = risk_data.correlations(
            self.session, asset_id, held, t, timedelta(days=prefs.correlation_max_age_days)
        )
        outcome = assess(
            AssessmentInputs(
                assessed_at=t,
                proposal=proposal,
                instrument=instrument,
                market=market,
                portfolio=view,
                correlations=correlations,
                config=self.risk_config,
            )
        )
        record.risk_result = outcome.result.value
        record.risk_reasons = [
            {
                "code": c.code,
                "outcome": c.outcome.value,
                "reason_code": None if c.reason_code is None else c.reason_code.value,
                "message": c.message,
            }
            for c in outcome.checks
            if c.outcome.value not in ("PASS",)
        ]
        record.permitted_quantity = outcome.permitted_quantity
        record.binding_constraint = outcome.binding_constraint
        record.risk_budget = outcome.risk_budget
        record.potential_loss = outcome.potential_loss
        record.estimated_costs = outcome.estimated_costs
        if outcome.result is not AssessmentResult.PASS_FOR_RESERVATION:
            record.outcome = ProposalOutcome.REJECTED
            for reason in outcome.reasons:
                self._reject(
                    t,
                    RejectionStage.RISK,
                    record,
                    rs.strategy_id,
                    asset.symbol,
                    spec.direction.value,
                    reason.reason_code.value if reason.reason_code else reason.code,
                    reason.message,
                )
            if not outcome.reasons:
                self._reject(
                    t,
                    RejectionStage.RISK,
                    record,
                    rs.strategy_id,
                    asset.symbol,
                    spec.direction.value,
                    outcome.result.value,
                    outcome.result.value,
                )
            return view
        reserved = outcome.calculations["reservation"]
        quantity = Decimal(reserved["quantity"])
        worst = Decimal(reserved["worst_entry_price"])
        stop = Decimal(outcome.calculations["stop"])
        record.reservation = {**reserved, "stop": str(stop)}
        reservation = Reservation(
            seq=record.seq,
            asset_id=asset_id,
            symbol=asset.symbol,
            asset_class=asset.asset_class,
            direction=direction,
            sector=tags.sector,
            cluster=tags.cluster,
            quantity=quantity,
            remaining_quantity=quantity,
            remaining={k: Decimal(reserved[k]) for k in ("risk", "exposure", "capital", "margin")},
        )
        self.reservations[record.seq] = reservation
        if self._instrument(asset_id, t) is None:  # pragma: no cover - risk checks the lot
            raise BacktestError("execution", f"{asset.symbol}: no lot size for an approved order")
        execution = self.config.execution
        side = OrderSide.BUY if direction is TradeDirection.LONG else OrderSide.SELL_SHORT
        kind = execution.entry_order_type
        order_type = OrderType(kind.value)
        order = self._new_order(
            OrderRole.ENTRY,
            asset_id,
            side,
            order_type,
            quantity,
            t + self._latency(asset.asset_class),
            t,
            f"entry for proposal {record.seq} ({rs.strategy_id} {spec.timeframe})",
            reference_price=worst,
            limit_price=worst
            if kind in (EntryOrderType.LIMIT, EntryOrderType.STOP_LIMIT)
            else None,
            stop_price=spec.reference_entry.quantize(PRICE_PLACES)
            if kind in (EntryOrderType.STOP, EntryOrderType.STOP_LIMIT)
            else None,
            protection_price=worst,
            expires_at=spec.expires_at,
            proposal_seq=record.seq,
        )
        record.order_seq = order.seq
        return view.model_copy(
            update={
                "reservations": [*view.reservations, reservation.exposure()],
                "pending_exposure": view.pending_exposure + reservation.remaining["exposure"],
            }
        )

    # ------------------------------------------------------------------ end of run
    def _finish(self, last: SessionDay) -> None:
        t = last.regular_close + self.settle
        for order in self.orders:
            if order.role is OrderRole.ENTRY and order.working and order.proposal_seq:
                proposal = self.proposals[order.proposal_seq - 1]
                proposal.outcome = (
                    ProposalOutcome.PARTIALLY_FILLED
                    if order.filled > 0
                    else ProposalOutcome.OPEN_AT_END
                )
        for trade in self.trades:
            if trade.open_quantity > 0:
                trade.status = TradeStatus.OPEN_AT_END
                trade.notes.append("open when the run ended: valued at the last close, not exited")
        self._reconcile(t)
        # Trade-level and ledger realized P/L agree for every asset that is flat.
        for asset_id in {tr.asset_id for tr in self.trades}:
            pos = self.ledger.state.positions.get(asset_id)
            if pos is not None and pos.quantity != 0:
                continue
            trades = [tr for tr in self.trades if tr.asset_id == asset_id]
            ledger_realized = sum(
                (
                    Decimal(m["realized_pnl"])
                    for e in self.ledger.journal
                    for m in e.movements
                    if m["asset_id"] == str(asset_id)
                ),
                ZERO,
            )
            trade_realized = sum((tr.realized_gross for tr in trades), ZERO)
            if abs(ledger_realized - trade_realized) > Decimal("0.05") * max(len(trades), 1):
                raise BacktestError(
                    "reconciliation",
                    f"{self.assets[asset_id].symbol}: trade P/L {trade_realized} differs from "
                    f"the ledger's realized P/L {ledger_realized}",
                    t,
                )


def previous_session(day: date) -> date:
    return previous_trading_day(CALENDAR, day).day


def local_day(at: datetime, settle: timedelta) -> date:
    return local_date(CALENDAR, at - settle)


__all__ = [
    "BacktestEngine",
    "BacktestError",
    "BacktestResult",
    "Checkpoint",
    "ProposalRecord",
    "Rejection",
    "RunEvent",
    "RunIssue",
    "Snapshot",
    "Trade",
    "fill_json",
    "order_json",
]
