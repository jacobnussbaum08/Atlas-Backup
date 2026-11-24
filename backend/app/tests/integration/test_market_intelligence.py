"""Market intelligence runs against real PostgreSQL (Phase 3B).

Instruments come from the TEST FIXTURE directory (``FakeAssetDirectory``); daily bars are
MOCK observations written straight into ``market_bars`` with controlled ingestion and
availability times; universe membership is written as membership events. No provider.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from app.asset_discovery import membership
from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.asset_discovery.sessions import calendar_for, completed_sessions, trading_day
from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import BarObservationKind, DataQualityStatus, DataSource, PriceAdjustment
from app.database.models import (
    CrossAssetRelationshipRecord,
    IntelligenceRunRecord,
    MarketBarRecord,
    MarketFeatureSnapshotRecord,
    MarketSummaryRecord,
    RegimeClassificationRecord,
)
from app.database.sessions import session_scope
from app.market_intelligence.config import MarketIntelligenceSettings
from app.market_intelligence.enums import KnowledgeBasis, RunKind, RunStatus, Scope
from app.market_intelligence.service import (
    STALE_AFTER,
    MarketIntelligenceService,
    RunInProgressError,
)
from app.tests.fixtures.alpaca_assets import asset_record
from app.tests.integration.asset_discovery_support import make_env

pytestmark = pytest.mark.integration

CAL = calendar_for("XNYS")
# Tuesday 3 November 2026 22:00 UTC: that day's session closed at 21:00 and has settled.
NOW = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)
MEMBERS = [f"M{i:02d}" for i in range(22)]


def midnight(day) -> datetime:
    return datetime.combine(day, time(0), tzinfo=CAL.zone).astimezone(UTC)


def path(symbol: str, count: int, drift: float) -> list[float]:
    """MOCK closes: a drift plus a deterministic, symbol-specific wiggle."""
    phase = sum(map(ord, symbol)) % 17
    return [
        100.0 * math.exp(drift * i + 0.01 * math.sin((i + phase) * 1.3) + 0.004 * math.cos(i * 0.7))
        for i in range(count)
    ]


def seed(
    env,
    symbol: str,
    closes: list[float],
    *,
    known: datetime,
    revision: int = 0,
    sessions=None,
    ingested: datetime | None = None,
) -> None:
    """MOCK daily bars (split-adjusted series) for the last len(closes) completed sessions."""
    days = sessions or completed_sessions(CAL, NOW, len(closes))
    asset_id = env.asset_id(symbol)
    rows = []
    for day, close in zip(days, closes, strict=True):
        price = Decimal(str(round(close, 4)))
        rows.append(
            {
                "asset_id": asset_id,
                "provider": "alpaca",
                "feed": "iex",
                "provider_symbol": symbol,
                "timeframe": "1Day",
                "adjustment": PriceAdjustment.SPLIT,
                "event_time": midnight(day.day),
                "revision": revision,
                "open": price,
                "high": price * Decimal("1.01"),
                "low": price * Decimal("0.99"),
                "close": price,
                "volume": Decimal(500_000),
                "vwap": price,
                "trade_count": 100,
                "is_complete": True,
                "source": DataSource.HISTORICAL_REST,
                "observation_kind": BarObservationKind.HISTORICAL,
                "quality_status": DataQualityStatus.VALID,
                "received_at": known,
                "available_at": known,
                "original_available_at": None,
                "processed_at": known,
                "ingested_at": ingested or known,
            }
        )
    with session_scope(env.factory) as session:
        session.execute(insert(MarketBarRecord), rows)


def join(env, symbols, at: datetime, layer=UniverseLayer.QUALIFIED_UNIVERSE) -> None:
    with session_scope(env.factory) as session:
        for symbol in symbols:
            membership.transition(
                session,
                asset_id=env.asset_id(symbol),
                layer=layer,
                action=MembershipAction.ADDED,
                effective_at=at,
                recorded_at=at,
                reason_code="TEST",
                reason="MOCK membership",
            )


@pytest.fixture
def env(fresh_engine):
    env = make_env(fresh_engine, now=NOW - timedelta(days=400))
    env.directory.set(*(asset_record(s) for s in ["SPY", *MEMBERS]))
    env.discover()
    return env


def service(env, clock: FixedClock, **settings) -> MarketIntelligenceService:
    values = {"prepare_history": False} | settings
    return MarketIntelligenceService(
        env.factory,
        env.service._provider,
        MarketDataSettings(_env_file=None),
        MarketIntelligenceSettings(_env_file=None, **values),
        clock,
    )


def market(env, run_id, timeframe="1Day") -> dict[str, RegimeClassificationRecord]:
    with session_scope(env.factory) as session:
        rows = session.scalars(
            select(RegimeClassificationRecord).where(
                RegimeClassificationRecord.run_id == run_id,
                RegimeClassificationRecord.scope == Scope.MARKET,
                RegimeClassificationRecord.timeframe == timeframe,
            )
        ).all()
        for r in rows:
            session.expunge(r)
        return {r.dimension.value: r for r in rows}


def snapshot(env, run_id, symbol, timeframe="1Day") -> MarketFeatureSnapshotRecord:
    with session_scope(env.factory) as session:
        row = session.scalars(
            select(MarketFeatureSnapshotRecord).where(
                MarketFeatureSnapshotRecord.run_id == run_id,
                MarketFeatureSnapshotRecord.asset_id == env.asset_id(symbol),
                MarketFeatureSnapshotRecord.timeframe == timeframe,
            )
        ).one()
        session.expunge(row)
        return row


def populated(env, *, drift=0.002, count=300, known=None):
    known = known or NOW - timedelta(hours=2)
    seed(env, "SPY", path("SPY", count, drift), known=known)
    for i, symbol in enumerate(MEMBERS):
        seed(env, symbol, path(symbol, count, drift * (1 if i % 4 else -0.5)), known=known)
    join(env, MEMBERS, NOW - timedelta(days=30))
    join(env, MEMBERS[:5], NOW - timedelta(days=30), UniverseLayer.ACTIVE_WATCHLIST)


# ------------------------------------------------------------------ live runs
def test_live_run_end_to_end(env):
    populated(env)
    svc = service(env, FixedClock(NOW))
    run = asyncio.run(svc.run(RunKind.LIVE, requested_by="test"))
    assert run.status is RunStatus.COMPLETED, run.error
    assert run.basis is KnowledgeBasis.RECORDED
    assert run.as_of_start == run.as_of_end == NOW

    rows = market(env, run.id)
    assert set(rows) == {"TREND", "VOLATILITY", "STABILITY"}
    trend = rows["TREND"]
    assert trend.label == "BULLISH"
    assert sum(Decimal(v) for v in trend.probabilities.values()) == pytest.approx(
        Decimal(1), abs=Decimal("1e-6")
    )
    assert trend.algorithm == "trend-evidence-v1"
    assert trend.available_at >= trend.as_of
    assert set(market(env, run.id, "1Week")) == {"TREND", "VOLATILITY"}

    spy = snapshot(env, run.id, "SPY")
    assert spy.quality.value == "OK"
    assert spy.last_event_at is not None
    assert spy.last_event_at < NOW
    assert spy.inputs_available_at is not None
    assert spy.inputs_available_at <= NOW  # RECORDED: every input known by the decision time
    assert spy.feature_set_version == "features-v1"
    assert spy.adjustment is PriceAdjustment.SPLIT

    with session_scope(env.factory) as session:
        summary = session.scalars(
            select(MarketSummaryRecord).where(MarketSummaryRecord.run_id == run.id)
        ).one()
        assert summary.participation_status == "OK"
        assert summary.members_total == summary.members_evaluated == len(MEMBERS)
        assert summary.participation["advancers"] is not None
        assert any("not market-wide breadth" in t for t in summary.limitations)
        assert any("single venue" in t for t in summary.limitations)
        assert summary.cross_asset["pairs_computed"] == len(MEMBERS) * (len(MEMBERS) - 1) // 2
        pairs = session.scalar(
            select(func.count())
            .select_from(CrossAssetRelationshipRecord)
            .where(CrossAssetRelationshipRecord.run_id == run.id)
        )
        assert pairs == len(MEMBERS) * (len(MEMBERS) - 1) // 2 + len(MEMBERS)


def test_bars_ingested_after_the_decision_time_are_invisible(env):
    populated(env)
    # Five newer sessions' bars exist but were stored only after NOW.
    seed(
        env,
        "SPY",
        path("SPY", 5, 0.05),
        known=NOW + timedelta(hours=1),
        revision=1,
        sessions=completed_sessions(CAL, NOW, 5),
    )
    svc = service(env, FixedClock(NOW))
    run = asyncio.run(svc.run(RunKind.LIVE, requested_by="test"))
    before = snapshot(env, run.id, "SPY")
    # Same decision time, later run: identical (the revision-1 bars are still unknown then).
    again = asyncio.run(service(env, FixedClock(NOW)).run(RunKind.LIVE, requested_by="test"))
    assert snapshot(env, again.id, "SPY").feature_values == before.feature_values
    # Two hours later the revisions are known and the result changes.
    later = asyncio.run(
        service(env, FixedClock(NOW + timedelta(hours=2))).run(RunKind.LIVE, requested_by="test")
    )
    assert snapshot(env, later.id, "SPY").feature_values != before.feature_values


def test_unfinished_sessions_are_never_used(env):
    intraday = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)  # Tuesday, during the session
    populated(env, known=datetime(2026, 11, 3, 6, 0, tzinfo=UTC))
    # Today's bar is already stored (a running bar), but its session has not completed.
    tuesday = trading_day(CAL, date(2026, 11, 3))
    assert tuesday is not None
    today = [tuesday]
    seed(env, "SPY", [500.0], known=intraday - timedelta(minutes=1), sessions=today, revision=1)
    run = asyncio.run(service(env, FixedClock(intraday)).run(RunKind.LIVE, requested_by="t"))
    spy = snapshot(env, run.id, "SPY")
    assert spy.last_event_at == midnight(completed_sessions(CAL, intraday, 1)[-1].day)
    assert spy.last_event_at < midnight(today[0].day)


def test_a_later_correction_never_rewrites_stored_intelligence(env):
    populated(env)
    run = asyncio.run(service(env, FixedClock(NOW)).run(RunKind.LIVE, requested_by="t"))
    original = snapshot(env, run.id, "SPY")
    # Correct the newest SPY bar after the run.
    last = completed_sessions(CAL, NOW, 1)
    seed(env, "SPY", [10.0], known=NOW + timedelta(minutes=30), revision=1, sessions=last)
    with pytest.raises(DBAPIError), session_scope(env.factory) as session:
        session.execute(
            update(RegimeClassificationRecord)
            .where(RegimeClassificationRecord.run_id == run.id)
            .values(label="BEARISH")
        )
    assert snapshot(env, run.id, "SPY").feature_values == original.feature_values
    corrected = asyncio.run(
        service(env, FixedClock(NOW + timedelta(hours=1))).run(RunKind.LIVE, requested_by="t")
    )
    assert (
        snapshot(env, corrected.id, "SPY").feature_values["return_1"]
        != original.feature_values["return_1"]
    )


def test_universe_membership_is_point_in_time(env):
    seed(env, "SPY", path("SPY", 300, 0.002), known=NOW - timedelta(days=60))
    for symbol in MEMBERS:
        seed(
            env,
            symbol,
            path(symbol, 300, 0.002),
            known=NOW - timedelta(days=60),
            sessions=completed_sessions(CAL, NOW - timedelta(days=50), 300),
        )
    joined = NOW - timedelta(days=10)
    join(env, MEMBERS, joined)
    early = asyncio.run(
        service(env, FixedClock(joined - timedelta(days=1))).run(RunKind.LIVE, requested_by="t")
    )
    late = asyncio.run(
        service(env, FixedClock(joined + timedelta(days=1))).run(RunKind.LIVE, requested_by="t")
    )
    with session_scope(env.factory) as session:
        summaries = {s.run_id: s for s in session.scalars(select(MarketSummaryRecord))}
    assert summaries[early.id].members_total == 0  # not yet members: never today's universe
    assert summaries[early.id].participation_status == "INSUFFICIENT_DATA"
    assert summaries[late.id].members_total == len(MEMBERS)


def test_insufficient_benchmark_history_is_uncertain(env):
    seed(env, "SPY", path("SPY", 40, 0.002), known=NOW - timedelta(hours=2))
    run = asyncio.run(service(env, FixedClock(NOW)).run(RunKind.LIVE, requested_by="t"))
    assert run.status is RunStatus.PARTIALLY_COMPLETED
    assert "benchmark daily history insufficient" in (run.error or "")
    rows = market(env, run.id)
    assert rows["TREND"].label == "UNCERTAIN"
    assert Decimal(rows["TREND"].probabilities["UNCERTAIN"]) == 1
    assert rows["TREND"].quality.value == "INSUFFICIENT_DATA"
    assert rows["STABILITY"].label == "UNCERTAIN"
    assert snapshot(env, run.id, "SPY").feature_values == {}


# ------------------------------------------------------------------ reconstruction
def test_reconstruction_uses_only_sessions_completed_by_each_decision_time(env):
    # History retrieved after every decision time (as a reconstruction is), then the run.
    seed(env, "SPY", path("SPY", 320, 0.002), known=NOW + timedelta(hours=1))
    run = asyncio.run(
        service(env, FixedClock(NOW + timedelta(hours=2))).run(
            RunKind.RECONSTRUCTION, requested_by="t", params={"sessions": 5}
        )
    )
    assert run.status is RunStatus.COMPLETED, run.error
    assert run.basis is KnowledgeBasis.RECONSTRUCTED
    with session_scope(env.factory) as session:
        rows = session.scalars(
            select(MarketFeatureSnapshotRecord)
            .where(
                MarketFeatureSnapshotRecord.run_id == run.id,
                MarketFeatureSnapshotRecord.timeframe == "1Day",
            )
            .order_by(MarketFeatureSnapshotRecord.as_of)
        ).all()
        assert len(rows) == 5
        for row in rows:
            assert row.basis is KnowledgeBasis.RECONSTRUCTED
            assert row.last_event_at is not None
            assert row.inputs_available_at is not None
            assert row.last_event_at < row.as_of  # no bar of an unfinished session
            # The bars were retrieved after the decision time: that is what RECONSTRUCTED says.
            assert row.inputs_available_at > row.as_of
        assert len({r.last_event_at for r in rows}) == 5  # one more session each time
    assert run.as_of_start < run.as_of_end


# ------------------------------------------------------------------ concurrency and recovery
def test_one_run_at_a_time_and_stale_runs_are_reclaimed(env):
    clock = FixedClock(NOW)
    svc = service(env, clock)
    first = svc.create_run(RunKind.LIVE, requested_by="a")
    with pytest.raises(RunInProgressError):
        svc.create_run(RunKind.LIVE, requested_by="b")
    clock.advance(STALE_AFTER + timedelta(seconds=1))
    second = svc.create_run(RunKind.LIVE, requested_by="b")  # the first was orphaned
    assert svc.get_run(first).status is RunStatus.FAILED
    assert "interrupted" in (svc.get_run(first).error or "")
    assert svc.get_run(second).status is RunStatus.RUNNING


def test_a_reclaimed_run_commits_nothing(env, monkeypatch):
    populated(env)
    svc = service(env, FixedClock(NOW))
    run_id = svc.create_run(RunKind.LIVE, requested_by="t")
    original = svc.load

    def reclaimed_meanwhile(*args, **kwargs):
        with session_scope(env.factory) as session:
            session.execute(
                update(IntelligenceRunRecord)
                .where(IntelligenceRunRecord.id == run_id)
                .values(
                    status=RunStatus.FAILED, error="reclaimed by another process", completed_at=NOW
                )
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "load", reclaimed_meanwhile)
    run = asyncio.run(svc.execute(run_id))
    assert run.status is RunStatus.FAILED
    assert run.error == "reclaimed by another process"  # never overwritten
    with session_scope(env.factory) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RegimeClassificationRecord)
                .where(RegimeClassificationRecord.run_id == run_id)
            )
            == 0
        )


def test_result_tables_are_append_only(env):
    populated(env)
    run = asyncio.run(service(env, FixedClock(NOW)).run(RunKind.LIVE, requested_by="t"))
    for table in (
        "market_feature_snapshots",
        "regime_classifications",
        "market_summaries",
        "cross_asset_relationships",
        "intelligence_configs",
    ):
        with pytest.raises(DBAPIError), session_scope(env.factory) as session:
            session.execute(text(f"DELETE FROM {table}"))  # fixed table names
    assert run.id is not None
    assert uuid.UUID(str(run.id))
