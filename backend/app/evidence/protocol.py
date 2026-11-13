"""Pre-registered evidence protocols (Phase 11A).

A protocol fixes, *before* the evidence is evaluated, what Atlas must show for a strategy to
be considered for promotion and what makes it rejected. It is stored once, hashed, and never
changed: a different set of criteria is a new version, and every scorecard names the
protocol it was judged under. Governance accepts a scorecard as evidence only under a
protocol registered before the strategy's shadow period began (D-224), so the criteria
cannot be fitted to results already seen.

Defaults follow the specification (§20.1, §14.1): at least 200 closed trades portfolio-wide,
a profit factor above 1.2, positive net expectancy after costs, drawdown within the 10%
maximum-drawdown threshold, risk-adjusted performance against the benchmark, evidence
across regimes, no concentration in a few trades or one asset, clean reconciliation and
reproducible results. "Insufficient evidence means the strategy remains unvalidated."
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.backtesting.config import sha256_of
from app.evidence.enums import AuditStatus

PROTOCOL_ENGINE_VERSION = "11A.1"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SampleRules(_Strict):
    """Minimum evidence before any conclusion. Below these a strategy is
    INSUFFICIENT_EVIDENCE, whatever its results (never promoted, never rejected)."""

    min_oos_trades: int = Field(default=30, ge=1, le=100_000)
    # A year of stitched out-of-sample sessions: a few months is one market episode.
    min_oos_sessions: int = Field(default=250, ge=20, le=100_000)
    min_paper_trades: int = Field(default=30, ge=1, le=100_000)
    # Observed paper sessions: sessions in which the orchestrator actually ran.
    min_paper_sessions: int = Field(default=60, ge=5, le=100_000)
    # Benchmark trend regimes (UP / DOWN) each holding this many out-of-sample trades.
    min_regimes: int = Field(default=2, ge=1, le=4)
    min_regime_trades: int = Field(default=10, ge=1, le=10_000)
    # A direction (LONG / SHORT) is judged on its own only with this many trades.
    min_direction_trades: int = Field(default=10, ge=1, le=10_000)
    # The holdout's first read is judged only with this many closed trades.
    min_holdout_trades: int = Field(default=10, ge=1, le=10_000)


class PromotionRules(_Strict):
    """Every rule must PASS (INSUFFICIENT and NOT_AVAILABLE do not pass)."""

    min_profit_factor: float = Field(default=1.2, ge=1.0, le=10.0)
    # Net expectancy per trade after commissions, fees, borrow and slippage must exceed this.
    min_net_expectancy: float = Field(default=0.0, ge=0.0)
    min_deflated_sharpe: float = Field(default=0.95, ge=0.5, le=1.0)
    max_pbo: float = Field(default=0.5, ge=0.0, le=1.0)
    max_drawdown: float = Field(default=0.10, gt=0.0, le=1.0)
    max_top5_share: float = Field(default=0.5, gt=0.0, le=10.0)
    max_single_asset_share: float = Field(default=0.5, gt=0.0, le=10.0)
    # Out-of-sample profit must survive this multiple of the assumed costs.
    cost_stress_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    # Out-of-sample annualized Sharpe at least the benchmark's on the same sessions.
    require_benchmark_sharpe: bool = True
    # Out-of-sample net return at least the equal-weight buy-and-hold of the universe
    # scaled to the strategy's average exposure is not required (exposures differ); the
    # baseline is reported. A strategy must beat holding cash:
    require_positive_oos_return: bool = True
    # Paper results must not be significantly worse than out of sample (one-sided Welch).
    paper_consistency_alpha: float = Field(default=0.05, gt=0.0, lt=0.5)
    # Mean paper entry shortfall against the decision's reference price, minus the
    # backtest's assumed slippage and half spread, may not exceed this.
    max_execution_shortfall_excess_bps: float = Field(default=10.0, ge=0.0, le=1000.0)


class RejectionRules(_Strict):
    """Applied to a section (out-of-sample or paper) only when its sample is sufficient."""

    reject_non_positive_expectancy: bool = True
    reject_profit_factor_below: float = Field(default=1.0, ge=0.0, le=10.0)
    reject_drawdown_above: float = Field(default=0.15, gt=0.0, le=1.0)
    # PBO above the promotion limit together with an unprofitable out-of-sample record.
    reject_overfit_and_unprofitable: bool = True


class PortfolioRules(_Strict):
    min_oos_trades: int = Field(default=200, ge=1, le=1_000_000)
    min_paper_trades: int = Field(default=200, ge=1, le=1_000_000)
    min_paper_sessions: int = Field(default=60, ge=5, le=100_000)
    max_paper_drawdown: float = Field(default=0.10, gt=0.0, le=1.0)
    max_open_critical_alerts: int = Field(default=0, ge=0, le=1000)
    require_clean_reconciliation: bool = True
    require_reproduction: bool = True
    allowed_audit_statuses: tuple[AuditStatus, ...] = (AuditStatus.PASS, AuditStatus.WARN)
    max_single_asset_share: float = Field(default=0.5, gt=0.0, le=10.0)


class EvidenceCriteria(_Strict):
    engine_version: str = PROTOCOL_ENGINE_VERSION
    sample: SampleRules = Field(default_factory=SampleRules)
    promotion: PromotionRules = Field(default_factory=PromotionRules)
    rejection: RejectionRules = Field(default_factory=RejectionRules)
    portfolio: PortfolioRules = Field(default_factory=PortfolioRules)
    # Only paper positions opened at or after the protocol's registration count (the
    # protocol was fixed before that evidence existed).
    paper_after_registration_only: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> EvidenceCriteria:
        if self.rejection.reject_profit_factor_below > self.promotion.min_profit_factor:
            raise ValueError("the rejection profit factor cannot exceed the promotion gate")
        if self.rejection.reject_drawdown_above < self.promotion.max_drawdown:
            raise ValueError("the rejection drawdown cannot be below the promotion limit")
        return self

    def sha256(self) -> str:
        return sha256_of(self.model_dump(mode="json"))

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


DEFAULT_PROTOCOL_NAME = "atlas-paper-evidence"

DEFAULT_RATIONALE = (
    "Specification §20.1 gates (200 trades portfolio-wide, profit factor > 1.2, positive net "
    "expectancy after costs, drawdown within the 10% threshold, benchmark-relative risk-"
    "adjusted performance, evidence across regimes, no concentration, accurate "
    "reconciliation, reproducibility) and the Phase 8B overfitting gates (deflated Sharpe "
    "0.95, PBO 0.5, cost stress 2x), fixed before the Phase 11A evaluation."
)


__all__ = [
    "DEFAULT_PROTOCOL_NAME",
    "DEFAULT_RATIONALE",
    "PROTOCOL_ENGINE_VERSION",
    "EvidenceCriteria",
    "PortfolioRules",
    "PromotionRules",
    "RejectionRules",
    "SampleRules",
]
