"""Potential reward, adverse move, costs and (only where supported) expected value.

All figures are fractions of the reference entry, direction-adjusted (a SHORT gains when
the price falls), for one unit of exposure: **no position size is computed here**.

* **Potential reward / adverse move** come from the proposal's own target and invalidation
  levels. They are the distances to levels a strategy chose, not forecasts.
* **Costs** are the policy's assumed round trip (labelled ASSUMPTION: Atlas has no
  spread or slippage model before Phase 7; short borrow fees are not modelled).
* **Expected value** needs a probability that means "the target is reached before the
  invalidation level" for *this* strategy's levels: an APPROVED model with a calibrated
  probability on a BARRIER target with STRATEGY_LEVELS barriers for the same strategy.
  An approved FORWARD_RETURN regression gives an expected (net) return instead. Anything
  else is NOT_SUPPORTED: Atlas does not invent a probability distribution.

A direction prediction ("the price rises") is not a profitability estimate; neither is a
win probability without the payoff sizes. The record keeps them apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.ai_engine.coordinator.bundle import MLPredictionInput, ProposalInput
from app.ai_engine.enums import EstimateStatus
from app.strategies.enums import Direction


@dataclass(frozen=True)
class TradeGeometry:
    entry: float
    gross_reward: float | None  # to the target, before costs
    adverse: float | None  # to the invalidation level, before costs
    cost: float
    net_reward: float | None
    net_adverse: float | None
    reward_risk: float | None  # net reward / net adverse

    def to_json(self) -> dict[str, Any]:
        return {
            "entry": self.entry,
            "potential_reward_gross": self.gross_reward,
            "potential_adverse_gross": self.adverse,
            "round_trip_cost": self.cost,
            "potential_reward_net": self.net_reward,
            "potential_adverse_net": self.net_adverse,
            "reward_risk_net": self.reward_risk,
        }


def geometry(proposal: ProposalInput, cost_bps: float) -> TradeGeometry:
    entry = float(proposal.reference_entry)
    sign = 1.0 if proposal.direction is Direction.LONG else -1.0
    cost = cost_bps / 10_000
    reward = (
        None
        if proposal.target_reference is None
        else sign * (float(proposal.target_reference) - entry) / entry
    )
    adverse = (
        None
        if proposal.invalidation_level is None
        else sign * (entry - float(proposal.invalidation_level)) / entry
    )
    net_reward = None if reward is None else reward - cost
    net_adverse = None if adverse is None else adverse + cost
    ratio = None
    if net_reward is not None and net_adverse is not None and net_adverse > 0:
        ratio = net_reward / net_adverse
    return TradeGeometry(entry, reward, adverse, cost, net_reward, net_adverse, ratio)


@dataclass(frozen=True)
class ExpectedValue:
    status: EstimateStatus
    method: str | None = None
    probability: float | None = None
    probability_meaning: str | None = None
    expected_return: float | None = None
    expected_value: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    model_version_id: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "method": self.method,
            "probability": self.probability,
            "probability_meaning": self.probability_meaning,
            "expected_return": self.expected_return,
            "expected_value": self.expected_value,
            "uncertainty_range": None
            if self.range_low is None
            else {"low": self.range_low, "high": self.range_high},
            "model_version_id": self.model_version_id,
            "reasons": self.reasons,
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def estimate(
    proposal: ProposalInput, geo: TradeGeometry, usable: list[MLPredictionInput]
) -> ExpectedValue:
    """Expected value for the lead proposal from usable (APPROVED, not degraded) predictions."""
    for p in usable:
        probability = _finite(p.output.get("probability"))
        if (
            p.output_kind == "CALIBRATED_PROBABILITY"
            and probability is not None
            and p.target_kind == "BARRIER"
            and p.barrier_source == "STRATEGY_LEVELS"
            and p.strategy_id == proposal.strategy_id
            and geo.net_reward is not None
            and geo.net_adverse is not None
        ):
            ev = probability * geo.net_reward - (1 - probability) * geo.net_adverse
            ece = _finite((p.calibration or {}).get("test_ece"))
            low = high = None
            if ece is not None:
                lo_p, hi_p = max(0.0, probability - ece), min(1.0, probability + ece)
                low = lo_p * geo.net_reward - (1 - lo_p) * geo.net_adverse
                high = hi_p * geo.net_reward - (1 - hi_p) * geo.net_adverse
            return ExpectedValue(
                EstimateStatus.SUPPORTED,
                method="calibrated barrier probability x net payoffs",
                probability=probability,
                probability_meaning="P(target reached before the invalidation level), "
                "calibrated on validation data (approved model)",
                expected_value=ev,
                range_low=low,
                range_high=high,
                model_version_id=str(p.model_version_id),
                reasons=[] if ece is not None else ["no test calibration error recorded"],
            )
    for p in usable:
        estimate_value = _finite(p.output.get("estimate"))
        if (
            p.output_kind == "REGRESSION_ESTIMATE"
            and estimate_value is not None
            and p.target_kind == "FORWARD_RETURN"
            and p.horizon_periods == proposal.holding_horizon_periods
        ):
            rmse = _finite(p.test_metrics.get("rmse"))
            return ExpectedValue(
                EstimateStatus.SUPPORTED,
                method="approved forward-return regression (net of its cost assumption)",
                expected_return=estimate_value,
                expected_value=estimate_value,
                range_low=None if rmse is None else estimate_value - rmse,
                range_high=None if rmse is None else estimate_value + rmse,
                model_version_id=str(p.model_version_id),
            )
    reasons = [
        "no APPROVED model with a calibrated probability of reaching this strategy's target "
        "before its invalidation level, and no approved forward-return estimate for this "
        "horizon: no expected value is claimed"
    ]
    if geo.net_reward is None or geo.net_adverse is None:
        reasons.append("the proposal lacks a target or an invalidation level")
    return ExpectedValue(EstimateStatus.NOT_SUPPORTED, reasons=reasons)


__all__ = ["ExpectedValue", "TradeGeometry", "estimate", "geometry"]
