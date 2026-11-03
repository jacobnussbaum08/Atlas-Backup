"""Shared setup for the Strategy Laboratory integration tests (Phase 4A).

Everything here is TEST FIXTURE material:

* assets are invented instruments;
* daily bars are MOCK observations (constructed prices following a simple formula, not
  market data) written straight into ``market_bars`` with controllable availability;
* the strategies are FIXTURE strategies that exercise the engine. Their "logic" is a
  deliberately trivial rule (close versus a short SMA) and is not a trading strategy; the
  six real families have no logic in Phase 4A.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import Engine, insert
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import CALENDARS, SessionDay, completed_sessions
from app.core.clocks import FixedClock
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.database.models import Asset, AssetClass, AssetStatus, MarketBarRecord
from app.database.sessions import session_scope
from app.feature_engine.registry import FeatureRequest
from app.strategies import catalog
from app.strategies.base import (
    EvaluationContext,
    EvaluationSchedule,
    Evidence,
    ProposalDraft,
    Strategy,
    StrategyOutcome,
    StrategyParameters,
    typed,
)
from app.strategies.config import StrategySettings
from app.strategies.engine import EvaluationRequest, StrategyEngine
from app.strategies.enums import Direction, StrategyFamily, TriggerKind
from app.strategies.registry import StrategyRegistry

CAL = CALENDARS["XNYS"]
SETTLE = timedelta(hours=1)
# Tuesday 3 November 2026, 22:30 UTC: the session closed at 21:00 UTC (EST) and settled.
NOW = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)


def sessions(as_of: datetime, count: int) -> list[SessionDay]:
    return completed_sessions(CAL, as_of, count, SETTLE)


def midnight(day: SessionDay) -> datetime:
    return datetime.combine(day.day, clock_time(0), tzinfo=CAL.zone).astimezone(UTC)


def make_asset(
    factory: sessionmaker[Session], symbol: str, asset_class: AssetClass = AssetClass.EQUITY
) -> uuid.UUID:
    with session_scope(factory) as session:
        asset = Asset(
            id=uuid.uuid4(),
            symbol=symbol,
            asset_class=asset_class,
            name=f"{symbol} Fixture Corp",
            exchange_code="XNAS",
            quote_currency="USD",
            status=AssetStatus.ACTIVE,
        )
        session.add(asset)
        session.flush()
        return asset.id


def bar_close(index: int, start: float = 100.0, step: float = 0.5) -> Decimal:
    """MOCK price formula: a gentle trend with an alternating wiggle (never constant)."""
    wiggle = 0.3 if index % 2 else -0.3
    return Decimal(str(round(start + step * index + wiggle, 4)))


def seed_bars(
    factory: sessionmaker[Session],
    asset_id: uuid.UUID,
    *,
    as_of: datetime = NOW,
    count: int = 40,
    available_after_close: timedelta = timedelta(minutes=30),
    available_at: datetime | None = None,
    skip: Iterable[int] = (),
    quality: DataQualityStatus = DataQualityStatus.VALID,
    revision: int = 0,
    start: float = 100.0,
    step: float = 0.5,
    adjustment: PriceAdjustment = PriceAdjustment.SPLIT,
    volume: int | None = 1000,
) -> list[SessionDay]:
    """MOCK daily bars for the ``count`` sessions completed by ``as_of`` (oldest first).
    Each becomes available ``available_after_close`` after its close unless
    ``available_at`` fixes one time for all."""
    days = sessions(as_of, count)
    skipped = set(skip)
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        if index in skipped:
            continue
        close = bar_close(index, start, step)
        known = available_at or day.regular_close + available_after_close
        rows.append(
            {
                "asset_id": asset_id,
                "provider": "alpaca",
                "feed": "iex",
                "provider_symbol": "FIXTURE",
                "timeframe": "1Day",
                "adjustment": adjustment,
                "event_time": midnight(day),
                "revision": revision,
                "open": close - Decimal("0.5"),
                "high": close + Decimal(1),
                "low": close - Decimal(1),
                "close": close,
                "volume": None if volume is None else Decimal(volume + 10 * index),
                "vwap": None,
                "trade_count": 100,
                "is_complete": True,
                "source": DataSource.HISTORICAL_REST,
                "observation_kind": BarObservationKind.HISTORICAL,
                "quality_status": quality,
                "received_at": known,
                "available_at": known,
                "original_available_at": None,
                "processed_at": known,
                "ingested_at": known,
            }
        )
    if rows:
        with session_scope(factory) as session:
            session.execute(insert(MarketBarRecord), rows)
    return days


# --- Fixture strategies ------------------------------------------------------------------


class FixtureParameters(StrategyParameters):
    sma_length: int = 5


class ProposingStrategy(Strategy):
    """TEST FIXTURE: proposes LONG when the close is above a short SMA, else SHORT."""

    strategy_id: ClassVar[str] = "fixture_proposer"
    family = StrategyFamily.MOMENTUM
    name = "Fixture proposer"
    description = "TEST FIXTURE strategy: close versus SMA (not a trading strategy)."
    logic_version: ClassVar[str] = "fixture-proposer-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY,)
    supported_timeframes = ("1Day", "1Week")
    Parameters: ClassVar[type[StrategyParameters]] = FixtureParameters

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, FixtureParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("sma", timeframe, {"length": p.sma_length}),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, FixtureParameters)
        close = ctx.feature("close")
        sma = ctx.feature("sma", length=p.sma_length)
        assert isinstance(close.value, float)
        assert isinstance(sma.value, float)
        entry = Decimal(str(close.value)).quantize(Decimal("0.0001"))
        long = close.value >= sma.value
        direction = Direction.LONG if long else Direction.SHORT
        if direction not in p.directions:
            return StrategyOutcome(notes=("direction not enabled",))
        offset = entry * Decimal("0.05")
        return StrategyOutcome(
            proposals=(
                ProposalDraft(
                    direction=direction,
                    reference_entry=entry,
                    invalidation_level=entry - offset if long else entry + offset,
                    target_reference=entry + offset if long else entry - offset,
                    signal_strength=Decimal("0.5"),
                    reasoning="TEST FIXTURE: close compared with its SMA.",
                    evidence=(
                        Evidence(close.key, "trigger", "newest completed close"),
                        Evidence(sma.key, "context", "short moving average"),
                    ),
                ),
            )
        )


class FailingStrategy(ProposingStrategy):
    strategy_id: ClassVar[str] = "fixture_failing"
    logic_version: ClassVar[str] = "fixture-failing-1"
    description = "TEST FIXTURE strategy that always raises."

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        raise RuntimeError("fixture failure token=abc123")


class UntraceableStrategy(ProposingStrategy):
    strategy_id: ClassVar[str] = "fixture_untraceable"
    logic_version: ClassVar[str] = "fixture-untraceable-1"
    description = "TEST FIXTURE strategy citing evidence it was not given."

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        draft = super().evaluate(ctx, params).proposals[0]
        bogus = Evidence("rsi.v1(length=14)@1Day", "trigger", "not in the snapshot")
        return StrategyOutcome(
            proposals=(
                ProposalDraft(
                    direction=draft.direction,
                    reference_entry=draft.reference_entry,
                    signal_strength=draft.signal_strength,
                    reasoning=draft.reasoning,
                    evidence=(bogus,),
                ),
            )
        )


class SlowStrategy(ProposingStrategy):
    strategy_id: ClassVar[str] = "fixture_slow"
    logic_version: ClassVar[str] = "fixture-slow-1"
    description = "TEST FIXTURE strategy that takes longer than the timeout."
    delay = 3.0

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        time.sleep(self.delay)
        return super().evaluate(ctx, params)


class NewsFixtureStrategy(ProposingStrategy):
    """TEST FIXTURE: requires a news count, so missing news coverage blocks it."""

    strategy_id: ClassVar[str] = "fixture_news"
    logic_version: ClassVar[str] = "fixture-news-1"
    description = "TEST FIXTURE strategy that needs news coverage."
    supported_timeframes = ("1Day",)
    schedule = EvaluationSchedule(
        (TriggerKind.BAR_CLOSE, TriggerKind.NEWS_EVENT), "bars and news (fixture)"
    )

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        return [
            *super().required_features(params, timeframe),
            FeatureRequest("news_event_count", timeframe, {"lookback_hours": 24}),
        ]


@dataclass
class Lab:
    factory: sessionmaker[Session]
    clock: FixedClock
    registry: StrategyRegistry
    engine: StrategyEngine

    def version_id(self, strategy_id: str) -> uuid.UUID:
        with session_scope(self.factory) as session:
            version = catalog.latest_version(session, strategy_id)
            assert version is not None
            return version.id

    def enable(self, strategy_id: str, enabled: bool = True) -> None:
        with session_scope(self.factory) as session:
            catalog.set_status(
                session,
                self.registry.get(strategy_id),
                enabled=enabled,
                reason="test",
                actor="test",
            )


def evaluation_request(
    lab: Lab, asset_id: uuid.UUID, strategy_id: str = "fixture_proposer", **overrides: Any
) -> EvaluationRequest:
    values: dict[str, Any] = {
        "strategy_id": strategy_id,
        "strategy_version_id": lab.version_id(strategy_id),
        "asset_id": asset_id,
        "timeframe": "1Day",
        "trigger": TriggerKind.BAR_CLOSE,
        "trigger_ref": "1Day:2026-11-03",
    } | overrides
    return EvaluationRequest(**values)


def make_lab(
    engine: Engine,
    strategies: Sequence[Strategy] = (ProposingStrategy(),),
    *,
    now: datetime = NOW,
    enable: bool = True,
    worker_id: str = "test-worker",
    **settings: Any,
) -> Lab:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = FixedClock(now)
    registry = StrategyRegistry(strategies)
    values: dict[str, Any] = {"evaluation_timeout_seconds": 5, "lease_seconds": 60} | settings
    strategy_settings = StrategySettings(_env_file=None, **values)
    lab_engine = StrategyEngine(
        factory,
        registry,
        strategy_settings,
        clock,
        provider="alpaca",
        feed="iex",
        worker_id=worker_id,
    )
    with session_scope(factory) as session:
        catalog.sync_catalog(session, registry)
    lab = Lab(factory, clock, registry, lab_engine)
    if enable:
        for strategy in registry:
            if strategy.implemented:
                lab.enable(strategy.strategy_id)
    return lab


__all__ = [
    "CAL",
    "NOW",
    "FailingStrategy",
    "Lab",
    "NewsFixtureStrategy",
    "ProposingStrategy",
    "SlowStrategy",
    "UntraceableStrategy",
    "bar_close",
    "evaluation_request",
    "make_asset",
    "make_lab",
    "midnight",
    "seed_bars",
    "sessions",
]
