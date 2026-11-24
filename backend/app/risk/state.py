"""Portfolio risk state from the ledger, marked to fresh prices (spec 13, 14.4, 14.5).

``evaluate`` reads the account's ledger (Phase 6A ``load_state``) and marks it to market at
``as_of`` **in memory**: it builds the Phase 6A valuation entry for every position and
foreign currency with fresh inputs, applies it to a copy of the ledger state and derives
the figures from the accounting equation. Nothing is posted: the books change only at
valuation checkpoints (D-133). If any input is stale or missing the mark is STALE or
INCOMPLETE and the figures cannot be trusted for risk (the DATA_QUALITY breaker).

From the marked state it derives exposure (long, short, gross, net; per asset, asset class,
sector, correlation cluster), short collateral (margin used), available capital, the daily
and weekly loss against their stored baselines, peak equity and drawdown, and the loss
allowance. With ``persist`` it locks the account row, fixes period baselines, records
breaker transitions and writes an exposure snapshot, all in the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.accounting import chart
from app.accounting.config import AccountingSettings
from app.accounting.enums import EntryType, FxRateSource, PriceStatus
from app.accounting.money import ZERO, money_context
from app.accounting.posting import last_sequence, load_state
from app.accounting.reference import (
    TEST_SOURCES,
    VALUATION_SOURCES,
    ReferenceDataError,
    functional_rate,
    instrument_info,
)
from app.accounting.state import LedgerState
from app.accounting.templates import (
    InstrumentInfo,
    PriceMark,
    RateQuote,
    TemplateError,
    ValuationParams,
    valuation,
)
from app.core.logging import get_logger
from app.database.models import (
    ActorType,
    Asset,
    AuditLog,
    JournalEntryRecord,
    PortfolioAccount,
    PortfolioSnapshotRecord,
    ReconciliationRunRecord,
)
from app.database.models.portfolio_account import PortfolioAccountPurpose
from app.database.models.risk import (
    RiskCircuitBreakerEventRecord,
    RiskEventRecord,
    RiskExposureSnapshotRecord,
    RiskPeriodBaselineRecord,
)
from app.portfolio.figures import PortfolioFigures, figures
from app.portfolio.pricing import valuation_price
from app.risk import breakers as rules
from app.risk.config import HardLimits, RiskConfiguration
from app.risk.data import classification_at, reference_at, short_margin_fraction
from app.risk.enums import (
    ActorKind,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    MarkStatus,
    PeriodKind,
    RiskEventType,
    Severity,
    SnapshotTrigger,
    TradeDirection,
)
from app.risk.models import HeldPosition, PortfolioView
from app.risk.periods import (
    TIMEZONE_POLICY,
    Baseline,
    Observation,
    choose_baseline,
    loss_state,
    peak_equity,
    period,
)

logger = get_logger(__name__)
ENGINE_VERSION = "6B.1"
MONITOR_ACTOR = "system:risk-monitor"


class _MarkContext:
    """TemplateContext for an in-memory valuation (fresh rates and specs at ``at``)."""

    def __init__(
        self,
        session: Session,
        functional: str,
        at: datetime,
        max_age: timedelta,
        sources: tuple[FxRateSource, ...],
    ) -> None:
        self._session = session
        self._functional = functional
        self._at = at
        self._max_age = max_age
        self._sources = sources

    @property
    def functional_currency(self) -> str:
        return self._functional

    def spot_rate(self, currency: str) -> RateQuote:
        found = functional_rate(
            self._session,
            currency,
            self._functional,
            self._at,
            max_age=self._max_age,
            sources=self._sources,
        )
        if found.status is not PriceStatus.FRESH or found.quote is None:
            raise TemplateError(f"no fresh {currency}/{self._functional} rate")
        return found.quote

    def instrument(self, asset_id: uuid.UUID) -> InstrumentInfo:
        return instrument_info(self._session, asset_id, self._at)


@dataclass
class MarkedPosition:
    asset: Asset
    direction: TradeDirection
    quantity: Decimal
    currency: str
    exposure: Decimal
    price_status: str
    price: Decimal | None
    price_source: str | None
    price_observed_at: datetime | None
    margin_rate: Decimal
    margin_required: Decimal
    sector: str | None
    cluster: str | None


@dataclass
class EvaluatedState:
    account: PortfolioAccount
    as_of: datetime
    view: PortfolioView
    figures: PortfolioFigures
    positions: list[MarkedPosition]
    conditions: list[rules.Condition]
    statuses: dict[BreakerType, BreakerStatus]
    baselines: dict[PeriodKind, Baseline | None]
    snapshot_id: uuid.UUID | None = None
    transitions: list[rules.Transition] = field(default_factory=list)
    reconciliation_run_id: uuid.UUID | None = None


def lock_account(session: Session, account_id: uuid.UUID) -> PortfolioAccount:
    account = session.scalar(
        select(PortfolioAccount).where(PortfolioAccount.id == account_id).with_for_update()
    )
    if account is None:
        raise LookupError(f"portfolio account {account_id} does not exist")
    return account


def rate_sources(account: PortfolioAccount) -> tuple[FxRateSource, ...]:
    return (
        TEST_SOURCES
        if account.purpose is PortfolioAccountPurpose.CONTROLLED_TEST
        else VALUATION_SOURCES
    )


def breaker_statuses(session: Session, account_id: uuid.UUID) -> dict[BreakerType, BreakerStatus]:
    e = RiskCircuitBreakerEventRecord
    rows = session.execute(
        select(e.breaker_type, e.new_status)
        .where(e.portfolio_account_id == account_id)
        .ext(distinct_on(e.breaker_type))
        .order_by(e.breaker_type, e.sequence.desc())
    ).all()
    found = {BreakerType(t): BreakerStatus(s) for t, s in rows}
    return {b: found.get(b, BreakerStatus.CLEAR) for b in BreakerType}


def _last_breaker_sequence(session: Session, account_id: uuid.UUID, breaker: BreakerType) -> int:
    e = RiskCircuitBreakerEventRecord
    found = session.scalar(
        select(e.sequence)
        .where(e.portfolio_account_id == account_id, e.breaker_type == breaker)
        .order_by(e.sequence.desc())
        .limit(1)
    )
    return found or 0


def record_breaker_event(
    session: Session,
    account_id: uuid.UUID,
    breaker: BreakerType,
    event: BreakerEvent,
    *,
    at: datetime,
    actor: str,
    actor_kind: ActorKind,
    reason: str,
    measurements: dict[str, Any],
    config_version_id: str | None,
    snapshot_id: uuid.UUID | None = None,
) -> RiskCircuitBreakerEventRecord:
    """Append a trip or reset (the database checks sequence and transition)."""
    previous, new = (
        (BreakerStatus.CLEAR, BreakerStatus.TRIPPED)
        if event is BreakerEvent.TRIPPED
        else (BreakerStatus.TRIPPED, BreakerStatus.CLEAR)
    )
    record = RiskCircuitBreakerEventRecord(
        portfolio_account_id=account_id,
        breaker_type=breaker,
        sequence=_last_breaker_sequence(session, account_id, breaker) + 1,
        event=event,
        previous_status=previous,
        new_status=new,
        occurred_at=at,
        actor_kind=actor_kind,
        actor=actor,
        reason=reason,
        measurements=measurements,
        response=rules.RESPONSE | {"recovery": rules.RECOVERY[breaker].value},
        config_version_id=None if config_version_id is None else uuid.UUID(config_version_id),
        exposure_snapshot_id=snapshot_id,
    )
    session.add(record)
    session.flush()
    tripped = event is BreakerEvent.TRIPPED
    session.add(
        RiskEventRecord(
            portfolio_account_id=account_id,
            event_type=RiskEventType.BREAKER_TRIPPED if tripped else RiskEventType.BREAKER_RESET,
            severity=Severity.CRITICAL if tripped else Severity.WARNING,
            occurred_at=at,
            actor=actor,
            subject_kind="risk_circuit_breaker_event",
            subject_id=str(record.id),
            summary=f"{breaker.value} {event.value}: {reason}",
            details={"measurements": measurements},
        )
    )
    session.add(
        AuditLog(
            occurred_at=at,
            actor_type=ActorType.USER if actor_kind is ActorKind.OPERATOR else ActorType.SUBSYSTEM,
            actor_id=actor,
            action=f"risk.breaker_{event.value.lower()}",
            entity_type="risk_circuit_breaker",
            entity_id=f"{account_id}:{breaker.value}",
            reason=reason,
            previous_state={"status": previous.value},
            new_state={"status": new.value},
            details={"sequence": record.sequence, "measurements": measurements},
        )
    )
    session.flush()
    log = logger.error if tripped else logger.warning
    log(
        f"risk.breaker_{event.value.lower()}",
        account_id=str(account_id),
        breaker=breaker.value,
        actor=actor,
    )
    return record


# ------------------------------------------------------------------ marking
def _mark(
    session: Session,
    account: PortfolioAccount,
    state: LedgerState,
    as_of: datetime,
    accounting: AccountingSettings,
) -> tuple[LedgerState, list[dict[str, Any]], dict[uuid.UUID, Any]]:
    functional = account.base_currency
    sources = rate_sources(account)
    max_age = timedelta(hours=accounting.fx_rate_max_age_hours)
    ctx = _MarkContext(session, functional, as_of, max_age, sources)
    issues: list[dict[str, Any]] = []
    marks: list[PriceMark] = []
    prices: dict[uuid.UUID, Any] = {}
    for asset_id, pos in sorted(state.positions.items(), key=lambda item: str(item[0])):
        if pos.quantity == ZERO:
            continue
        asset = session.get(Asset, asset_id)
        if asset is None:  # pragma: no cover - foreign keys prevent it
            raise LookupError(f"asset {asset_id} disappeared")
        price = valuation_price(session, asset, as_of, accounting)
        prices[asset_id] = price
        if price.status is not PriceStatus.FRESH or price.observation is None:
            issues.append(
                {
                    "kind": f"PRICE_{price.status.value}",
                    "asset_id": str(asset_id),
                    "symbol": asset.symbol,
                    "detail": price.reason,
                }
            )
            continue
        if pos.currency != functional:
            found = functional_rate(
                session, pos.currency, functional, as_of, max_age=max_age, sources=sources
            )
            if found.status is not PriceStatus.FRESH:
                issues.append(
                    {
                        "kind": f"RATE_{found.status.value}",
                        "asset_id": str(asset_id),
                        "currency": pos.currency,
                        "detail": found.reason,
                    }
                )
                continue
        try:
            multiplier = instrument_info(session, asset_id, as_of).multiplier
        except ReferenceDataError as exc:
            issues.append({"kind": "SPEC_MISSING", "asset_id": str(asset_id), "detail": str(exc)})
            continue
        marks.append(
            PriceMark(
                asset_id=asset_id,
                price=price.observation.price,
                currency=pos.currency,
                multiplier=multiplier,
                provenance=price.observation.provenance,
            )
        )
    translate: list[str] = []
    for cur in sorted(state.currencies()):
        if cur == functional:
            continue
        net, _ = state.net_position(cur)
        if net == ZERO and state.translation(cur) == ZERO:
            continue
        found = functional_rate(session, cur, functional, as_of, max_age=max_age, sources=sources)
        if found.status is PriceStatus.FRESH:
            translate.append(cur)
        else:
            issues.append(
                {"kind": f"RATE_{found.status.value}", "currency": cur, "detail": found.reason}
            )
    marked = state.copy()
    if marks or translate:
        draft = valuation(
            state,
            ctx,
            ValuationParams(as_of=as_of, marks=tuple(marks), translate=tuple(translate)),
        )
        for line in draft.lines:
            marked.apply_line(line)
    return marked, issues, prices


def _position_exposure(state: LedgerState, asset_id: uuid.UUID, currency: str) -> Decimal:
    pos = state.positions[asset_id]
    if pos.quantity > ZERO:
        return (
            state.balance(chart.LONG_COST, currency, asset_id).functional
            + state.balance(chart.LONG_VALUATION, currency, asset_id).functional
        )
    return -(
        state.balance(chart.SHORT_PROCEEDS, currency, asset_id).functional
        + state.balance(chart.SHORT_VALUATION, currency, asset_id).functional
    )


# ------------------------------------------------------------------ observations
def observations(session: Session, account_id: uuid.UUID) -> list[Observation]:
    """Every COMPLETE equity observation of the account (risk snapshots and checkpoints)."""
    rs = RiskExposureSnapshotRecord
    ps = PortfolioSnapshotRecord
    found = [
        Observation(r.as_of, r.equity, r.contributed_capital, "RISK_SNAPSHOT", str(r.id))
        for r in session.execute(
            select(rs.as_of, rs.equity, rs.contributed_capital, rs.id).where(
                rs.portfolio_account_id == account_id, rs.mark_status == MarkStatus.COMPLETE
            )
        )
    ]
    found += [
        Observation(r.as_of, r.equity, r.contributed_capital, "VALUATION_CHECKPOINT", str(r.id))
        for r in session.execute(
            select(ps.as_of, ps.equity, ps.contributed_capital, ps.id).where(
                ps.portfolio_account_id == account_id, ps.valuation_status == "COMPLETE"
            )
        )
    ]
    return found


def _funding(session: Session, account_id: uuid.UUID) -> tuple[datetime | None, Decimal | None]:
    entry = session.scalar(
        select(JournalEntryRecord).where(
            JournalEntryRecord.portfolio_account_id == account_id,
            JournalEntryRecord.entry_type == EntryType.OPENING_FUNDING,
        )
    )
    if entry is None:
        return None, None
    amount = entry.payload.get("amount") if isinstance(entry.payload, dict) else None
    return entry.effective_at, None if amount is None else Decimal(str(amount))


def _baseline(
    session: Session,
    account_id: uuid.UUID,
    kind: PeriodKind,
    as_of: datetime,
    obs: list[Observation],
    current: Observation | None,
    funded: tuple[datetime | None, Decimal | None],
    *,
    persist: bool,
) -> Baseline | None:
    p = period(kind, as_of)
    stored = session.scalar(
        select(RiskPeriodBaselineRecord).where(
            RiskPeriodBaselineRecord.portfolio_account_id == account_id,
            RiskPeriodBaselineRecord.period_kind == kind,
            RiskPeriodBaselineRecord.period_start == p.start,
        )
    )
    if stored is not None:
        return Baseline(
            kind,
            stored.period_label,
            stored.period_start,
            stored.baseline_equity,
            stored.baseline_contributed_capital,
            stored.basis,
            stored.observed_at,
            stored.source_kind,
            stored.source_id,
        )
    eligible = [o for o in obs if o.observed_at <= as_of]
    chosen = choose_baseline(p, eligible, funded_at=funded[0], funding=funded[1], current=current)
    if chosen is None:
        return None
    if chosen.source_kind == "CURRENT":
        # This evaluation itself is the first observation of the period; its snapshot is
        # written in the same transaction at the same time.
        chosen = Baseline(
            chosen.kind,
            chosen.label,
            chosen.period_start,
            chosen.equity,
            chosen.contributed_capital,
            chosen.basis,
            chosen.observed_at,
            "CURRENT_EVALUATION",
            chosen.observed_at.isoformat()[:64],
        )
    if persist:
        session.add(
            RiskPeriodBaselineRecord(
                portfolio_account_id=account_id,
                period_kind=kind,
                period_label=chosen.label,
                period_start=chosen.period_start,
                period_end=p.end,
                baseline_equity=chosen.equity,
                baseline_contributed_capital=chosen.contributed_capital,
                basis=chosen.basis,
                observed_at=chosen.observed_at,
                source_kind=chosen.source_kind,
                source_id=chosen.source_id,
                timezone_policy=TIMEZONE_POLICY,
            )
        )
        session.flush()
    return chosen


def _reconciliation(
    session: Session, account_id: uuid.UUID, sequence: int
) -> tuple[str | None, bool, uuid.UUID | None]:
    run = session.scalar(
        select(ReconciliationRunRecord)
        .where(ReconciliationRunRecord.portfolio_account_id == account_id)
        .order_by(ReconciliationRunRecord.finished_at.desc())
        .limit(1)
    )
    if run is None:
        return None, False, None
    return run.status.value, run.entry_sequence == sequence, run.id


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


# ------------------------------------------------------------------ evaluation
def evaluate(
    session: Session,
    account: PortfolioAccount,
    as_of: datetime,
    config: RiskConfiguration | None,
    accounting: AccountingSettings,
    *,
    persist: bool,
    trigger: SnapshotTrigger = SnapshotTrigger.MONITOR,
    actor: str = MONITOR_ACTOR,
) -> EvaluatedState:
    """The account's risk state at ``as_of``. With ``persist`` the caller must hold the
    account lock; breaker transitions, baselines and a snapshot are written."""
    functional = account.base_currency
    limits = config.hard_limits if config is not None else HardLimits()
    ledger = load_state(session, account.id, functional)
    sequence = last_sequence(session, account.id)
    marked, issues, prices = _mark(session, account, ledger, as_of, accounting)
    totals = figures(marked)
    if issues:
        mark_status = (
            MarkStatus.INCOMPLETE
            if any(str(i["kind"]).endswith("MISSING") for i in issues)
            else MarkStatus.STALE
        )
    else:
        mark_status = MarkStatus.COMPLETE

    positions: list[MarkedPosition] = []
    for asset_id, pos in sorted(marked.positions.items(), key=lambda item: str(item[0])):
        if pos.quantity == ZERO:
            continue
        asset = session.get(Asset, asset_id)
        assert asset is not None  # noqa: S101  # foreign keys
        exposure = abs(_position_exposure(marked, asset_id, pos.currency))
        short = pos.quantity < ZERO
        margin_rate = ZERO
        if short:
            provider = short_margin_fraction(reference_at(session, asset_id, as_of))
            margin_rate = max(limits.min_short_margin_rate, provider or ZERO)
        tags = classification_at(session, asset_id, as_of)
        price = prices.get(asset_id)
        obs = price.observation if price is not None else None
        with money_context():
            margin = margin_rate * exposure
        positions.append(
            MarkedPosition(
                asset=asset,
                direction=TradeDirection.SHORT if short else TradeDirection.LONG,
                quantity=pos.quantity,
                currency=pos.currency,
                exposure=exposure,
                price_status=price.status.value if price is not None else "MISSING",
                price=None if obs is None else obs.price,
                price_source=None if obs is None else obs.source,
                price_observed_at=None if obs is None else obs.observed_at,
                margin_rate=margin_rate,
                margin_required=margin,
                sector=tags.sector,
                cluster=tags.cluster,
            )
        )
    long_exposure = sum((p.exposure for p in positions if p.direction is TradeDirection.LONG), ZERO)
    short_exposure = sum(
        (p.exposure for p in positions if p.direction is TradeDirection.SHORT), ZERO
    )
    margin_used = sum((p.margin_required for p in positions), ZERO)
    equity = totals.equity

    # ---- loss state (only from a complete mark)
    obs = observations(session, account.id)
    current = (
        Observation(as_of, equity, totals.contributed_capital, "CURRENT", "current")
        if mark_status is MarkStatus.COMPLETE
        else None
    )
    funded = _funding(session, account.id)
    baselines: dict[PeriodKind, Baseline | None] = {}
    daily_loss = weekly_loss = drawdown = peak = budget = None
    daily_base = weekly_base = None
    if current is not None and config is not None:
        for kind in PeriodKind:
            baselines[kind] = _baseline(
                session, account.id, kind, as_of, obs, current, funded, persist=persist
            )
        daily, weekly = baselines[PeriodKind.DAILY], baselines[PeriodKind.WEEKLY]
        if daily is not None and weekly is not None:
            peak = peak_equity(
                [o for o in obs if o.observed_at <= as_of], equity, totals.contributed_capital
            )
            ls = loss_state(
                equity=equity,
                contributed_capital=totals.contributed_capital,
                daily=daily,
                weekly=weekly,
                peak=peak,
                daily_limit=limits.daily_loss_limit_pct,
                weekly_limit=limits.weekly_loss_limit_pct,
                drawdown_limit=limits.max_drawdown_pct,
            )
            daily_loss, weekly_loss, drawdown = ls.daily_loss, ls.weekly_loss, ls.drawdown
            daily_base, weekly_base = ls.daily_baseline, ls.weekly_baseline
            budget = ls.available_risk_budget

    recon_status, recon_current, recon_run = _reconciliation(session, account.id, sequence)
    statuses = breaker_statuses(session, account.id)
    found = rules.conditions(
        rules.StateFigures(
            mark_status=mark_status,
            mark_issues=issues,
            equity=equity,
            gross_exposure=long_exposure + short_exposure,
            net_exposure=long_exposure - short_exposure,
            short_exposure=short_exposure,
            margin_used=margin_used,
            open_positions=len(positions),
            daily_loss=daily_loss,
            daily_baseline=daily_base,
            weekly_loss=weekly_loss,
            weekly_baseline=weekly_base,
            drawdown=drawdown,
            peak_equity=peak,
            reconciliation_status=recon_status,
            reconciliation_run_id=None if recon_run is None else str(recon_run),
        ),
        limits,
    )

    view = PortfolioView(
        account_id=account.id,
        functional_currency=functional,
        ledger_sequence=sequence,
        mark_status=mark_status,
        mark_issues=issues,
        equity=equity,
        contributed_capital=totals.contributed_capital,
        cash=totals.cash,
        available_capital=totals.available_capital - margin_used,
        long_exposure=long_exposure,
        short_exposure=short_exposure,
        gross_exposure=long_exposure + short_exposure,
        net_exposure=long_exposure - short_exposure,
        margin_used=margin_used,
        positions=[
            HeldPosition(
                asset_id=p.asset.id,
                symbol=p.asset.symbol,
                asset_class=p.asset.asset_class.value,
                currency=p.currency,
                direction=p.direction,
                quantity=p.quantity,
                exposure=p.exposure,
                price_status=p.price_status,
                margin_required=p.margin_required,
                sector=p.sector,
                cluster=p.cluster,
            )
            for p in positions
        ],
        daily_loss=daily_loss,
        daily_baseline=daily_base,
        weekly_loss=weekly_loss,
        weekly_baseline=weekly_base,
        peak_equity=peak,
        drawdown=drawdown,
        available_risk_budget=budget,
        active_breakers=[b for b, s in statuses.items() if s is BreakerStatus.TRIPPED],
        reconciliation_status=recon_status,
        reconciliation_current=recon_current,
    )
    result = EvaluatedState(
        account=account,
        as_of=as_of,
        view=view,
        figures=totals,
        positions=positions,
        conditions=found,
        statuses=statuses,
        baselines=baselines,
        reconciliation_run_id=recon_run,
    )
    if not persist:
        return result

    snapshot = _write_snapshot(session, result, config, trigger, actor)
    result.snapshot_id = snapshot.id
    version_id = config.version_id if config is not None else None
    for t in rules.transitions(found, statuses):
        record_breaker_event(
            session,
            account.id,
            t.breaker,
            t.event,
            at=as_of,
            actor=MONITOR_ACTOR,
            actor_kind=ActorKind.SYSTEM,
            reason=t.condition.message
            if t.event is BreakerEvent.TRIPPED
            else f"automatic recovery: {t.condition.message}",
            measurements=t.condition.measurements(),
            config_version_id=version_id,
            snapshot_id=snapshot.id,
        )
        result.transitions.append(t)
        statuses[t.breaker] = (
            BreakerStatus.TRIPPED if t.event is BreakerEvent.TRIPPED else BreakerStatus.CLEAR
        )
    result.statuses = statuses
    result.view = view.model_copy(
        update={"active_breakers": [b for b, s in statuses.items() if s is BreakerStatus.TRIPPED]}
    )
    return result


def breakdown(state: EvaluatedState) -> dict[str, Any]:
    """Exposure by asset, asset class, sector and correlation cluster (gross, not netted)."""
    equity = state.view.equity

    def share(value: Decimal) -> str | None:
        if equity <= ZERO:
            return None
        with money_context():
            return str(value / equity)

    def group(key: str) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"long": ZERO, "short": ZERO})
        for p in state.positions:
            name = {
                "asset_class": p.asset.asset_class.value,
                "sector": p.sector or "UNCLASSIFIED",
                "cluster": p.cluster or "UNCLUSTERED",
            }[key]
            side = "long" if p.direction is TradeDirection.LONG else "short"
            buckets[name][side] += p.exposure
        return [
            {
                key: name,
                "long": str(v["long"]),
                "short": str(v["short"]),
                "gross": str(v["long"] + v["short"]),
                "net": str(v["long"] - v["short"]),
                "of_equity": share(v["long"] + v["short"]),
            }
            for name, v in sorted(buckets.items())
        ]

    return {
        "assets": [
            {
                "asset_id": str(p.asset.id),
                "symbol": p.asset.symbol,
                "asset_class": p.asset.asset_class.value,
                "direction": p.direction.value,
                "quantity": str(p.quantity),
                "exposure": str(p.exposure),
                "of_equity": share(p.exposure),
                "margin_required": str(p.margin_required),
                "sector": p.sector,
                "cluster": p.cluster,
                "price": None if p.price is None else str(p.price),
                "price_status": p.price_status,
                "price_source": p.price_source,
                "price_observed_at": None
                if p.price_observed_at is None
                else p.price_observed_at.isoformat(),
            }
            for p in state.positions
        ],
        "asset_classes": group("asset_class"),
        "sectors": group("sector"),
        "clusters": group("cluster"),
    }


def _write_snapshot(
    session: Session,
    state: EvaluatedState,
    config: RiskConfiguration | None,
    trigger: SnapshotTrigger,
    actor: str,
) -> RiskExposureSnapshotRecord:
    v = state.view
    parts = breakdown(state)
    conditions = [c.measurements() | {"breaker": c.breaker.value} for c in state.conditions]
    content = {
        "view": v.model_dump(mode="json"),
        "breakdown": parts,
        "conditions": conditions,
    }
    record = RiskExposureSnapshotRecord(
        portfolio_account_id=v.account_id,
        as_of=state.as_of,
        ledger_sequence=v.ledger_sequence,
        trigger=trigger,
        mark_status=v.mark_status,
        functional_currency=v.functional_currency,
        equity=v.equity,
        contributed_capital=v.contributed_capital,
        cash=v.cash,
        available_capital=v.available_capital,
        long_exposure=v.long_exposure,
        short_exposure=v.short_exposure,
        gross_exposure=v.gross_exposure,
        net_exposure=v.net_exposure,
        margin_used=v.margin_used,
        pending_exposure=v.pending_exposure,
        open_positions=len(v.positions),
        daily_loss=v.daily_loss,
        daily_baseline=v.daily_baseline,
        weekly_loss=v.weekly_loss,
        weekly_baseline=v.weekly_baseline,
        peak_equity=v.peak_equity,
        drawdown=v.drawdown,
        available_risk_budget=v.available_risk_budget,
        breakdown=parts,
        positions=parts["assets"],
        issues=v.mark_issues,
        conditions=conditions,
        config_version_id=None
        if config is None or config.version_id is None
        else uuid.UUID(config.version_id),
        engine_version=ENGINE_VERSION,
        content_sha256=_digest(content),
        created_by=actor,
    )
    session.add(record)
    session.flush()
    return record


__all__ = [
    "ENGINE_VERSION",
    "MONITOR_ACTOR",
    "EvaluatedState",
    "MarkedPosition",
    "breakdown",
    "breaker_statuses",
    "evaluate",
    "lock_account",
    "observations",
    "rate_sources",
    "record_breaker_event",
]
