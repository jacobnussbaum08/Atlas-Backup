"""TEST FIXTURE builders for decision-coordinator inputs (Phase 5B).

Everything here is invented: assets, proposals, regimes, predictions and analyses are
constructed values for exercising the coordinator's rules. None of it is market data or
a real model or LLM output.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.ai_engine.coordinator.bundle import (
    AssetFeaturesInput,
    AssetRegimeInput,
    CorrelationInput,
    InputBundle,
    LLMAnalysisInput,
    MLPredictionInput,
    ProposalInput,
    RegimeInput,
    TrackRecordInput,
)
from app.ai_engine.enums import DecisionMode
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import Direction

DECISION_AT = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
FAMILIES = {
    "momentum": "MOMENTUM",
    "trend_following": "TREND_FOLLOWING",
    "mean_reversion": "MEAN_REVERSION",
    "breakout": "BREAKOUT",
    "volatility": "VOLATILITY",
    "event_driven": "EVENT_DRIVEN",
}
ASSETS: dict[str, uuid.UUID] = {}
_regime_ids = iter(range(1, 10_000))


def asset(symbol: str) -> uuid.UUID:
    return ASSETS.setdefault(symbol, uuid.uuid5(uuid.NAMESPACE_DNS, f"fixture-{symbol}"))


def proposal(
    symbol: str = "AAA",
    strategy: str = "momentum",
    direction: Direction = Direction.LONG,
    *,
    strength: str = "0.70",
    entry: str = "100",
    stop: str | None = None,
    target: str | None = None,
    timeframe: str = "1Day",
    cutoff: datetime | None = None,
    valid: int = 10,
    features: int = 10,
    basis: KnowledgeBasis = KnowledgeBasis.RECORDED,
    seed: str = "",
) -> ProposalInput:
    sign = 1 if direction is Direction.LONG else -1
    e = Decimal(entry)
    stop_d = Decimal(stop) if stop is not None else e - sign * Decimal(4)
    target_d = Decimal(target) if target is not None else e + sign * Decimal(10)
    cut = cutoff or DECISION_AT - timedelta(hours=2)
    return ProposalInput(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"p-{symbol}-{strategy}-{direction}-{timeframe}-{seed}"),
        strategy_id=strategy,
        family=FAMILIES[strategy],
        strategy_version_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"v-{strategy}"),
        strategy_version=1,
        logic_version=f"{strategy}-1",
        configuration_sha256="c" * 64,
        asset_id=asset(symbol),
        symbol=symbol,
        market="EQUITY",
        direction=direction,
        timeframe=timeframe,
        mode="LIVE",
        basis=basis,
        signal_at=cut,
        data_cutoff=cut,
        available_at=cut + timedelta(minutes=5),
        expires_at=DECISION_AT + timedelta(days=2),
        reference_entry=e,
        invalidation_level=stop_d,
        target_reference=target_d,
        holding_horizon_periods=5,
        signal_strength=Decimal(strength),
        strength_kind="UNCALIBRATED_SCORE",
        feature_snapshot_ids=[uuid.uuid5(uuid.NAMESPACE_DNS, f"s-{symbol}-{strategy}")],
        snapshot_feature_count=features,
        snapshot_valid_count=valid,
        snapshot_basis=basis,
        inputs_available_at=cut,
        evidence_count=3,
    )


def market_regime(dimension: str, label: str, timeframe: str = "1Day") -> RegimeInput:
    return RegimeInput(
        id=next(_regime_ids),
        scope="MARKET",
        asset_id=None,
        timeframe=timeframe,
        dimension=dimension,
        label=label,
        confidence=0.7,
        quality="OK",
        basis=KnowledgeBasis.RECORDED,
        as_of=DECISION_AT - timedelta(hours=3),
        available_at=DECISION_AT - timedelta(hours=2),
        algorithm="fixture",
    )


def asset_regime(symbol: str, dimension: str, label: str) -> AssetRegimeInput:
    return AssetRegimeInput(
        **{
            **market_regime(dimension, label).model_dump(),
            "id": next(_regime_ids),
            "scope": "ASSET",
            "asset_id": asset(symbol),
        }
    )


def features(
    symbol: str,
    *,
    close_vs_sma_slow: float | None = 0.05,
    slow_slope_t: float | None = 3.0,
    relative_strength: float | None = 0.02,
    volatility_percentile: float | None = 0.5,
    median_dollar_volume: float | None = 50_000_000,
) -> AssetFeaturesInput:
    return AssetFeaturesInput(
        asset_id=asset(symbol),
        timeframe="1Day",
        snapshot_id=1,
        feature_set_version="features-v1",
        features={
            "close_vs_sma_slow": close_vs_sma_slow,
            "slow_slope_t": slow_slope_t,
            "relative_strength": relative_strength,
            "volatility_percentile": volatility_percentile,
        },
        quality="OK",
        basis=KnowledgeBasis.RECORDED,
        as_of=DECISION_AT - timedelta(hours=3),
        available_at=DECISION_AT - timedelta(hours=2),
        liquidity_metric_id=1,
        median_dollar_volume=median_dollar_volume,
        liquidity_as_of=DECISION_AT - timedelta(days=1),
    )


def correlation(a: str, b: str, value: float, ident: int = 1) -> CorrelationInput:
    return CorrelationInput(
        id=ident,
        asset_a=asset(a),
        asset_b=asset(b),
        correlation=value,
        window=60,
        observations=60,
        as_of=DECISION_AT - timedelta(hours=3),
        available_at=DECISION_AT - timedelta(hours=2),
    )


def prediction(
    symbol: str = "AAA",
    direction: Direction = Direction.LONG,
    *,
    status: str = "APPROVED",
    output_kind: str = "CALIBRATED_PROBABILITY",
    output: dict[str, Any] | None = None,
    target_kind: str = "BARRIER",
    barrier_source: str | None = "STRATEGY_LEVELS",
    strategy_id: str | None = "momentum",
    validation_status: str = "PASSED",
    test_metrics: dict[str, Any] | None = None,
    test_baseline: dict[str, Any] | None = None,
    name: str = "fixture-model",
    ece: float | None = 0.02,
) -> MLPredictionInput:
    return MLPredictionInput(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"pred-{symbol}-{direction}-{name}-{status}"),
        model_version_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"model-{name}"),
        model_name=name,
        model_version=1,
        model_type="XGBOOST",
        status_at_decision=status,
        mode="APPROVED" if status == "APPROVED" else "SHADOW",
        asset_id=asset(symbol),
        direction=direction,
        timeframe="1Day",
        strategy_id=strategy_id,
        decision_at=DECISION_AT - timedelta(minutes=30),
        predicted_at=DECISION_AT - timedelta(minutes=20),
        output_kind=output_kind,
        output=output if output is not None else {"raw_score": 0.62, "probability": 0.6},
        feature_snapshot_sha256="f" * 64,
        validation_status=validation_status,
        target_kind=target_kind,
        barrier_source=barrier_source,
        horizon_periods=5,
        calibration={"method": "SIGMOID", "test_ece": ece},
        test_metrics=test_metrics if test_metrics is not None else {"log_loss": 0.60, "ece": ece},
        test_baseline=test_baseline if test_baseline is not None else {"log_loss": 0.65},
        model_created_at=DECISION_AT - timedelta(days=30),
    )


def llm_analysis(symbol: str, sentiment: int, ident: str = "1") -> LLMAnalysisInput:
    return LLMAnalysisInput(
        id=uuid.uuid5(uuid.NAMESPACE_DNS, f"llm-{symbol}-{ident}"),
        task="NEWS_ANALYSIS",
        subject_ref=ident,
        article_id=None,
        asset_ids=[asset(symbol)],
        sentiment={str(asset(symbol)): sentiment},
        event_type="EARNINGS",
        catalysts=["fixture catalyst"],
        summary="TEST FIXTURE summary",
        provider="fixture",
        model="fixture-model",
        prompt_version="news-analysis-v1",
        subject_time=DECISION_AT - timedelta(hours=6),
        available_at=DECISION_AT - timedelta(hours=5),
    )


def track(
    strategy: str, mean: float, n: int = 100, direction: Direction = Direction.LONG
) -> TrackRecordInput:
    return TrackRecordInput(
        strategy_id=strategy,
        direction=direction,
        timeframe="1Day",
        dataset_version_id=uuid.uuid5(uuid.NAMESPACE_DNS, "dataset"),
        dataset_source="STRATEGY_REPLAY",
        data_known_at=DECISION_AT - timedelta(days=1),
        observations=n,
        mean_net_return=mean,
        hit_rate=0.55,
        cost_assumption_bps=10.0,
        last_label_end_at=DECISION_AT - timedelta(days=2),
    )


def bundle(**items: Any) -> InputBundle:
    base: dict[str, Any] = {
        "decision_at": DECISION_AT,
        "knowledge_at": DECISION_AT,
        "basis": KnowledgeBasis.RECORDED,
        "mode": DecisionMode.HISTORICAL,
    }
    return InputBundle(**(base | items))


def rich_context(*symbols: str) -> dict[str, Any]:
    """Market regimes and per-asset features so most components are available."""
    return {
        "market_regimes": [
            market_regime("TREND", "BULLISH"),
            market_regime("VOLATILITY", "NORMAL"),
        ],
        "asset_features": [features(s) for s in symbols],
    }


__all__ = [
    "DECISION_AT",
    "asset",
    "asset_regime",
    "bundle",
    "correlation",
    "features",
    "llm_analysis",
    "market_regime",
    "prediction",
    "proposal",
    "rich_context",
    "track",
]
