"""Qualification rules (mandatory vs optional, missing data) and watchlist scoring."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

import pytest

from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.enums import (
    InstrumentType,
    ProviderAssetStatus,
    QualificationOutcome,
    RuleStatus,
)
from app.asset_discovery.metrics import DailyBar, SpreadSummary, compute, spread_measurements
from app.asset_discovery.rules import (
    DEFAULT_RULE_SETS,
    DEFAULT_SCORING,
    InstrumentFacts,
    RuleSetConfig,
    ScoringConfig,
    evaluate,
)
from app.asset_discovery.scoring import DISCLAIMER, ScoreInput, score_assets
from app.asset_discovery.sessions import calendar_for, completed_sessions
from app.database.models import AssetClass

IEX = DEFAULT_RULE_SETS["us-equity-iex"][1]
SCORING = DEFAULT_SCORING["watchlist-default"][1]
CAL = calendar_for("XNAS")
SESSIONS = completed_sessions(CAL, datetime(2026, 10, 30, 23, tzinfo=UTC), 60)
A = uuid.UUID(int=7)


def facts(**overrides) -> InstrumentFacts:
    values: dict[str, Any] = {
        "asset_id": A,
        "symbol": "FIXT",
        "asset_class": AssetClass.EQUITY,
        "instrument_type": InstrumentType.UNCLASSIFIED,
        "exchange_mic": "XNAS",
        "calendar_code": "XNAS",
        "status": ProviderAssetStatus.ACTIVE,
        "tradable": True,
        "mapped": True,
        "mapping_valid_from": "2016-01-01T00:00:00+00:00",
        "provider_supports_class": True,
        "feed_has_history": True,
    } | overrides
    return InstrumentFacts(**values)


def measured(close=25.0, volume=200_000.0, swing=0.0):
    bars = []
    for i, s in enumerate(SESSIONS):
        c = close * (1 + swing) if i % 2 else close
        bars.append(
            DailyBar(
                A,
                s.day,
                datetime.combine(s.day, time(4), tzinfo=UTC),
                c,
                c + 1,
                c - 1,
                c,
                volume,
                None,
                "VALID",
            )
        )
    return compute(bars, [A], SESSIONS)[A]


def status(evaluation, rule):
    return next(r.status for r in evaluation.results if r.rule == rule)


def test_liquid_instrument_qualifies_without_quotes():
    result = evaluate(facts(), measured(), spread_measurements(A, None, 5), IEX)
    assert result.outcome is QualificationOutcome.QUALIFIED
    # Optional spread rules: not applied (no quotes), never a rejection.
    assert status(result, "max_spread") is RuleStatus.UNAVAILABLE
    assert status(result, "quote_quality") is RuleStatus.UNAVAILABLE
    assert not result.failed


def test_missing_history_is_insufficient_data_not_disqualification():
    result = evaluate(facts(), compute([], [A], SESSIONS)[A], [], IEX)
    assert result.outcome is QualificationOutcome.INSUFFICIENT_DATA
    assert "history_coverage" in result.unavailable
    assert result.failed == []


@pytest.mark.parametrize(
    ("override", "rule"),
    [
        ({"exchange_mic": "OTCM", "calendar_code": None}, "supported_exchange"),
        ({"exchange_mic": None, "calendar_code": None}, "supported_exchange"),
        ({"status": ProviderAssetStatus.INACTIVE}, "active_status"),
        ({"status": None}, "active_status"),
        ({"tradable": False}, "tradable"),
        ({"mapped": False}, "provider_mapping"),
        ({"feed_has_history": False}, "market_data_capability"),
        ({"asset_class": AssetClass.CRYPTO}, "supported_asset_class"),
    ],
)
def test_instrument_rules_disqualify_and_skip_data_rules(override, rule):
    result = evaluate(facts(**override), measured(), [], IEX)
    assert result.outcome is QualificationOutcome.DISQUALIFIED
    assert rule in result.failed
    assert status(result, "data_rules") is RuleStatus.NOT_APPLICABLE


def test_price_and_liquidity_thresholds():
    cheap = evaluate(facts(), measured(close=2.0), [], IEX)
    assert cheap.failed == ["min_price", "min_median_dollar_volume"]
    thin = evaluate(facts(), measured(close=50.0, volume=5_000.0), [], IEX)
    assert "min_avg_daily_volume" in thin.failed
    message = next(r.message for r in thin.results if r.rule == "min_avg_daily_volume")
    assert "IEX" in message


def test_wide_spread_fails_only_when_enough_quotes_exist():
    window = (SESSIONS[-5].regular_open, SESSIONS[-1].regular_close)
    wide = spread_measurements(A, SpreadSummary(5_000, 80.0, 0.0, *window), 5)
    result = evaluate(facts(), measured(), wide, IEX)
    assert result.outcome is QualificationOutcome.QUALIFIED  # optional rule
    assert status(result, "max_spread") is RuleStatus.FAIL
    few = spread_measurements(A, SpreadSummary(10, 80.0, 0.0, *window), 5)
    assert status(evaluate(facts(), measured(), few, IEX), "max_spread") is RuleStatus.UNAVAILABLE


def test_disabled_volatility_cap_is_not_applicable():
    result = evaluate(facts(), measured(), [], IEX)
    assert status(result, "max_volatility") is RuleStatus.NOT_APPLICABLE
    capped = IEX.model_copy(update={"max_annualized_volatility": Decimal("0.0001")})
    swinging = measured(swing=0.02)
    assert status(evaluate(facts(), swinging, [], capped), "max_volatility") is RuleStatus.FAIL


def test_default_rule_sets_are_feed_specific():
    iex, sip = DEFAULT_RULE_SETS["us-equity-iex"][1], DEFAULT_RULE_SETS["us-equity-sip"][1]
    assert iex.feed == "iex"
    assert sip.feed == "sip"
    assert sip.min_median_dollar_volume > iex.min_median_dollar_volume
    assert AssetDiscoverySettings(_env_file=None).rule_set_code("iex") == "us-equity-iex"
    with pytest.raises(ValueError, match="extra"):
        RuleSetConfig.model_validate(iex.model_dump() | {"unknown": 1})


# ------------------------------------------------------------------ scoring
def score_input(i: int, **overrides) -> ScoreInput:
    values: dict[str, Any] = {
        "asset_id": uuid.UUID(int=100 + i),
        "symbol": f"S{i}",
        "median_dollar_volume": 1e6 * (i + 1),
        "relative_volume": 1.0 + i / 10,
        "realized_volatility": 0.3,
        "return_20d": 0.01 * i,
        "relative_strength_20d": -0.01 * i,
        "median_spread_bps": 5.0 + i,
        "history_coverage": 1.0,
    } | overrides
    return ScoreInput(**values)


def test_scores_are_explained_ranked_and_never_a_signal():
    scores = score_assets(
        [score_input(i) for i in range(5)], SCORING, min_history_coverage=0.9, feed="iex"
    )
    ranked = [s for s in scores if s.rank is not None]
    assert [s.rank for s in ranked] == [1, 2, 3, 4, 5]
    assert all(DISCLAIMER in s.explanation for s in scores)
    assert all(0 <= float(s.overall or 0) <= 100 for s in ranked)
    best = ranked[0]
    assert {f.factor for f in best.factors} == set(SCORING.weights)
    assert "IEX" in best.explanation


def test_missing_factor_renormalizes_weights_and_is_reported():
    scores = score_assets(
        [score_input(0, median_spread_bps=None), score_input(1, median_spread_bps=None)],
        SCORING,
        min_history_coverage=0.9,
        feed="iex",
    )
    assert all(s.weight_coverage == Decimal("0.9000") for s in scores)
    assert all("spread quality" in s.explanation for s in scores)


def test_too_little_data_leaves_an_asset_unranked():
    sparse = score_input(
        0,
        median_dollar_volume=None,
        relative_volume=None,
        realized_volatility=None,
        return_20d=None,
        relative_strength_20d=None,
        median_spread_bps=None,
    )
    scores = score_assets([sparse, score_input(1)], SCORING, min_history_coverage=0.9, feed="iex")
    unranked = next(s for s in scores if s.asset_id == sparse.asset_id)
    assert unranked.rank is None
    assert unranked.overall is None
    assert not unranked.selected
    assert "Not ranked" in unranked.explanation


def test_higher_volatility_is_not_automatically_better():
    calm = score_input(0, realized_volatility=0.3)
    wild = score_input(1, realized_volatility=2.0)
    scores = {
        s.symbol: s
        for s in score_assets([calm, wild], SCORING, min_history_coverage=0.9, feed="iex")
    }
    band = {
        s: next(f.score for f in scores[s].factors if f.factor == "volatility_band") for s in scores
    }
    assert band["S0"] == 100.0
    assert band["S1"] == 0.0


def test_watchlist_size_retention_and_deterministic_ties():
    config = SCORING.model_copy(update={"watchlist_size": 2, "retention_buffer": 1})
    inputs = [score_input(i) for i in range(4)]
    first = score_assets(inputs, config, min_history_coverage=0.9, feed="iex")
    selected = {s.symbol for s in first if s.selected}
    assert len(selected) == 2
    third = next(s for s in first if s.rank == 3)
    kept = score_assets(
        inputs, config, min_history_coverage=0.9, feed="iex", current_members={third.asset_id: True}
    )
    assert next(s for s in kept if s.asset_id == third.asset_id).selected  # within buffer
    again = score_assets(inputs, config, min_history_coverage=0.9, feed="iex")
    assert [s.asset_id for s in again] == [s.asset_id for s in first]


def test_unknown_factor_is_rejected():
    with pytest.raises(ValueError, match="unknown scoring factor"):
        ScoringConfig.model_validate(SCORING.model_dump() | {"weights": {"profitability": 1}})
    bad = SCORING.model_copy(update={"weights": {"profitability": Decimal(1)}})  # unvalidated
    with pytest.raises(ValueError, match="unknown scoring factor"):
        score_assets([score_input(0)], bad, min_history_coverage=0.9, feed="iex")
