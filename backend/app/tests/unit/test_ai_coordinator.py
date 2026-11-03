"""Decision coordinator: pure evaluation rules (Phase 5B). Inputs are TEST FIXTURES."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.ai_engine.coordinator.bundle import InputBundle
from app.ai_engine.coordinator.evaluate import SCORE_NOTE, evaluate
from app.ai_engine.coordinator.expected_value import geometry
from app.ai_engine.coordinator.policy import DecisionPolicy, Weights
from app.ai_engine.enums import (
    ComponentStatus,
    ConflictType,
    DecisionOutcome,
    EstimateStatus,
    RunOutcome,
)
from app.strategies.enums import Direction
from app.tests.fixtures.ai_bundles import (
    DECISION_AT,
    asset,
    asset_regime,
    bundle,
    correlation,
    features,
    llm_analysis,
    market_regime,
    prediction,
    proposal,
    rich_context,
    track,
)

POLICY = DecisionPolicy()
LONG, SHORT = Direction.LONG, Direction.SHORT


def by_symbol(result, symbol, direction=LONG):
    return next(d for d in result.decisions if d.symbol == symbol and d.direction is direction)


def component(decision, name):
    return next(c for c in decision.components if c["name"] == name)


def test_no_proposals_is_a_valid_no_opportunity_outcome():
    result = evaluate(bundle(), POLICY)
    assert result.outcome is RunOutcome.NO_OPPORTUNITY
    [only] = result.decisions
    assert only.outcome is DecisionOutcome.NO_OPPORTUNITY
    assert only.asset_id is None
    assert only.reasons[0]["code"] == "NO_PROPOSALS"


def test_agreeing_strategies_are_one_opportunity_and_raise_agreement():
    alone = evaluate(bundle(proposals=[proposal()], **rich_context("AAA")), POLICY)
    both = evaluate(
        bundle(
            proposals=[proposal(), proposal(strategy="breakout", entry="100.5")],
            **rich_context("AAA"),
        ),
        POLICY,
    )
    [d_alone] = [d for d in alone.decisions if d.asset_id]
    [d_both] = [d for d in both.decisions if d.asset_id]
    assert len(d_both.proposal_ids) == 2
    assert (
        component(d_both, "strategy_agreement")["score"]
        > component(d_alone, "strategy_agreement")["score"]
    )
    duplicates = [c for c in both.conflicts if c.type is ConflictType.DUPLICATE_PROPOSALS]
    assert duplicates, "nearly identical entries are recorded as duplicates"
    assert "counted once" in duplicates[0].handling


def test_repeated_strategy_proposals_count_once():
    result = evaluate(
        bundle(
            proposals=[
                proposal(strength="0.6", seed="a"),
                proposal(strength="0.8", timeframe="1Week", seed="b"),
            ],
            **rich_context("AAA"),
        ),
        POLICY,
    )
    d = by_symbol(result, "AAA")
    assert [s["strategy_id"] for s in d.strategy_versions] == ["momentum"]
    assert component(d, "signal_quality")["score"] == pytest.approx(0.8)
    assert d.timeframes == ["1Day", "1Week"]


def test_close_opposing_proposals_are_both_deferred_never_merged():
    result = evaluate(
        bundle(
            proposals=[
                proposal(strategy="momentum", strength="0.70"),
                proposal(strategy="mean_reversion", direction=SHORT, strength="0.70"),
            ],
            market_regimes=[market_regime("VOLATILITY", "NORMAL")],
            asset_features=[features("AAA")],
        ),
        POLICY,
    )
    long_, short = by_symbol(result, "AAA", LONG), by_symbol(result, "AAA", SHORT)
    assert {long_.outcome, short.outcome} <= {DecisionOutcome.DEFER, DecisionOutcome.REJECT}
    opposing = [c for c in result.conflicts if c.type is ConflictType.OPPOSING_DIRECTIONS]
    assert len(opposing) == 1
    assert result.outcome is RunOutcome.NO_OPPORTUNITY


def test_a_clearly_stronger_side_deprioritizes_the_weaker():
    policy = DecisionPolicy(opposing_margin=0.01, prioritize_threshold=0.3)
    result = evaluate(
        bundle(
            proposals=[
                proposal(strategy="momentum", strength="0.95"),
                proposal(strategy="breakout", strength="0.95", entry="100.2"),
                proposal(strategy="mean_reversion", direction=SHORT, strength="0.10"),
            ],
            **rich_context("AAA"),
        ),
        policy,
    )
    short = by_symbol(result, "AAA", SHORT)
    assert short.outcome in (DecisionOutcome.DEPRIORITIZE, DecisionOutcome.REJECT)
    long_ = by_symbol(result, "AAA", LONG)
    assert any(c["type"] == "OPPOSING_DIRECTIONS" for c in long_.conflicts)
    assert long_.scores["ranking_score"] < long_.scores["heuristic_score"]


def test_missing_evidence_is_insufficient_data():
    sparse = DecisionPolicy(min_component_coverage=0.9)
    result = evaluate(bundle(proposals=[proposal()]), sparse)
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.INSUFFICIENT_DATA
    assert d.reasons[0]["code"] == "INSUFFICIENT_EVIDENCE"
    unavailable = {c["name"] for c in d.components if c["status"] != "AVAILABLE"}
    assert {"ml_support", "regime_suitability", "liquidity", "news"} <= unavailable


def test_unreliable_feature_data_is_insufficient_data():
    result = evaluate(bundle(proposals=[proposal(valid=2)], **rich_context("AAA")), POLICY)
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.INSUFFICIENT_DATA
    assert any(r["code"] == "UNRELIABLE_DATA" for r in d.reasons)


def test_poor_reward_relative_to_risk_is_rejected():
    result = evaluate(
        bundle(proposals=[proposal(stop="95", target="103")], **rich_context("AAA")), POLICY
    )
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.REJECT
    assert d.reasons[0]["code"] == "POOR_REWARD_RISK"


def test_transaction_costs_reduce_net_reward_and_risk():
    p = proposal(stop="96", target="102")
    cheap, dear = geometry(p, 0.0), geometry(p, 50.0)
    assert cheap.net_reward == pytest.approx(0.02)
    assert dear.net_reward == pytest.approx(0.015)
    assert dear.net_adverse == pytest.approx(0.045)
    assert dear.reward_risk is not None
    assert cheap.reward_risk is not None
    assert dear.reward_risk < cheap.reward_risk
    short = geometry(proposal(direction=SHORT, stop="104", target="90"), 10.0)
    assert short.gross_reward == pytest.approx(0.10)
    assert short.adverse == pytest.approx(0.04)


def test_opposed_market_regime_rejects_trend_strategies():
    context = rich_context("AAA") | {"market_regimes": [market_regime("TREND", "BEARISH")]}
    result = evaluate(bundle(proposals=[proposal()], **context), POLICY)
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.REJECT
    assert any(r["code"] == "UNFAVORABLE_REGIME" for r in d.reasons)
    assert any(c.type is ConflictType.REGIME_CONFLICT for c in result.conflicts)


def test_extreme_market_volatility_defers():
    context = rich_context("AAA") | {
        "market_regimes": [
            market_regime("TREND", "BULLISH"),
            market_regime("VOLATILITY", "EXTREME"),
        ]
    }
    d = by_symbol(evaluate(bundle(proposals=[proposal()], **context), POLICY), "AAA")
    assert d.outcome is DecisionOutcome.DEFER
    assert any(r["code"] == "UNFAVORABLE_MARKET_CONDITIONS" for r in d.reasons)


def test_stale_inputs_are_deferred():
    old = DECISION_AT - timedelta(days=6)
    d = by_symbol(
        evaluate(bundle(proposals=[proposal(cutoff=old)], **rich_context("AAA")), POLICY), "AAA"
    )
    assert d.outcome is DecisionOutcome.DEFER
    assert any(r["code"] == "STALE_DATA" for r in d.reasons)


def test_strong_well_supported_opportunity_is_prioritized_for_review_only():
    context = rich_context("AAA") | {
        "asset_regimes": [asset_regime("AAA", "TREND", "BULLISH")],
        "track_records": [track("momentum", 0.01), track("breakout", 0.01)],
    }
    result = evaluate(
        bundle(
            proposals=[proposal(strength="0.8"), proposal(strategy="breakout", strength="0.8")],
            **context,
        ),
        DecisionPolicy(prioritize_threshold=0.5),
    )
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.PRIORITIZE_FOR_REVIEW
    assert d.rank == 1
    assert result.outcome is RunOutcome.OPPORTUNITIES_PRIORITIZED
    assert "not an approved trade" in " ".join(r["message"] for r in d.reasons)
    assert "risk engine has not reviewed" in d.explanation
    assert d.scores["note"] == SCORE_NOTE
    assert d.scores["calibrated_probability"] is None  # no approved model
    assert d.expected["expected_value"]["status"] == EstimateStatus.NOT_SUPPORTED.value


def test_correlated_opportunities_compete_for_capacity():
    context = {
        "market_regimes": [
            market_regime("TREND", "BULLISH"),
            market_regime("VOLATILITY", "NORMAL"),
        ],
        "asset_features": [features("AAA"), features("BBB")],
        "correlations": [correlation("AAA", "BBB", 0.92)],
    }
    result = evaluate(
        bundle(
            proposals=[proposal("AAA", strength="0.9"), proposal("BBB", strength="0.6")],
            **context,
        ),
        DecisionPolicy(prioritize_threshold=0.3),
    )
    a, b = by_symbol(result, "AAA"), by_symbol(result, "BBB")
    assert a.outcome is DecisionOutcome.PRIORITIZE_FOR_REVIEW
    assert b.outcome is DecisionOutcome.DEPRIORITIZE
    assert any(r["code"] == "CORRELATED_WITH_HIGHER_RANKED" for r in b.reasons)
    assert any(c.type is ConflictType.CORRELATED_CAPACITY for c in result.conflicts)


def test_hedging_directions_on_correlated_assets_do_not_compete():
    context = rich_context("AAA", "BBB") | {"correlations": [correlation("AAA", "BBB", 0.92)]}
    context["market_regimes"] = [market_regime("TREND", "SIDEWAYS")]
    result = evaluate(
        bundle(
            proposals=[
                proposal("AAA", strategy="mean_reversion", strength="0.9"),
                proposal("BBB", strategy="mean_reversion", direction=SHORT, strength="0.9"),
            ],
            **context,
        ),
        DecisionPolicy(prioritize_threshold=0.3, reject_on_opposed_regime=False),
    )
    assert not [c for c in result.conflicts if c.type is ConflictType.CORRELATED_CAPACITY]


def test_review_capacity_cap():
    symbols = ["S1", "S2", "S3"]
    result = evaluate(
        bundle(proposals=[proposal(s) for s in symbols], **rich_context(*symbols)),
        DecisionPolicy(prioritize_threshold=0.3, max_prioritized=1),
    )
    outcomes = sorted(by_symbol(result, s).outcome.value for s in symbols)
    assert outcomes.count("PRIORITIZE_FOR_REVIEW") == 1
    assert any(
        r["code"] == "REVIEW_CAPACITY" for s in symbols for r in by_symbol(result, s).reasons
    )


# --- ML ---------------------------------------------------------------------------------


def test_only_approved_models_contribute():
    preds = [
        prediction(status="SHADOW", name="shadow"),
        prediction(status="CANDIDATE", name="candidate"),
    ]
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=preds, **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    ml = component(d, "ml_support")
    assert ml["status"] == ComponentStatus.UNAVAILABLE.value
    assert all(not m["used"] for m in d.model_versions)
    assert any("shadow" in e["summary"] and "never used" in e["summary"] for e in d.evidence)


def test_approved_calibrated_model_gives_probability_and_expected_value():
    pred = prediction(output={"raw_score": 0.61, "probability": 0.6})
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=[pred], **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    assert component(d, "ml_support")["kind"] == "CALIBRATED_PROBABILITY"
    assert d.scores["calibrated_probability"] == pytest.approx(0.6)
    ev = d.expected["expected_value"]
    assert ev["status"] == "SUPPORTED"
    # 0.6 x 0.099 - 0.4 x 0.041
    assert ev["expected_value"] == pytest.approx(0.6 * 0.099 - 0.4 * 0.041)
    assert ev["uncertainty_range"]["low"] < ev["expected_value"] < ev["uncertainty_range"]["high"]
    assert d.model_versions[0]["used"] is True


def test_probability_of_another_target_is_not_an_expected_value():
    pred = prediction(target_kind="DIRECTION", barrier_source=None)
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=[pred], **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    assert d.expected["expected_value"]["status"] == "NOT_SUPPORTED"
    assert d.scores["calibrated_probability"] == pytest.approx(0.6)


def test_negative_supported_expected_value_is_rejected():
    pred = prediction(output={"raw_score": 0.2, "probability": 0.2})
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=[pred], **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    assert d.outcome is DecisionOutcome.REJECT
    assert any(r["code"] == "NEGATIVE_EXPECTED_VALUE" for r in d.reasons)


def test_degraded_model_is_excluded():
    pred = prediction(test_metrics={"log_loss": 0.70}, test_baseline={"log_loss": 0.69})
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=[pred], **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    assert component(d, "ml_support")["status"] == "UNAVAILABLE"
    assert any("worse than its baseline" in e["summary"] for e in d.evidence)


def test_uncalibrated_scores_have_reduced_authority():
    pred = prediction(output_kind="UNCALIBRATED_SCORE", output={"raw_score": 0.9})
    d = by_symbol(
        evaluate(
            bundle(proposals=[proposal()], ml_predictions=[pred], **rich_context("AAA")), POLICY
        ),
        "AAA",
    )
    ml = component(d, "ml_support")
    assert ml["kind"] == "RAW_MODEL_SCORE"
    assert ml["score"] == pytest.approx(0.5 + 0.4 * POLICY.uncalibrated_authority)
    assert d.scores["calibrated_probability"] is None


def test_disagreeing_approved_models_defer():
    preds = [
        prediction(name="m1", output={"raw_score": 0.8, "probability": 0.8}),
        prediction(name="m2", output={"raw_score": 0.2, "probability": 0.2}),
    ]
    result = evaluate(
        bundle(proposals=[proposal()], ml_predictions=preds, **rich_context("AAA")),
        DecisionPolicy(reject_negative_expected_value=False),
    )
    d = by_symbol(result, "AAA")
    assert d.outcome is DecisionOutcome.DEFER
    assert any(r["code"] == "ML_DISAGREEMENT" for r in d.reasons)
    assert any(c.type is ConflictType.ML_DISAGREEMENT for c in result.conflicts)
    assert d.uncertainty["ml_disagreement"] == pytest.approx(0.6)


# --- News -------------------------------------------------------------------------------


def test_news_analysis_contributes_and_contradiction_is_recorded():
    supportive = evaluate(
        bundle(
            proposals=[proposal()], llm_analyses=[llm_analysis("AAA", 1)], **rich_context("AAA")
        ),
        POLICY,
    )
    assert component(by_symbol(supportive, "AAA"), "news")["score"] == pytest.approx(1.0)
    contrary = evaluate(
        bundle(
            proposals=[proposal()], llm_analyses=[llm_analysis("AAA", -1)], **rich_context("AAA")
        ),
        POLICY,
    )
    assert component(by_symbol(contrary, "AAA"), "news")["score"] == pytest.approx(0.0)
    assert any(c.type is ConflictType.NEWS_PRICE_CONFLICT for c in contrary.conflicts)


def test_without_news_analysis_the_component_is_unavailable_not_neutral():
    d = by_symbol(evaluate(bundle(proposals=[proposal()], **rich_context("AAA")), POLICY), "AAA")
    news = component(d, "news")
    assert news["status"] == "UNAVAILABLE"
    assert news["score"] is None
    assert "deterministic fallback" in news["reason"]


# --- Policy, determinism, replay ---------------------------------------------------------


def test_policy_is_versioned_by_content():
    assert DecisionPolicy().sha256() == DecisionPolicy().sha256()
    assert DecisionPolicy(weights=Weights(news=0.0)).sha256() != DecisionPolicy().sha256()
    with pytest.raises(ValidationError):
        DecisionPolicy(defer_market_volatility=("PANIC",))
    with pytest.raises(ValidationError):
        DecisionPolicy.model_validate({"unknown_field": 1})


def test_evaluation_is_deterministic_and_replayable_from_stored_payloads():
    original = bundle(
        proposals=[
            proposal(),
            proposal("BBB", strategy="breakout"),
            proposal(direction=SHORT, strategy="volatility"),
        ],
        ml_predictions=[prediction()],
        llm_analyses=[llm_analysis("AAA", 1)],
        correlations=[correlation("AAA", "BBB", 0.5)],
        **rich_context("AAA", "BBB"),
    )
    first, second = evaluate(original, POLICY), evaluate(original, POLICY)
    assert first.result_sha256() == second.result_sha256()
    rebuilt = InputBundle.from_rows(
        decision_at=original.decision_at,
        knowledge_at=original.knowledge_at,
        basis=original.basis,
        mode=original.mode,
        rows=[(item.kind, item.payload()) for item in reversed(original.items())],
        notes=list(original.notes),
    )
    assert rebuilt.sha256() == original.sha256()
    assert evaluate(rebuilt, POLICY).result_sha256() == first.result_sha256()


def test_heuristic_scores_are_never_labelled_probabilities():
    d = by_symbol(evaluate(bundle(proposals=[proposal()], **rich_context("AAA")), POLICY), "AAA")
    assert "not probabilities" in d.scores["note"]
    assert d.uncertainty["kind"].startswith("HEURISTIC")
    kinds = {c["kind"] for c in d.components if c["status"] == "AVAILABLE"}
    assert "CALIBRATED_PROBABILITY" not in kinds
    assert asset("AAA") == d.asset_id
