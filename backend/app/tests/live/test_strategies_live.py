"""REAL-data smoke test of the six Phase 4B strategies (opt-in). Uses the credentials in
``.env`` for Alpaca's market-data API only (never trading endpoints).

Real split-adjusted daily history for SPY (the momentum benchmark) and AAPL is retrieved
through the Phase 3B market-intelligence run into a throw-away database; then each
strategy is evaluated through the engine on both assets. TEST SETUP (written here, not
real results): AAPL's qualified-universe membership and a provider reference version for
each asset (the eligibility source normally written by Phase 3A discovery).

What is checked is that real data flows end to end without failures and with honest
states; whether any strategy proposes anything depends on the market and is only printed.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

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
from app.database.models import Asset
from app.database.sessions import session_scope
from app.market_intelligence.config import MarketIntelligenceSettings
from app.market_intelligence.enums import RunKind, RunStatus
from app.market_intelligence.service import MarketIntelligenceService
from app.strategies import catalog
from app.strategies.config import StrategySettings
from app.strategies.engine import EvaluationRequest, StrategyEngine
from app.strategies.enums import SignalState, TriggerKind
from app.strategies.registry import default_registry
from app.tests.integration.strategy_support import add_reference

pytestmark = [pytest.mark.live, pytest.mark.integration]


def test_six_strategies_on_real_history(fresh_engine):
    alpaca = get_alpaca_settings()  # reads the real local .env
    if not alpaca.credentials_configured:
        pytest.skip("Alpaca credentials are not configured in .env")
    clock = SystemClock()
    provider = AlpacaMarketDataProvider(alpaca, clock, max_retries=3, max_pages=50)
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    now = clock.now()
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
        assets = {row.symbol: row.id for row in session.execute(select(Asset.symbol, Asset.id))}
        for symbol, asset_id in assets.items():  # TEST SETUP eligibility source
            add_reference(session, asset_id, symbol, observed_at=now - timedelta(days=1))
        membership.transition(  # TEST SETUP, not a qualification result
            session,
            asset_id=assets["AAPL"],
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
    run = asyncio.run(service.run(RunKind.LIVE, requested_by="live-test"))
    assert run.status is RunStatus.COMPLETED, run.error

    registry = default_registry()
    engine = StrategyEngine(
        factory,
        registry,
        StrategySettings(_env_file=None),
        clock,
        provider=provider.provider_id,
        feed=provider.feed,
    )
    with session_scope(factory) as session:
        catalog.sync_catalog(session, registry)
        versions = {s.strategy_id: catalog.latest_version(session, s.strategy_id) for s in registry}
    requests = [
        EvaluationRequest(
            strategy_id=sid,
            strategy_version_id=version.id,
            asset_id=assets[symbol],
            timeframe="1Day",
            trigger=TriggerKind.MANUAL,
            trigger_ref="live-smoke",
        )
        for sid, version in versions.items()
        if version is not None
        for symbol in ("SPY", "AAPL")
    ]
    results = asyncio.run(engine.evaluate_many(requests))
    states = {
        (r.request.strategy_id, "SPY" if r.request.asset_id == assets["SPY"] else "AAPL"): r
        for r in results
    }
    for key, result in sorted(states.items()):
        print("[live]", key, result.status, result.signal_state, len(result.proposal_ids))
        assert result.outcome == "EVALUATED"
        assert result.signal_state is not SignalState.FAILED, result.error
        assert result.signal_state is not SignalState.INELIGIBLE_ASSET
    # Real price history was enough for every bar-based strategy on SPY; event-driven needs
    # news coverage this test does not retrieve, so it must report UNAVAILABLE, not a signal.
    assert states["event_driven", "SPY"].signal_state is SignalState.UNAVAILABLE_DATA
    for sid in ("momentum", "trend_following", "mean_reversion", "breakout", "volatility"):
        assert states[sid, "SPY"].signal_state in (SignalState.SIGNAL, SignalState.NO_SIGNAL)
