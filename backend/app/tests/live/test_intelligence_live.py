"""REAL Alpaca market intelligence run (opt-in, Phase 3B). Uses the credentials in ``.env``.

Retrieves real split-adjusted daily history for SPY (benchmark) and AAPL from Alpaca's
market-data API, then computes and stores market intelligence in a throw-away database.
AAPL's qualified-universe membership is TEST SETUP (a membership event written here):
real qualification needs the 3A pipeline. Never calls trading endpoints; prints labels and
counts only.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.asset_discovery import membership
from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.core.clocks import SystemClock
from app.core.config import MarketDataSettings
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.database.models import (
    Asset,
    CrossAssetRelationshipRecord,
    IngestionJobRecord,
    MarketFeatureSnapshotRecord,
    RegimeClassificationRecord,
)
from app.database.sessions import session_scope
from app.market_intelligence.config import MarketIntelligenceSettings
from app.market_intelligence.enums import KnowledgeBasis, RunKind, RunStatus, Scope
from app.market_intelligence.service import MarketIntelligenceService

pytestmark = [pytest.mark.live, pytest.mark.integration]


@pytest.fixture
def setup(fresh_engine):
    alpaca = get_alpaca_settings()  # reads the real local .env
    if not alpaca.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    clock = SystemClock()
    provider = AlpacaMarketDataProvider(alpaca, clock, max_retries=3, max_pages=50)
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    now = clock.now()
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
        aapl = session.scalars(select(Asset.id).where(Asset.symbol == "AAPL")).one()
        membership.transition(  # TEST SETUP, not a qualification result
            session,
            asset_id=aapl,
            layer=UniverseLayer.QUALIFIED_UNIVERSE,
            action=MembershipAction.ADDED,
            effective_at=now - timedelta(days=1),
            recorded_at=now - timedelta(days=1),
            reason_code="LIVE_TEST",
            reason="live test setup",
        )
    market = MarketDataSettings(_env_file=None)
    service = MarketIntelligenceService(
        factory,
        provider,
        market,
        MarketIntelligenceSettings(_env_file=None),
        clock,
        ingestion=HistoricalIngestionService(factory, provider, market, clock),
    )
    return factory, service


def test_real_live_and_reconstructed_market_intelligence(setup):
    factory, service = setup
    run = asyncio.run(service.run(RunKind.LIVE, requested_by="live-test"))
    assert run.status is RunStatus.COMPLETED, run.error
    preparation = run.summary["history_preparation"]
    assert preparation["adjustment"] == "SPLIT"
    assert all(j.get("status") == "COMPLETED" for j in preparation["jobs"])

    with session_scope(factory) as session:
        jobs = session.scalars(select(IngestionJobRecord)).all()
        assert {j.adjustment.value for j in jobs} == {"SPLIT"}  # the series qualified on
        spy = session.scalars(
            select(MarketFeatureSnapshotRecord)
            .join(Asset, Asset.id == MarketFeatureSnapshotRecord.asset_id)
            .where(
                MarketFeatureSnapshotRecord.run_id == run.id,
                Asset.symbol == "SPY",
                MarketFeatureSnapshotRecord.timeframe == "1Day",
            )
        ).one()
        assert spy.quality.value in ("OK", "PARTIAL")
        assert spy.last_event_at is not None
        assert spy.last_event_at < run.as_of_end
        assert spy.inputs_available_at is not None
        assert spy.inputs_available_at <= run.as_of_end
        rows = session.scalars(
            select(RegimeClassificationRecord).where(
                RegimeClassificationRecord.run_id == run.id,
                RegimeClassificationRecord.scope == Scope.MARKET,
            )
        ).all()
        found = {(r.timeframe, r.dimension.value): r for r in rows}
        assert ("1Day", "TREND") in found
        assert ("1Day", "STABILITY") in found
        for r in rows:
            total = sum(Decimal(v) for v in r.probabilities.values() if v is not None)
            assert total == pytest.approx(Decimal(1), abs=Decimal("1e-6"))
        relation = session.scalars(
            select(CrossAssetRelationshipRecord).where(
                CrossAssetRelationshipRecord.run_id == run.id
            )
        ).one()  # AAPL against SPY
        print(
            "[live] market:",
            {f"{k[0]}:{k[1]}": (v.label, str(v.confidence)) for k, v in sorted(found.items())},
            "| SPY daily quality:",
            spy.quality.value,
            "| AAPL vs SPY:",
            relation.status,
            str(relation.correlation),
            str(relation.beta),
        )

    rebuilt = asyncio.run(
        service.run(RunKind.RECONSTRUCTION, requested_by="live-test", params={"sessions": 5})
    )
    assert rebuilt.status is RunStatus.COMPLETED, rebuilt.error
    with session_scope(factory) as session:
        past = session.scalars(
            select(RegimeClassificationRecord).where(
                RegimeClassificationRecord.run_id == rebuilt.id,
                RegimeClassificationRecord.scope == Scope.MARKET,
                RegimeClassificationRecord.timeframe == "1Day",
                RegimeClassificationRecord.dimension == "TREND",
            )
        ).all()
        assert len(past) == 5
        assert {r.basis for r in past} == {KnowledgeBasis.RECONSTRUCTED}
        print(
            "[live] reconstructed daily trend:",
            [(r.as_of.date().isoformat(), r.label) for r in sorted(past, key=lambda r: r.as_of)],
        )
