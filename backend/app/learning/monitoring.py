"""Degradation monitoring of approved strategies and models (Phase 8B).

A monitoring pass compares **current** evidence with a **reference** and stores every
measurement (``learning_drift_measurements``), raising an alert for each WARNING or BREACH.

Strategies (every enabled strategy and every governed champion):

* Reference: the out-of-sample trades of the newest COMPLETED validation run of the strategy
  alone (8B). Without one there is nothing to compare against: INSUFFICIENT_DATA.
* Current: the strategy's CLOSED paper positions (7B), net of commissions, fees and borrow.
  Return per trade = net P/L / entry value.
* EXPECTANCY_SHIFT (Welch one-sided t-test, current mean below reference), WIN_RATE_SHIFT
  (two-proportion z-test), RETURN_DISTRIBUTION (two-sample KS), COST_PER_TRADE (ratio of mean
  cost / entry value), DRAWDOWN (cumulative closed-trade P/L against the validation run's
  capital, as a multiple of the reference out-of-sample drawdown), REGIME_FAILURE (the
  current benchmark regime is one in which the reference lost money on enough trades).
* FEATURE_PSI and FEATURE_KS: the strategy's scale-free input features in the live strategy
  feature snapshots, the last ``current_days`` against the ``reference_days`` before them.
  Snapshots overlap heavily, so KS p-values are tiny for trivial shifts: KS alerts only when
  significant *and* its statistic reaches 0.1 (warning) / 0.2 (breach).
  Price-level features (close, moving averages, ATR in currency, ...) are skipped: their
  levels differ between assets and drift with prices by construction.

Models (every 5A APPROVED version):

* PREDICTION_SHIFT: PSI of recent raw scores against the earlier ones.
* CALIBRATION_ECE: ECE of probabilities whose outcomes have matured (joined to 5A dataset
  observations by asset, timeframe, direction and decision time) against the training run's
  test ECE.

**Thresholds** come from a :class:`MonitorRule`: p-values for tests (warning 0.05, breach
0.01), customary PSI levels (0.1 / 0.25), ratios for costs and drawdown, and minimum
observation counts below which a measurement is INSUFFICIENT_DATA and never alerts. Several
measurements per pass are tested, so a single WARNING is weak evidence; BREACH levels are
stricter for that reason.

**Actions.** Without an operator-approved rule the monitor only warns. A rule approved by an
operator (``learning_monitoring_rules``, versioned) with action SUSPEND lets a BREACH of a
*performance* measurement (expectancy, win rate, drawdown) disable the strategy, recorded as
done by the monitor under that rule. The monitor never enables anything, never changes a
risk limit, and never promotes, approves or replaces a model or strategy.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.config import sha256_of
from app.backtesting.data import load_dataset, sessions_back
from app.core.clocks import Clock
from app.core.exceptions import AtlasError
from app.database.models import ActorType, Asset, MLModelVersionRecord, PaperPositionRecord
from app.database.models.learning import (
    LearningDegradationAlertRecord,
    LearningDriftMeasurementRecord,
    LearningMonitoringRuleRecord,
    ValidationExperimentRecord,
    ValidationFoldRecord,
    ValidationRunRecord,
    ValidationSegmentRecord,
)
from app.database.models.ml import (
    MLDatasetObservationRecord,
    MLPredictionRecord,
    MLTrainingRunRecord,
)
from app.database.models.strategy import StrategyFeatureSnapshotRecord
from app.database.repositories import AuditLogRepository
from app.database.sessions import session_scope
from app.learning import regimes as rg
from app.learning import statistics as st
from app.learning.enums import (
    AlertAction,
    MeasurementStatus,
    MonitorMetric,
    RuleAction,
    RunStatus,
    SegmentKind,
    SubjectKind,
)
from app.learning.governance import approved_candidate
from app.ml_engine.enums import LabelStatus, PredictionMode, PredictionValidity
from app.ml_engine.registry import lifecycle as ml
from app.strategies import catalog
from app.strategies.registry import StrategyRegistry

PRICE_LEVEL_FEATURES = frozenset(
    {
        "atr",
        "bollinger_lower",
        "bollinger_middle",
        "bollinger_upper",
        "close",
        "ema",
        "macd_histogram",
        "macd_line",
        "macd_signal",
        "prior_high",
        "prior_low",
        "sma",
    }
)
PERFORMANCE = frozenset(
    {MonitorMetric.EXPECTANCY_SHIFT, MonitorMetric.WIN_RATE_SHIFT, MonitorMetric.DRAWDOWN}
)
ACTOR = "degradation-monitor"


class MonitorRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_trades: int = Field(default=30, ge=10, le=10_000)
    warn_p: float = Field(default=0.05, gt=0, lt=0.5)
    breach_p: float = Field(default=0.01, gt=0, lt=0.5)
    drawdown_warn_ratio: float = Field(default=1.25, ge=1, le=10)
    drawdown_breach_ratio: float = Field(default=1.5, ge=1, le=10)
    cost_warn_ratio: float = Field(default=1.5, ge=1, le=10)
    cost_breach_ratio: float = Field(default=2.0, ge=1, le=10)
    # KS on thousands of overlapping snapshots is significant for trivial shifts: a KS
    # measurement alerts only when it is significant AND its statistic is material.
    ks_warn_statistic: float = Field(default=0.1, gt=0, le=1)
    ks_breach_statistic: float = Field(default=0.2, gt=0, le=1)
    psi_warn: float = Field(default=0.1, gt=0, le=5)
    psi_breach: float = Field(default=0.25, gt=0, le=5)
    min_feature_observations: int = Field(default=200, ge=50, le=1_000_000)
    ece_warn_increase: float = Field(default=0.03, gt=0, le=1)
    ece_breach_increase: float = Field(default=0.05, gt=0, le=1)
    min_predictions: int = Field(default=100, ge=30, le=1_000_000)
    reference_days: int = Field(default=120, ge=10, le=3650)
    current_days: int = Field(default=30, ge=5, le=365)


DEFAULT_RULE = MonitorRule()


class MonitorError(AtlasError):
    code = "learning_monitor_invalid"
    http_status = 400


@dataclass
class Measurement:
    metric: MonitorMetric
    status: MeasurementStatus
    value: float | None
    p_value: float | None
    n_reference: int
    n_current: int
    reference: dict[str, Any]
    current: dict[str, Any]
    thresholds: dict[str, Any]
    feature: str | None = None
    details: dict[str, Any] | None = None


def _by_p(p: float | None, rule: MonitorRule) -> MeasurementStatus:
    if p is None:
        return MeasurementStatus.INSUFFICIENT_DATA
    if p < rule.breach_p:
        return MeasurementStatus.BREACH
    if p < rule.warn_p:
        return MeasurementStatus.WARNING
    return MeasurementStatus.OK


def _by_level(value: float | None, warn: float, breach: float) -> MeasurementStatus:
    if value is None:
        return MeasurementStatus.INSUFFICIENT_DATA
    if value >= breach:
        return MeasurementStatus.BREACH
    if value >= warn:
        return MeasurementStatus.WARNING
    return MeasurementStatus.OK


# ---------------------------------------------------------------------- pure checks
def performance_checks(
    reference: list[dict[str, float]],
    current: list[dict[str, float]],
    rule: MonitorRule,
    *,
    reference_drawdown: float | None,
    capital: float,
) -> list[Measurement]:
    """``reference`` / ``current``: trades as {"return", "net_pnl", "cost_rate"}."""
    out: list[Measurement] = []
    nr, nc = len(reference), len(current)
    enough = nr >= rule.min_trades and nc >= rule.min_trades
    ref_r = [t["return"] for t in reference]
    cur_r = [t["return"] for t in current]
    thresholds = {"warn_p": rule.warn_p, "breach_p": rule.breach_p, "min_trades": rule.min_trades}

    def summary(rs: list[float]) -> dict[str, Any]:
        return {
            "trades": len(rs),
            "mean_return": sum(rs) / len(rs) if rs else None,
            "win_rate": sum(1 for r in rs if r > 0) / len(rs) if rs else None,
        }

    test = st.welch_shift(ref_r, cur_r) if enough else None
    out.append(
        Measurement(
            MonitorMetric.EXPECTANCY_SHIFT,
            _by_p(None if test is None else test[1], rule)
            if enough
            else MeasurementStatus.INSUFFICIENT_DATA,
            None if test is None else test[0],
            None if test is None else test[1],
            nr,
            nc,
            summary(ref_r),
            summary(cur_r),
            thresholds,
        )
    )
    prop = (
        st.proportion_drop(sum(1 for r in ref_r if r > 0), nr, sum(1 for r in cur_r if r > 0), nc)
        if enough
        else None
    )
    out.append(
        Measurement(
            MonitorMetric.WIN_RATE_SHIFT,
            _by_p(None if prop is None else prop[1], rule)
            if enough
            else MeasurementStatus.INSUFFICIENT_DATA,
            None if prop is None else prop[0],
            None if prop is None else prop[1],
            nr,
            nc,
            summary(ref_r),
            summary(cur_r),
            thresholds,
        )
    )
    ks = st.ks_test(ref_r, cur_r) if enough else None
    out.append(
        Measurement(
            MonitorMetric.RETURN_DISTRIBUTION,
            _by_p(None if ks is None else ks[1], rule)
            if enough
            else MeasurementStatus.INSUFFICIENT_DATA,
            None if ks is None else ks[0],
            None if ks is None else ks[1],
            nr,
            nc,
            summary(ref_r),
            summary(cur_r),
            thresholds,
        )
    )
    ref_c = [t["cost_rate"] for t in reference]
    cur_c = [t["cost_rate"] for t in current]
    ratio = None
    if enough and ref_c and sum(ref_c) > 0:
        ratio = (sum(cur_c) / len(cur_c)) / (sum(ref_c) / len(ref_c))
    out.append(
        Measurement(
            MonitorMetric.COST_PER_TRADE,
            _by_level(ratio, rule.cost_warn_ratio, rule.cost_breach_ratio)
            if enough
            else MeasurementStatus.INSUFFICIENT_DATA,
            ratio,
            None,
            nr,
            nc,
            {"mean_cost_rate": sum(ref_c) / len(ref_c) if ref_c else None},
            {"mean_cost_rate": sum(cur_c) / len(cur_c) if cur_c else None},
            {"warn_ratio": rule.cost_warn_ratio, "breach_ratio": rule.cost_breach_ratio},
        )
    )
    drawdown = (
        st.max_drawdown_path(np.array([t["net_pnl"] for t in current], dtype=float), capital)
        if current
        else 0.0
    )
    dd_ratio = None
    if nc >= rule.min_trades and reference_drawdown and reference_drawdown > 0:
        dd_ratio = drawdown / reference_drawdown
    out.append(
        Measurement(
            MonitorMetric.DRAWDOWN,
            _by_level(dd_ratio, rule.drawdown_warn_ratio, rule.drawdown_breach_ratio)
            if dd_ratio is not None
            else MeasurementStatus.INSUFFICIENT_DATA,
            dd_ratio,
            None,
            nr,
            nc,
            {"max_drawdown": reference_drawdown},
            {"max_drawdown": drawdown, "capital": capital},
            {"warn_ratio": rule.drawdown_warn_ratio, "breach_ratio": rule.drawdown_breach_ratio},
        )
    )
    return out


def feature_checks(
    reference: dict[str, list[float]], current: dict[str, list[float]], rule: MonitorRule
) -> list[Measurement]:
    out: list[Measurement] = []
    for key in sorted(set(reference) | set(current)):
        ref = reference.get(key, [])
        cur = current.get(key, [])
        enough = (
            len(ref) >= rule.min_feature_observations and len(cur) >= rule.min_feature_observations
        )
        value = st.psi(ref, cur) if enough else None
        out.append(
            Measurement(
                MonitorMetric.FEATURE_PSI,
                _by_level(value, rule.psi_warn, rule.psi_breach)
                if enough
                else MeasurementStatus.INSUFFICIENT_DATA,
                value,
                None,
                len(ref),
                len(cur),
                {"observations": len(ref)},
                {"observations": len(cur)},
                {
                    "warn": rule.psi_warn,
                    "breach": rule.psi_breach,
                    "minimum": rule.min_feature_observations,
                },
                feature=key,
            )
        )
        ks = st.ks_test(ref, cur) if enough else None
        ks_status = MeasurementStatus.INSUFFICIENT_DATA
        if ks is not None:
            ks_status = (
                _by_level(ks[0], rule.ks_warn_statistic, rule.ks_breach_statistic)
                if ks[1] < rule.warn_p
                else MeasurementStatus.OK
            )
        out.append(
            Measurement(
                MonitorMetric.FEATURE_KS,
                ks_status,
                None if ks is None else ks[0],
                None if ks is None else ks[1],
                len(ref),
                len(cur),
                {"observations": len(ref)},
                {"observations": len(cur)},
                {
                    "warn_p": rule.warn_p,
                    "warn_statistic": rule.ks_warn_statistic,
                    "breach_statistic": rule.ks_breach_statistic,
                },
                feature=key,
                details={
                    "note": "snapshots overlap (assets re-evaluated, shared windows), so p-values "
                    "are far too small; the KS statistic decides materiality"
                },
            )
        )
    return out


# ---------------------------------------------------------------------- service
class MonitoringService:
    def __init__(
        self, session_factory: sessionmaker[Session], clock: Clock, registry: StrategyRegistry
    ) -> None:
        self._sessions = session_factory
        self.clock = clock
        self.registry = registry

    # .................................................................. rules
    def approve_rule(
        self,
        kind: SubjectKind,
        key: str,
        rule: MonitorRule,
        action: RuleAction,
        *,
        actor: str,
        reason: str,
        active: bool = True,
    ) -> uuid.UUID:
        if action is RuleAction.SUSPEND and kind is not SubjectKind.STRATEGY:
            raise MonitorError("only a strategy can be suspended by a rule")
        if not reason.strip():
            raise MonitorError("a reason is required")
        with session_scope(self._sessions) as session:
            r = LearningMonitoringRuleRecord
            version = (
                session.scalar(
                    select(func.max(r.version)).where(r.subject_kind == kind, r.subject_key == key)
                )
                or 0
            ) + 1
            data = rule.model_dump()
            row = r(
                subject_kind=kind,
                subject_key=key,
                version=version,
                rule=data,
                rule_sha256=sha256_of(data),
                action=action,
                active=active,
                approved_by=actor,
                reason=reason.strip(),
            )
            session.add(row)
            session.flush()
            AuditLogRepository(session).append(
                actor_type=ActorType.USER,
                actor_id=actor,
                action="learning.monitoring_rule_approved",
                entity_type="learning_monitoring_rule",
                entity_id=str(row.id),
                reason=reason.strip(),
                new_state={"version": version, "action": action.value, "active": active},
                details={"subject_kind": kind.value, "subject_key": key},
            )
            return row.id

    @staticmethod
    def active_rule(
        session: Session, kind: SubjectKind, key: str
    ) -> LearningMonitoringRuleRecord | None:
        r = LearningMonitoringRuleRecord
        row = session.scalar(
            select(r)
            .where(r.subject_kind == kind, r.subject_key == key)
            .order_by(r.version.desc())
            .limit(1)
        )
        return row if row is not None and row.active else None

    # .................................................................. subjects
    def subjects(self, session: Session) -> list[tuple[SubjectKind, str]]:
        out: list[tuple[SubjectKind, str]] = []
        for sid in sorted(s.strategy_id for s in self.registry):
            if catalog.is_enabled(session, sid) or approved_candidate(
                session, SubjectKind.STRATEGY, sid
            ):
                out.append((SubjectKind.STRATEGY, sid))
        names = session.scalars(select(MLModelVersionRecord.model_name).distinct()).all()
        for name in sorted(names):
            if ml.approved_versions(session, name):
                out.append((SubjectKind.MODEL, name))
        return out

    # .................................................................. pass
    def run(
        self, *, subjects: list[tuple[SubjectKind, str]] | None = None
    ) -> tuple[uuid.UUID, dict[str, Any]]:
        monitor_id = uuid.uuid4()
        now = self.clock.now()
        report: dict[str, Any] = {
            "monitor_run_id": str(monitor_id),
            "at": now.isoformat(),
            "subjects": [],
        }
        with session_scope(self._sessions) as session:
            for kind, key in subjects or self.subjects(session):
                stored_rule = self.active_rule(session, kind, key)
                rule = (
                    DEFAULT_RULE
                    if stored_rule is None
                    else MonitorRule.model_validate(stored_rule.rule)
                )
                if kind is SubjectKind.STRATEGY:
                    measurements = self._strategy(session, key, rule, now)
                else:
                    measurements = self._model(session, key, rule, now)
                alerts = self._store(session, monitor_id, kind, key, now, measurements, stored_rule)
                report["subjects"].append(
                    {
                        "subject_kind": kind.value,
                        "subject_key": key,
                        "rule": None if stored_rule is None else str(stored_rule.id),
                        "measurements": len(measurements),
                        "alerts": alerts,
                    }
                )
        return monitor_id, report

    def _store(
        self,
        session: Session,
        monitor_id: uuid.UUID,
        kind: SubjectKind,
        key: str,
        now: datetime,
        measurements: list[Measurement],
        rule: LearningMonitoringRuleRecord | None,
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        suspended = False
        for m in measurements:
            row = LearningDriftMeasurementRecord(
                monitor_run_id=monitor_id,
                subject_kind=kind,
                subject_key=key,
                metric=m.metric,
                feature=m.feature,
                measured_at=now,
                reference=_clean(m.reference),
                current=_clean(m.current),
                value=_dec(m.value),
                p_value=_dec(m.p_value),
                thresholds=m.thresholds,
                n_reference=m.n_reference,
                n_current=m.n_current,
                status=m.status,
                rule_id=None if rule is None else rule.id,
                details=_clean(m.details or {}),
            )
            session.add(row)
            session.flush()
            if m.status not in (MeasurementStatus.WARNING, MeasurementStatus.BREACH):
                continue
            action, reference = AlertAction.WARNED, {}
            if (
                m.status is MeasurementStatus.BREACH
                and m.metric in PERFORMANCE
                and kind is SubjectKind.STRATEGY
                and rule is not None
                and rule.action is RuleAction.SUSPEND
                and not suspended
            ):
                strategy = self.registry.get(key)
                event, changed = catalog.set_status(
                    session,
                    strategy,
                    enabled=False,
                    reason=f"{m.metric.value} BREACH under monitoring rule {rule.id} "
                    f"v{rule.version} approved by {rule.approved_by}",
                    actor=f"{ACTOR} (rule {rule.id})",
                )
                suspended = True
                action = AlertAction.SUSPENDED if changed else AlertAction.WARNED
                reference = {"strategy_status_event_id": event.id, "changed": changed}
            message = (
                f"{key}: {m.metric.value}"
                + (f" [{m.feature}]" if m.feature else "")
                + f" is {m.status.value}"
                + ("" if m.value is None else f" (value {m.value:.4g}")
                + ("" if m.p_value is None else f", p {m.p_value:.3g}")
                + ("" if m.value is None else ")")
            )
            alert = LearningDegradationAlertRecord(
                subject_kind=kind,
                subject_key=key,
                severity="CRITICAL" if m.status is MeasurementStatus.BREACH else "WARNING",
                metric=m.metric,
                message=message,
                measurement_id=row.id,
                rule_id=None if rule is None else rule.id,
                action=action,
                action_reference=reference,
            )
            session.add(alert)
            session.flush()
            alerts.append({"id": str(alert.id), "message": message, "action": action.value})
        return alerts

    # .................................................................. strategies
    def reference_run(self, session: Session, strategy_id: str) -> ValidationRunRecord | None:
        r, e = ValidationRunRecord, ValidationExperimentRecord
        return session.scalar(
            select(r)
            .join(e, e.id == r.experiment_id)
            .where(
                r.status == RunStatus.COMPLETED,
                e.strategy_ids == [strategy_id],
            )
            .order_by(r.finished_at.desc())
            .limit(1)
        )

    def _strategy(
        self, session: Session, key: str, rule: MonitorRule, now: datetime
    ) -> list[Measurement]:
        run = self.reference_run(session, key)
        reference: list[dict[str, float]] = []
        ref_dd: float | None = None
        capital = 100_000.0
        if run is not None:
            capital = float(run.base_config.get("initial_capital", capital))
            for fold in session.scalars(
                select(ValidationFoldRecord).where(ValidationFoldRecord.run_id == run.id)
            ):
                reference += [
                    {"return": t["return"], "net_pnl": t["net_pnl"], "cost_rate": t["cost_rate"]}
                    for t in fold.out_of_sample.get("trades", [])
                ]
            oos = (run.summary or {}).get("out_of_sample", {})
            ref_dd = oos.get("max_drawdown")
        champion = approved_candidate(session, SubjectKind.STRATEGY, key)
        since = None
        if champion is not None:
            from app.learning.governance import latest_event

            ev = latest_event(session, champion.id)
            since = None if ev is None else ev.created_at
        p = PaperPositionRecord
        q = select(p).where(p.strategy_id == key, p.status == "CLOSED").order_by(p.closed_at)
        if since is not None:
            q = q.where(p.opened_at >= since)
        current = []
        for pos in session.scalars(q):
            costs = pos.commissions + pos.fees + pos.borrow_accrued
            net = pos.realized_pnl - costs
            if pos.entry_value > 0:
                current.append(
                    {
                        "return": float(net / pos.entry_value),
                        "net_pnl": float(net),
                        "cost_rate": float(costs / pos.entry_value),
                    }
                )
        measurements = performance_checks(
            reference, current, rule, reference_drawdown=ref_dd, capital=capital
        )
        for m in measurements:
            m.reference["validation_run_id"] = None if run is None else str(run.id)
            m.current["since"] = None if since is None else since.isoformat()
            m.current["source"] = "closed paper positions (7B), net of commissions, fees, borrow"
        measurements.append(self._regime_failure(session, key, run, now, rule))
        measurements += self._features(session, key, rule, now)
        return measurements

    def _regime_failure(
        self,
        session: Session,
        key: str,
        run: ValidationRunRecord | None,
        now: datetime,
        rule: MonitorRule,
    ) -> Measurement:
        losing: list[str] = []
        evaluated = 0  # trend regimes with enough reference trades to judge
        if run is not None:
            for seg in session.scalars(
                select(ValidationSegmentRecord).where(
                    ValidationSegmentRecord.run_id == run.id,
                    ValidationSegmentRecord.segment_kind == SegmentKind.MARKET_TREND,
                )
            ):
                if seg.metrics.get("trades", 0) >= rule.min_trades:
                    evaluated += 1
                    if seg.metrics.get("net_pnl", 0) < 0:
                        losing.append(seg.segment_value)
        bench = session.scalar(select(Asset.id).where(Asset.symbol == "SPY"))
        current_regime = "UNKNOWN"
        if bench is not None:
            dataset = load_dataset(
                session,
                [bench],
                provider="alpaca",
                feed="iex",
                first=sessions_back(now.date(), 120),
                last=now.date(),
                known_at=now,
                settle=timedelta(hours=1),
                split_adjusted=True,
            )
            series = dataset.split.get(bench) or {}
            regimes = rg.classify([(d, float(b.close)) for d, b in series.items() if b.valid])
            if regimes:
                current_regime = regimes[max(regimes)].trend
        status = MeasurementStatus.INSUFFICIENT_DATA
        if run is not None and current_regime != "UNKNOWN" and evaluated:
            status = MeasurementStatus.WARNING if current_regime in losing else MeasurementStatus.OK
        return Measurement(
            MonitorMetric.REGIME_FAILURE,
            status,
            None,
            None,
            0,
            0,
            {
                "losing_trend_regimes": losing,
                "validation_run_id": None if run is None else str(run.id),
            },
            {"trend_regime": current_regime},
            {"min_trades": rule.min_trades},
            details={"definition": rg.DEFINITION},
        )

    def _features(
        self, session: Session, key: str, rule: MonitorRule, now: datetime
    ) -> list[Measurement]:
        version = catalog.latest_version(session, key)
        if version is None:
            return []
        wanted = set()
        for keys in (version.required_features or {}).values():
            for item in keys if isinstance(keys, list) else []:
                # Stored as {"key": "rsi.v1(length=14)@1Day", "optional": ...} (4A catalog).
                k = str(item.get("key")) if isinstance(item, dict) else str(item)
                name = k.split(".", 1)[0]
                if name not in PRICE_LEVEL_FEATURES and name != "regime_label":
                    wanted.add(k)
        if not wanted:
            return []
        split = now - timedelta(days=rule.current_days)
        start = split - timedelta(days=rule.reference_days)
        s = StrategyFeatureSnapshotRecord
        reference: dict[str, list[float]] = defaultdict(list)
        current: dict[str, list[float]] = defaultdict(list)
        for cutoff, features in session.execute(
            select(s.data_cutoff, s.features).where(s.data_cutoff >= start, s.data_cutoff <= now)
        ):
            bucket = current if cutoff >= split else reference
            for k in wanted:
                f = features.get(k)
                if f and f.get("status") == "VALID" and f.get("value") is not None:
                    try:
                        v = float(f["value"])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(v):
                        bucket[k].append(v)
        out = feature_checks(dict(reference), dict(current), rule)
        for m in out:
            m.reference["window"] = [start.isoformat(), split.isoformat()]
            m.current["window"] = [split.isoformat(), now.isoformat()]
        return out

    # .................................................................. models
    def _model(
        self, session: Session, name: str, rule: MonitorRule, now: datetime
    ) -> list[Measurement]:
        approved = ml.approved_versions(session, name)
        if not approved:
            return []
        model = session.get(MLModelVersionRecord, approved[0])
        assert model is not None  # noqa: S101
        p = MLPredictionRecord
        rows = session.execute(
            select(p.created_at, p.output, p.asset_id, p.timeframe, p.direction, p.decision_at)
            .where(
                p.model_version_id == model.id,
                p.mode == PredictionMode.APPROVED,
                p.validity == PredictionValidity.VALID,
            )
            .order_by(p.created_at)
        ).all()
        split = now - timedelta(days=rule.current_days)
        ref_scores = [
            float(r.output["raw_score"])
            for r in rows
            if r.output and r.created_at < split and r.output.get("raw_score") is not None
        ]
        cur_scores = [
            float(r.output["raw_score"])
            for r in rows
            if r.output and r.created_at >= split and r.output.get("raw_score") is not None
        ]
        enough = len(ref_scores) >= rule.min_predictions and len(cur_scores) >= rule.min_predictions
        value = st.psi(ref_scores, cur_scores) if enough else None
        out = [
            Measurement(
                MonitorMetric.PREDICTION_SHIFT,
                _by_level(value, rule.psi_warn, rule.psi_breach)
                if enough
                else MeasurementStatus.INSUFFICIENT_DATA,
                value,
                None,
                len(ref_scores),
                len(cur_scores),
                {"model_version_id": str(model.id)},
                {},
                {"warn": rule.psi_warn, "breach": rule.psi_breach, "minimum": rule.min_predictions},
            )
        ]
        # Calibration on matured outcomes.
        probs, outcomes = [], []
        o = MLDatasetObservationRecord
        for r in rows:
            prob = (r.output or {}).get("probability")
            if prob is None or r.asset_id is None:
                continue
            label = session.scalar(
                select(o.label_class)
                .where(
                    o.asset_id == r.asset_id,
                    o.timeframe == r.timeframe,
                    o.direction == r.direction,
                    o.decision_at == r.decision_at,
                    o.label_status == LabelStatus.MATURED,
                    o.label_class.is_not(None),
                )
                .limit(1)
            )
            if label is not None:
                probs.append(float(prob))
                outcomes.append(int(label))
        run = session.get(MLTrainingRunRecord, model.training_run_id)
        ref_ece = None if run is None or not run.test_metrics else run.test_metrics.get("ece")
        ece = (
            st.expected_calibration_error(probs, outcomes)
            if len(probs) >= rule.min_predictions
            else None
        )
        increase = None if ece is None or ref_ece is None else ece - float(ref_ece)
        out.append(
            Measurement(
                MonitorMetric.CALIBRATION_ECE,
                _by_level(increase, rule.ece_warn_increase, rule.ece_breach_increase)
                if increase is not None
                else MeasurementStatus.INSUFFICIENT_DATA,
                increase,
                None,
                0,
                len(probs),
                {"test_ece": ref_ece},
                {"ece": ece, "matured_predictions": len(probs)},
                {
                    "warn_increase": rule.ece_warn_increase,
                    "breach_increase": rule.ece_breach_increase,
                    "minimum": rule.min_predictions,
                },
            )
        )
        return out


def _dec(value: float | None) -> Decimal | None:
    if value is None or not math.isfinite(value):
        return None
    return Decimal(repr(value)).quantize(Decimal("0.0000000001"))


def _clean(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


__all__ = [
    "DEFAULT_RULE",
    "PRICE_LEVEL_FEATURES",
    "Measurement",
    "MonitorError",
    "MonitorRule",
    "MonitoringService",
    "feature_checks",
    "performance_checks",
]
