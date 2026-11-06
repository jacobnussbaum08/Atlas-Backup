"""Phase 6B risk engine against a real PostgreSQL (TEST FIXTURE data, nothing executed)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.accounting.enums import InstrumentSpecSource
from app.accounting.reference import register_instrument_spec
from app.core.exceptions import InvalidRequestError
from app.data_engine.enums import PriceAdjustment
from app.database.models import (
    AIDecisionRecord,
    Asset,
    AuditLog,
    DecisionPolicyRecord,
    DecisionPolicyVersionRecord,
    DecisionRunRecord,
)
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.database.models.risk import (
    RiskAssessmentReasonRecord,
    RiskAssessmentRecord,
    RiskCircuitBreakerEventRecord,
    RiskConfigurationVersionRecord,
    RiskEventRecord,
    RiskExposureSnapshotRecord,
    RiskPeriodBaselineRecord,
)
from app.database.sessions import session_scope
from app.risk import governance, proposals
from app.risk.enums import (
    ActorKind,
    AssessmentResult,
    BaselineBasis,
    BreakerEvent,
    BreakerStatus,
    BreakerType,
    CheckOutcome,
    MarkStatus,
    PeriodKind,
)
from app.risk.governance import GovernanceError
from app.risk.service import RiskControlError
from app.risk.state import breaker_statuses
from app.strategies.enums import StrategyFamily
from app.tests.integration.accounting_support import SPEC_FROM
from app.tests.integration.ai_support import make_proposal, make_strategy
from app.tests.integration.risk_support import NOW, OPERATOR, SHORT, Risk, make_risk
from app.tests.integration.strategy_support import make_asset, seed_bars

pytestmark = pytest.mark.integration

PASS = AssessmentResult.PASS_FOR_RESERVATION
WED_CLOSE = datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
THU_CLOSE = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


@pytest.fixture
def risk(fresh_engine) -> Risk:
    return make_risk(fresh_engine)


def reasons(risk: Risk, assessment_id: uuid.UUID) -> list[RiskAssessmentReasonRecord]:
    with session_scope(risk.factory) as session:
        rows = session.scalars(
            select(RiskAssessmentReasonRecord)
            .where(RiskAssessmentReasonRecord.assessment_id == assessment_id)
            .order_by(RiskAssessmentReasonRecord.ordinal)
        ).all()
        session.expunge_all()
        return list(rows)


def decisive(risk: Risk, assessment_id: uuid.UUID) -> set[str]:
    return {
        r.reason_code
        for r in reasons(risk, assessment_id)
        if r.outcome in (CheckOutcome.FAIL, CheckOutcome.UNKNOWN, CheckOutcome.HALT)
        and r.reason_code
    }


def breaker_events(risk: Risk, account: uuid.UUID) -> list[tuple[str, int, str, str]]:
    with session_scope(risk.factory) as session:
        e = RiskCircuitBreakerEventRecord
        return [
            (r.breaker_type.value, r.sequence, r.event.value, r.actor)
            for r in session.scalars(
                select(e).where(e.portfolio_account_id == account).order_by(e.id)
            )
        ]


# ------------------------------------------------------------------ assessments
def test_long_assessment_is_sized_from_the_ledger_and_fully_recorded(risk):
    account = risk.account()
    asset = risk.asset("RSKA")  # MOCK close 99.70 for the latest session
    out = risk.assess(account, risk.proposal(asset, "99.70", "94.70"))
    # Default cost assumptions, no fresh quote (25 bps assumed spread): entry fill 99.874475,
    # stop fill 94.486925, fees 0.01943614, 5.40698614 a share plus 0.005 commission with a
    # 1.00 minimum per leg: 92 shares, loss 92 x 5.40698614 + 2.00 = 499.4427 -> 499.45.
    assert out.result is PASS
    assert out.permitted_quantity == D("92")
    with session_scope(risk.factory) as session:
        rec = session.get(RiskAssessmentRecord, out.assessment_id)
        assert rec is not None
        assert rec.equity == D("100000.00")
        assert rec.risk_budget == D("500.00")
        assert rec.potential_loss == D("499.45")
        assert rec.config_version_id is not None
        assert rec.exposure_snapshot_id is not None
        assert rec.valid_until == rec.assessed_at + timedelta(seconds=300)
        assert D(rec.inputs["portfolio"]["equity"]) == D("100000")
        assert rec.inputs["market"]["price_status"] == "FRESH"
        snapshot = session.get(RiskExposureSnapshotRecord, rec.exposure_snapshot_id)
        assert snapshot is not None
        assert snapshot.mark_status is MarkStatus.COMPLETE
        event = session.scalar(
            select(RiskEventRecord).where(RiskEventRecord.subject_id == str(rec.id))
        )
        assert event is not None
        assert "PASS_FOR_RESERVATION" in event.summary
    checks = reasons(risk, out.assessment_id)
    assert [c.ordinal for c in checks] == list(range(1, len(checks) + 1))
    assert {c.check_code for c in checks} >= {"circuit_breakers", "potential_loss", "liquidity"}
    reproduced, outcome = risk.service.replay(out.assessment_id)
    assert reproduced is True
    assert outcome.permitted_quantity == D("92")


def test_every_decision_is_recorded_with_its_reasons(risk):
    account = risk.account()
    asset = risk.asset("RSKA")
    missing_stop = risk.assess(account, risk.proposal(asset, "99.70", None))
    assert missing_stop.result is AssessmentResult.REJECTED
    assert decisive(risk, missing_stop.assessment_id) == {"MISSING_STOP"}
    stale = risk.asset("STALE", bars_as_of=NOW - timedelta(days=3))
    old = risk.assess(account, risk.proposal(stale, "99.70", "94.70"))
    assert old.result is AssessmentResult.INSUFFICIENT_DATA
    assert decisive(risk, old.assessment_id) == {"PRICE_STALE"}
    nothing = risk.asset("NODATA", adv=None)
    no_liquidity = risk.assess(account, risk.proposal(nothing, "99.70", "94.70"))
    assert decisive(risk, no_liquidity.assessment_id) == {"LIQUIDITY_DATA_MISSING"}
    with session_scope(risk.factory) as session:
        assert session.scalar(select(func.count()).select_from(RiskAssessmentRecord)) == 3


def test_short_needs_margin_data_or_an_explicit_policy(risk):
    account = risk.account()
    stock = risk.asset("RSKS", start=50.3)  # close 50.00
    refused = risk.assess(account, risk.proposal(stock, "50.00", "52.00", SHORT))
    assert refused.result is AssessmentResult.REJECTED
    assert decisive(risk, refused.assessment_id) == {"MARGIN_DATA_MISSING"}
    with session_scope(risk.factory) as session:
        governance.change(
            session,
            account,
            actor=OPERATOR,
            reason="use the Reg T minimum when the provider reports none",
            at=risk.now(),
            hard_limit_updates={"missing_margin_policy": "USE_CONFIGURED_MINIMUM"},
        )
    allowed = risk.assess(account, risk.proposal(stock, "50.00", "52.00", SHORT))
    assert allowed.result is PASS
    assert allowed.outcome is not None
    assert allowed.outcome.risk_budget == D("250.00")  # half the long budget
    assert allowed.outcome.potential_loss is not None
    assert allowed.outcome.potential_loss <= D("250.00")
    codes = {c.reason_code for c in reasons(risk, allowed.assessment_id)}
    assert {"MARGIN_RATE_ASSUMED", "BORROW_COST_ASSUMED"} <= codes


def test_foreign_currency_instrument_uses_a_fresh_rate(risk):
    account = risk.account()
    risk.books.rate("EUR", "1.0835")
    eur = make_asset(risk.factory, "EURSTK")
    with session_scope(risk.factory) as session:
        stored = session.get(Asset, eur)
        assert stored is not None
        stored.quote_currency = "EUR"
    with session_scope(risk.factory) as session:
        register_instrument_spec(
            session,
            asset_id=eur,
            quantity_increment=D("1"),
            min_quantity=D("1"),
            valid_from=SPEC_FROM,
            source=InstrumentSpecSource.OPERATOR,
            provenance="test fixture lot size",
            created_by="test",
        )
    seed_bars(
        risk.factory, eur, as_of=NOW, count=5, start=100.0, step=0.0, adjustment=PriceAdjustment.RAW
    )
    risk.adv(eur, "50000000")
    out = risk.assess(account, risk.proposal(eur, "99.70", "94.70"))
    assert out.result is PASS
    assert out.outcome is not None
    assert out.outcome.calculations["fx_rate"] == "1.083500000000"
    # The EUR loss converted at 1.0835 still fits the 500.00 USD budget.
    assert out.outcome.potential_loss is not None
    assert out.outcome.potential_loss <= D("500.00")
    gbp = make_asset(risk.factory, "GBPSTK")
    with session_scope(risk.factory) as session:
        stored = session.get(Asset, gbp)
        assert stored is not None
        stored.quote_currency = "GBP"
    no_rate = risk.assess(account, risk.proposal(gbp, "99.70", "94.70"))
    assert "FX_RATE_MISSING" in decisive(risk, no_rate.assessment_id)


def test_exposure_comes_from_the_ledger_and_matches_a_valuation_checkpoint(risk):
    account = risk.account()
    a = risk.asset("RSKA", sector="TECH")
    b = risk.asset("RSKB", start=50.3, sector="ENERGY")
    risk.books.trade(account, "long_purchase", "buy-a", a, "100", "99.70", commission="1.00")
    risk.books.trade(account, "short_open", "short-b", b, "40", "50.00")
    risk.reconcile(account)
    checkpoint = risk.books.ctx.valuation.checkpoint(account, actor="test")
    state = risk.service.monitor(account)
    v = state.view
    assert v.mark_status is MarkStatus.COMPLETE
    assert v.long_exposure == D("9970.00")
    assert v.short_exposure == D("2000.00")
    assert v.gross_exposure == D("11970.00")
    assert v.net_exposure == D("7970.00")
    assert v.equity == checkpoint.equity == D("99999.00")
    # Short collateral beyond the restricted proceeds: 50% (Reg T minimum) of 2,000.
    assert v.margin_used == D("1000.00")
    # Available capital = cash - accrued costs (6A) - short collateral; proceeds are not cash.
    assert v.available_capital == D("100000.00") - D("9971.00") - D("1000.00")
    with session_scope(risk.factory) as session:
        snap = session.get(PortfolioSnapshotRecord, checkpoint.snapshot_id)
        assert snap is not None
        assert snap.long_exposure == v.long_exposure
        assert snap.short_exposure == v.short_exposure
        risk_snap = session.get(RiskExposureSnapshotRecord, state.snapshot_id)
        assert risk_snap is not None
        sectors = {s["sector"]: s for s in risk_snap.breakdown["sectors"]}
        assert D(sectors["TECH"]["long"]) == D("9970.00")
        assert D(sectors["ENERGY"]["short"]) == D("2000.00")


# ------------------------------------------------------------------ circuit breakers
def test_kill_switch_halts_new_entries_until_an_operator_releases_it(risk):
    account = risk.account()
    asset = risk.asset("RSKA")
    assert risk.service.engage_kill_switch(account, actor=OPERATOR, reason="manual halt test")
    assert not risk.service.engage_kill_switch(account, actor=OPERATOR, reason="again")
    halted = risk.assess(account, risk.proposal(asset, "99.70", "94.70"))
    assert halted.result is AssessmentResult.RISK_HALTED
    assert decisive(risk, halted.assessment_id) == {"BREAKER_ACTIVE"}
    with pytest.raises(GovernanceError):
        risk.service.reset_breaker(
            account, BreakerType.KILL_SWITCH, actor="ai:coordinator", reason="resume"
        )
    with pytest.raises(GovernanceError):
        risk.service.engage_kill_switch(account, actor="system:risk-monitor", reason="x" * 5)
    risk.service.reset_breaker(account, BreakerType.KILL_SWITCH, actor=OPERATOR, reason="resume")
    assert risk.assess(account, risk.proposal(asset, "99.70", "94.70")).result is PASS
    assert breaker_events(risk, account) == [
        ("KILL_SWITCH", 1, "TRIPPED", OPERATOR),
        ("KILL_SWITCH", 2, "RESET", OPERATOR),
    ]
    with session_scope(risk.factory) as session:
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.action.like("risk.breaker_%"))
        ).all()
        assert actions == ["risk.breaker_tripped", "risk.breaker_reset"]


def test_daily_loss_breaker_activation_review_and_recovery(risk):
    account = risk.account()
    a = risk.asset("RSKA", sector="SECTOR_A")  # Tuesday close 99.70
    risk.books.trade(account, "long_purchase", "buy-a", a, "400", "99.70")
    risk.reconcile(account)
    first = risk.service.monitor(account)  # Tuesday after the close
    assert first.view.daily_loss == 0
    assert first.baselines[PeriodKind.DAILY] is not None
    assert first.baselines[PeriodKind.DAILY].basis is BaselineBasis.FUNDED_IN_PERIOD

    # Wednesday's close at 89.70: 400 x 10 = 4,000 loss, 4% of the 100,000 baseline.
    risk.clock.set(WED_CLOSE)
    risk.bar(a, 90.0, session_close_as_of=WED_CLOSE + timedelta(hours=1))
    wed = risk.service.monitor(account)
    assert wed.view.equity == D("96000.00")
    assert wed.view.daily_loss == D("4000.00")
    assert [(t.breaker, t.event) for t in wed.transitions] == [
        (BreakerType.DAILY_LOSS, BreakerEvent.TRIPPED)
    ]
    # Monitoring continues and nothing is sold: the position is still there.
    assert wed.view.long_exposure == D("35880.00")
    halted = risk.assess(account, risk.proposal(a, "89.70", "85.00"))
    assert halted.result is AssessmentResult.RISK_HALTED
    with pytest.raises(RiskControlError, match="still breached"):
        risk.service.reset_breaker(
            account, BreakerType.DAILY_LOSS, actor=OPERATOR, reason="reviewed the loss"
        )

    # Thursday, unchanged price: the new day's loss is zero, so an operator may resume.
    risk.clock.set(THU_CLOSE)
    risk.bar(a, 90.0, session_close_as_of=THU_CLOSE + timedelta(hours=1))
    still = risk.service.monitor(account)
    assert still.statuses[BreakerType.DAILY_LOSS] is BreakerStatus.TRIPPED  # never silently
    risk.service.reset_breaker(
        account, BreakerType.DAILY_LOSS, actor=OPERATOR, reason="reviewed the loss"
    )
    b = risk.asset("RSKB", start=50.3, sector="SECTOR_B")
    # Thursday's bar for B, available at the close (a bar published later would be stale).
    risk.bar(b, 50.3, session_close_as_of=THU_CLOSE + timedelta(hours=1))
    resumed = risk.assess(account, risk.proposal(b, "50.00", "48.00"))
    assert resumed.result is PASS, decisive(risk, resumed.assessment_id)
    assert resumed.outcome is not None
    # Weekly loss 4,000 of 5,000 allowed: only 1,000 of loss allowance is left; per-trade
    # budget 0.5% x 96,000 = 480.00 fits inside it.
    assert resumed.outcome.risk_budget == D("480.00")
    assert breaker_events(risk, account) == [
        ("DAILY_LOSS", 1, "TRIPPED", "system:risk-monitor"),
        ("DAILY_LOSS", 2, "RESET", OPERATOR),
    ]
    with session_scope(risk.factory) as session:
        refused = session.scalar(
            select(func.count())
            .select_from(RiskEventRecord)
            .where(RiskEventRecord.event_type == "BREAKER_RESET_REFUSED")
        )
        assert refused == 1
        baselines = session.scalars(
            select(RiskPeriodBaselineRecord).order_by(RiskPeriodBaselineRecord.period_start)
        ).all()
        thursday = [x for x in baselines if x.period_label == "2026-11-05"]
        assert thursday
        assert thursday[0].baseline_equity == D("96000.00")
        assert thursday[0].basis is BaselineBasis.OBSERVED_AT_BOUNDARY


def test_stale_data_halts_and_recovers_automatically(risk):
    account = risk.account()
    a = risk.asset("RSKA")
    risk.books.trade(account, "long_purchase", "buy-a", a, "100", "99.70")
    risk.reconcile(account)
    # Thursday 10:00 ET: the latest price is Tuesday's close, stale during the session.
    risk.clock.set(datetime(2026, 11, 5, 15, 0, tzinfo=UTC))
    stale = risk.service.monitor(account)
    assert stale.view.mark_status is MarkStatus.STALE
    assert stale.view.daily_loss is None
    assert risk.assess(account, risk.proposal(a, "99.70", "94.70")).result is (
        AssessmentResult.RISK_HALTED
    )
    risk.clock.set(datetime(2026, 11, 5, 22, 30, tzinfo=UTC))
    risk.bar(a, 100.0, session_close_as_of=datetime(2026, 11, 5, 22, 30, tzinfo=UTC))
    fresh = risk.service.monitor(account)
    assert fresh.view.mark_status is MarkStatus.COMPLETE
    assert breaker_events(risk, account) == [
        ("DATA_QUALITY", 1, "TRIPPED", "system:risk-monitor"),
        ("DATA_QUALITY", 2, "RESET", "system:risk-monitor"),
    ]


def test_reconciliation_failure_halts_and_cannot_be_reset_while_failing(risk, fresh_engine):
    account = risk.account()
    with fresh_engine.begin() as conn:
        conn.execute(
            text("UPDATE currency_balances SET balance = balance + 1 WHERE account_code = '1000'")
        )
    assert risk.reconcile(account) == "FAILED"
    state = risk.service.monitor(account)
    assert state.statuses[BreakerType.RECONCILIATION_FAILURE] is BreakerStatus.TRIPPED
    asset = risk.asset("RSKA")
    assert risk.assess(account, risk.proposal(asset, "99.70", "94.70")).result is (
        AssessmentResult.RISK_HALTED
    )
    with pytest.raises(RiskControlError):
        risk.service.reset_breaker(
            account, BreakerType.RECONCILIATION_FAILURE, actor=OPERATOR, reason="try anyway"
        )


def test_excessive_short_exposure_trips_without_liquidating(risk):
    account = risk.account()
    s = risk.asset("RSKS", start=50.3)
    risk.books.trade(account, "short_open", "short-s", s, "700", "50.00")  # 35% short
    risk.reconcile(account)
    state = risk.service.monitor(account)
    assert state.statuses[BreakerType.EXCESSIVE_EXPOSURE] is BreakerStatus.TRIPPED
    assert state.view.short_exposure == D("35000.00")
    with pytest.raises(RiskControlError, match="still breached"):
        risk.service.reset_breaker(
            account, BreakerType.EXCESSIVE_EXPOSURE, actor=OPERATOR, reason="reviewed"
        )
    with session_scope(risk.factory) as session:
        event = session.scalar(select(RiskCircuitBreakerEventRecord))
        assert event is not None
        assert "short exposure" in event.reason
        assert event.response["liquidates"].startswith("nothing")


def test_execution_safety_interface(risk):
    account = risk.account()
    assert risk.service.report_execution_safety(
        account, source="system:execution", reason="simulated fill error", details={"code": 1}
    )
    assert breaker_statuses_of(risk, account)[BreakerType.EXECUTION_SAFETY] is (
        BreakerStatus.TRIPPED
    )
    with pytest.raises(InvalidRequestError):
        risk.service.report_execution_safety(account, source="ai:x", reason="n/a", details={})


def breaker_statuses_of(risk: Risk, account: uuid.UUID) -> dict[BreakerType, BreakerStatus]:
    with session_scope(risk.factory) as session:
        return breaker_statuses(session, account)


# ------------------------------------------------------------------ governance
def test_configuration_versions_are_immutable_and_operator_only(risk, fresh_engine):
    account = risk.account()
    asset = risk.asset("RSKA")
    with session_scope(risk.factory) as session, pytest.raises(GovernanceError):
        governance.change(
            session,
            account,
            actor="ai:coordinator",
            reason="the model wants more risk",
            at=risk.now(),
            hard_limit_updates={"risk_per_trade_pct": "0.05"},
        )
    with session_scope(risk.factory) as session:
        result = governance.change(
            session,
            account,
            actor=OPERATOR,
            reason="halve the per-trade risk for the test",
            at=risk.now(),
            hard_limit_updates={"risk_per_trade_pct": "0.0025"},
        )
    assert result.version == 2
    assert result.changes == [
        {
            "section": "hard_limits",
            "field": "risk_per_trade_pct",
            "previous": "0.005",
            "new": "0.0025",
        }
    ]
    out = risk.assess(account, risk.proposal(asset, "99.70", "94.70"))
    assert out.outcome is not None
    assert out.outcome.risk_budget == D("250.00")
    with session_scope(risk.factory) as session:
        versions = session.scalars(
            select(RiskConfigurationVersionRecord).order_by(RiskConfigurationVersionRecord.version)
        ).all()
        assert [v.version for v in versions] == [1, 2]
        assert versions[1].previous_version_id == versions[0].id
        assert versions[0].hard_limits["risk_per_trade_pct"] == "0.005"  # history kept
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "risk.configuration_changed")
        )
        assert audit is not None
        assert audit.previous_state is not None
        assert audit.previous_state["hard_limits"]["risk_per_trade_pct"] == "0.005"
        assert audit.new_state is not None
        assert audit.new_state["hard_limits"]["risk_per_trade_pct"] == "0.0025"
        version_id, configuration_id = versions[1].id, versions[1].configuration_id
    for statement in (
        "UPDATE risk_configuration_versions SET hard_limits = '{}'::jsonb",
        "DELETE FROM risk_configuration_versions",
        "TRUNCATE risk_configuration_versions CASCADE",
    ):
        with pytest.raises(DBAPIError, match="append-only"), fresh_engine.begin() as conn:
            conn.execute(text(statement))

    def raw_version(version: int, actor: str, previous: uuid.UUID | None) -> None:
        with fresh_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO risk_configuration_versions (id, configuration_id, "
                    "portfolio_account_id, version, previous_version_id, change_kind, "
                    "hard_limits, preferences, hard_limits_sha256, preferences_sha256, changes, "
                    "actor, reason, created_at) VALUES (gen_random_uuid(), :c, :a, :v, :p, "
                    "'HARD_LIMITS', '{\"risk_per_trade_pct\": \"0.05\"}', '{}', 'x', 'x', '[]', "
                    ":actor, 'raw write attempt', now())"
                ),
                {"c": configuration_id, "a": account, "v": version, "p": previous, "actor": actor},
            )

    with pytest.raises(IntegrityError, match="operator_actor"):
        raw_version(3, "strategy:momentum", version_id)
    with pytest.raises(DBAPIError, match="must follow version 2"):
        raw_version(5, OPERATOR, version_id)


# ------------------------------------------------------------------ database guards
def _clone(row: RiskAssessmentRecord, **changes) -> RiskAssessmentRecord:
    columns = {
        c.key: getattr(row, c.key)
        for c in RiskAssessmentRecord.__table__.columns
        if c.key not in ("id", "created_txid", "created_at", "request_key")
    }
    return RiskAssessmentRecord(id=uuid.uuid4(), **(columns | changes))


def _reason(assessment_id: uuid.UUID, outcome: CheckOutcome, ordinal: int = 1):
    return RiskAssessmentReasonRecord(
        assessment_id=assessment_id,
        ordinal=ordinal,
        check_code="raw",
        outcome=outcome,
        message="raw insert attempt",
    )


def test_the_database_refuses_passes_that_bypass_the_engine(risk, fresh_engine):
    account = risk.account()
    asset = risk.asset("RSKA")
    passed = risk.assess(account, risk.proposal(asset, "99.70", "94.70"))
    with session_scope(risk.factory) as session:
        source = session.get(RiskAssessmentRecord, passed.assessment_id)
        assert source is not None
        session.expunge(source)

    def attempt(*rows) -> None:
        with session_scope(risk.factory) as session:
            for row in rows:
                session.add(row)
                session.flush()

    # A budget above the configured risk per trade.
    big = _clone(source, risk_budget=D("5000.00"), potential_loss=D("4999.00"))
    with pytest.raises(DBAPIError, match="exceeds"):
        attempt(big, _reason(big.id, CheckOutcome.PASS))
    # A loss above the budget, or no quantity.
    over = _clone(source, potential_loss=D("500.01"))
    with pytest.raises(IntegrityError, match="pass_is_sized_within_budget"):
        attempt(over)
    # A pass with a failing check, or with no recorded checks.
    failing = _clone(source)
    with pytest.raises(DBAPIError, match="checks say REJECTED"):
        attempt(
            failing,
            _reason(failing.id, CheckOutcome.PASS),
            _reason(failing.id, CheckOutcome.FAIL, 2),
        )
    bare = _clone(source)
    with pytest.raises(DBAPIError, match="no recorded checks"):
        attempt(bare)
    # A rejection presented as a pass of its checks.
    labelled = _clone(source, result=AssessmentResult.REJECTED, permitted_quantity=None)
    with pytest.raises(DBAPIError, match="checks say PASS_FOR_RESERVATION"):
        attempt(labelled, _reason(labelled.id, CheckOutcome.PASS))
    # Checks cannot be added to an existing assessment later.
    with pytest.raises(DBAPIError, match="assessment's transaction"):
        attempt(_reason(source.id, CheckOutcome.PASS, 999))
    # A pass against a configuration that is no longer the latest.
    with session_scope(risk.factory) as session:
        governance.change(
            session,
            account,
            actor=OPERATOR,
            reason="new version for the guard test",
            at=risk.now(),
            hard_limit_updates={"max_open_positions": 6},
        )
    stale_config = _clone(source)
    with pytest.raises(DBAPIError, match="latest configuration"):
        attempt(stale_config, _reason(stale_config.id, CheckOutcome.PASS))
    # A pass while a circuit breaker is tripped.
    risk.service.engage_kill_switch(account, actor=OPERATOR, reason="guard test")
    with session_scope(risk.factory) as session:
        latest = governance.latest_version(session, account)
        assert latest is not None
        latest_id = latest.id
    halted = _clone(source, config_version_id=latest_id)
    with pytest.raises(DBAPIError, match="breakers are tripped: KILL_SWITCH"):
        attempt(halted, _reason(halted.id, CheckOutcome.PASS))
    # Stored decisions cannot be rewritten.
    for statement in (
        "UPDATE risk_assessments SET result = 'PASS_FOR_RESERVATION'",
        "UPDATE risk_assessment_reasons SET outcome = 'PASS'",
        "DELETE FROM risk_circuit_breaker_events",
        "UPDATE risk_exposure_snapshots SET equity = 0",
        "DELETE FROM risk_period_baselines",
        "DELETE FROM risk_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"), fresh_engine.begin() as conn:
            conn.execute(text(statement))


def test_breaker_transitions_are_checked_by_the_database(risk):
    account = risk.account()

    def event(breaker, seq, ev, prev, new, kind=ActorKind.SYSTEM, actor="system:x"):
        with session_scope(risk.factory) as session:
            session.add(
                RiskCircuitBreakerEventRecord(
                    portfolio_account_id=account,
                    breaker_type=breaker,
                    sequence=seq,
                    event=ev,
                    previous_status=prev,
                    new_status=new,
                    occurred_at=risk.now(),
                    actor_kind=kind,
                    actor=actor,
                    reason="raw insert",
                )
            )

    T, C = BreakerStatus.TRIPPED, BreakerStatus.CLEAR
    with pytest.raises(DBAPIError, match="sequence 1 from CLEAR"):
        event(BreakerType.DAILY_LOSS, 2, BreakerEvent.TRIPPED, C, T)
    with pytest.raises(IntegrityError, match="valid_transition"):
        event(BreakerType.DAILY_LOSS, 1, BreakerEvent.RESET, C, T, ActorKind.OPERATOR, OPERATOR)
    with pytest.raises(IntegrityError, match="kill_switch_operator"):
        event(BreakerType.KILL_SWITCH, 1, BreakerEvent.TRIPPED, C, T)
    event(BreakerType.DAILY_LOSS, 1, BreakerEvent.TRIPPED, C, T)
    # Only an operator resets a loss breaker; a duplicate trip is out of sequence.
    with pytest.raises(IntegrityError, match="manual_reset"):
        event(BreakerType.DAILY_LOSS, 2, BreakerEvent.RESET, T, C)
    with pytest.raises(IntegrityError, match="valid_transition"):
        event(BreakerType.DAILY_LOSS, 2, BreakerEvent.TRIPPED, T, T)
    with pytest.raises(IntegrityError, match="operator_prefix_matches"):
        event(BreakerType.WEEKLY_LOSS, 1, BreakerEvent.TRIPPED, C, T, ActorKind.SYSTEM, OPERATOR)
    event(BreakerType.DAILY_LOSS, 2, BreakerEvent.RESET, T, C, ActorKind.OPERATOR, OPERATOR)
    assert breaker_statuses_of(risk, account)[BreakerType.DAILY_LOSS] is BreakerStatus.CLEAR


# ------------------------------------------------------------------ concurrency
def test_concurrent_assessments_and_breaker_trips(risk):
    account = risk.account()
    asset = risk.asset("RSKA")
    risk.books.trade(account, "long_purchase", "buy-a", asset, "10", "99.70")
    risk.reconcile(account)
    results: list[AssessmentResult] = []
    errors: list[BaseException] = []
    start = threading.Barrier(12)

    def assess(index: int) -> None:
        try:
            start.wait()
            out = risk.assess(account, risk.proposal(asset, "99.70", "94.70"), key=f"k{index % 6}")
            results.append(out.result)
        except BaseException as exc:
            errors.append(exc)

    def kill() -> None:
        try:
            start.wait()
            risk.service.engage_kill_switch(account, actor=OPERATOR, reason="concurrent halt")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=assess, args=(i,)) for i in range(10)]
    threads += [threading.Thread(target=kill) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == 10
    with session_scope(risk.factory) as session:
        # Six distinct request keys: each recorded once, retries return the stored one.
        assert session.scalar(select(func.count()).select_from(RiskAssessmentRecord)) == 6
        kills = session.scalars(
            select(RiskCircuitBreakerEventRecord).where(
                RiskCircuitBreakerEventRecord.breaker_type == BreakerType.KILL_SWITCH
            )
        ).all()
        assert [(k.sequence, k.event) for k in kills] == [(1, BreakerEvent.TRIPPED)]
        kill_tx = kills[0].id
        assert kill_tx
    # Concurrent monitors with stale data: exactly one DATA_QUALITY trip.
    risk.clock.set(datetime(2026, 11, 5, 15, 0, tzinfo=UTC))
    barrier = threading.Barrier(6)

    def monitor() -> None:
        try:
            barrier.wait()
            risk.service.monitor(account)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=monitor) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    events = [e for e in breaker_events(risk, account) if e[0] == "DATA_QUALITY"]
    assert events == [("DATA_QUALITY", 1, "TRIPPED", "system:risk-monitor")]


def test_request_keys_are_idempotent(risk):
    account = risk.account()
    asset = risk.asset("RSKA")
    prop = risk.proposal(asset, "99.70", "94.70")
    first = risk.assess(account, prop, key="retry-1")
    second = risk.assess(account, prop, key="retry-1")
    assert first.created
    assert not second.created
    assert second.assessment_id == first.assessment_id
    with pytest.raises(InvalidRequestError, match="different proposal"):
        risk.assess(account, risk.proposal(asset, "99.70", "90.00"), key="retry-1")


# ------------------------------------------------------------------ proposal sources
def _decision(risk: Risk, asset: uuid.UUID, lead: uuid.UUID, outcome: str) -> uuid.UUID:
    with session_scope(risk.factory) as session:
        policy = DecisionPolicyRecord(code=f"fixture_{uuid.uuid4().hex[:6]}", description="fixture")
        session.add(policy)
        session.flush()
        version = DecisionPolicyVersionRecord(
            policy_id=policy.id,
            version=1,
            parameters={},
            parameters_sha256=uuid.uuid4().hex * 2,
            logic_version="fixture",
            reason="fixture",
            created_by="test",
        )
        session.add(version)
        session.flush()
        at = NOW - timedelta(minutes=10)
        run = DecisionRunRecord(
            policy_version_id=version.id,
            mode="LIVE",
            basis="RECORDED",
            trigger="test",
            decision_at=at,
            knowledge_at=at,
            status="COMPLETED",
            outcome="OPPORTUNITIES_PRIORITIZED",
            code_version={},
            worker_id="test",
            created_by="test",
            started_at=at,
            finished_at=at,
        )
        session.add(run)
        session.flush()
        decision = AIDecisionRecord(
            run_id=run.id,
            outcome=outcome,
            rank=1 if outcome == "PRIORITIZE_FOR_REVIEW" else None,
            asset_id=asset,
            symbol="RSKA",
            direction="LONG",
            proposal_ids=[lead],
            policy_version_id=version.id,
            # Scores and expected values exist on the record; the risk engine never reads them.
            scores={"heuristic_score": 0.99, "calibrated_probability": 0.99},
            expected={"lead_proposal_id": str(lead), "expected_value": {"value": 1e9}},
            uncertainty={},
            explanation="TEST FIXTURE decision",
            decision_at=at,
            data_cutoff=at,
            expires_at=NOW + timedelta(hours=6),
            content_sha256="0" * 64,
        )
        session.add(decision)
        session.flush()
        return decision.id


def test_strategy_and_ai_proposals_contribute_levels_only(risk):
    account = risk.account()
    asset = risk.asset("RSKA")
    version = make_strategy(risk.factory, "momentum_fixture", StrategyFamily.MOMENTUM)
    lead = make_proposal(
        risk.factory,
        strategy_id="momentum_fixture",
        version_id=version,
        asset_id=asset,
        stored_at=NOW - timedelta(hours=1),
        cutoff=NOW - timedelta(hours=2),
        entry="99.70",
        strength="1.00",
        expires_at=NOW + timedelta(days=1),
    )
    decision = _decision(risk, asset, lead, "PRIORITIZE_FOR_REVIEW")
    with session_scope(risk.factory) as session:
        prop = proposals.from_ai_decision(session, decision)
    assert prop.entry == D("99.70")
    assert prop.stop == D("95.70")  # the lead proposal's invalidation level
    assert prop.ai_decision_id == decision
    assert prop.strategy_proposal_id == lead
    assert prop.expected_holding_days == 5
    out = risk.assess(account, prop)
    assert out.result is PASS
    with session_scope(risk.factory) as session:
        rec = session.get(RiskAssessmentRecord, out.assessment_id)
        assert rec is not None
        assert rec.ai_decision_id == decision
        assert rec.source.value == "AI_DECISION"
        assert "scores" not in str(rec.inputs["proposal"])
    # The same levels from the strategy proposal alone size identically: scores do nothing.
    with session_scope(risk.factory) as session:
        plain = proposals.from_strategy_proposal(session, lead)
    again = risk.assess(account, plain)
    assert again.permitted_quantity == out.permitted_quantity
    rejected = _decision(risk, asset, lead, "REJECT")
    with session_scope(risk.factory) as session, pytest.raises(InvalidRequestError):
        proposals.from_ai_decision(session, rejected)
