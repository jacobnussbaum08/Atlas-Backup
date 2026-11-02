"""Active Watchlist scoring (Phase 3A): which qualified assets get analytical attention.

This ranks **analytical priority only**. A score is not a trading signal, does not
approve, size or open anything, and says nothing about whether an asset will perform
well. Every score is stored with its factor values, the scoring version and a plain-text
explanation.

Factors (each 0-100, computed across the qualified assets of one evaluation):

* ``liquidity``: percentile of median daily dollar volume.
* ``activity``: percentile of relative volume (recent 5 sessions vs 20).
* ``volatility_band``: 100 inside a configured realized-volatility band, falling linearly
  to 0 at the band's outer limits. Higher volatility is **not** treated as better.
* ``momentum_magnitude`` / ``relative_strength_magnitude``: percentile of the absolute
  20-session return and of the absolute return relative to the benchmark. Direction is
  ignored: Atlas analyses long and short, and a large move in either direction merits
  attention, not a position.
* ``spread_quality``: inverse percentile of the median quoted spread (when quotes exist).
* ``data_reliability``: history coverage scaled from the rule set's minimum (0) to 100%.

A factor without data is excluded and the remaining weights are renormalized; an asset
for which less than ``min_weight_coverage`` of the weight is computable is not ranked.
"""

from __future__ import annotations

import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.asset_discovery.rules import SCORING_FACTORS, ScoringConfig

DISCLAIMER = (
    "Analytical priority only: not a trading signal, not a recommendation, and no "
    "indication of future performance."
)


@dataclass(frozen=True)
class ScoreInput:
    asset_id: uuid.UUID
    symbol: str
    median_dollar_volume: float | None
    relative_volume: float | None
    realized_volatility: float | None
    return_20d: float | None
    relative_strength_20d: float | None
    median_spread_bps: float | None
    history_coverage: float | None


@dataclass(frozen=True)
class FactorScore:
    factor: str
    raw: float | None
    score: float | None
    weight: Decimal
    explanation: str

    @property
    def available(self) -> bool:
        return self.score is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": None if self.raw is None else round(self.raw, 6),
            "score": None if self.score is None else round(self.score, 2),
            "weight": str(self.weight),
            "available": self.available,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class AssetScore:
    asset_id: uuid.UUID
    symbol: str
    overall: Decimal | None  # None: not enough factors to rank
    weight_coverage: Decimal
    factors: tuple[FactorScore, ...]
    rank: int | None = None
    selected: bool = False
    explanation: str = ""


def _percentile(values: list[float], value: float) -> float:
    """Mid-rank percentile of ``value`` within sorted ``values`` (0-100)."""
    if len(values) == 1:
        return 50.0
    below = bisect_left(values, value)
    equal = bisect_right(values, value) - below
    return 100.0 * (below + 0.5 * equal - 0.5) / (len(values) - 1)


def _band(value: float, config: ScoringConfig) -> float:
    lo, hi = (float(x) for x in config.volatility_band)
    zero_lo, zero_hi = (float(x) for x in config.volatility_zero_at)
    if lo <= value <= hi:
        return 100.0
    if value < lo:
        return 0.0 if value <= zero_lo else 100.0 * (value - zero_lo) / (lo - zero_lo)
    return 0.0 if value >= zero_hi else 100.0 * (zero_hi - value) / (zero_hi - hi)


def _money(value: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"${value / size:,.1f}{unit}"
    return f"${value:,.0f}"


def score_assets(
    inputs: list[ScoreInput],
    config: ScoringConfig,
    *,
    min_history_coverage: float,
    feed: str,
    current_members: Mapping[uuid.UUID, bool] | None = None,
) -> list[AssetScore]:
    """Score, rank and select. Deterministic: ties break on liquidity, then asset ID."""
    unknown = set(config.weights) - set(SCORING_FACTORS)
    if unknown:
        raise ValueError(f"unknown scoring factor(s): {sorted(unknown)}")
    members = current_members or {}
    n = len(inputs)
    pools: dict[str, list[float]] = {
        "liquidity": sorted(
            i.median_dollar_volume for i in inputs if i.median_dollar_volume is not None
        ),
        "activity": sorted(i.relative_volume for i in inputs if i.relative_volume is not None),
        "momentum_magnitude": sorted(abs(i.return_20d) for i in inputs if i.return_20d is not None),
        "relative_strength_magnitude": sorted(
            abs(i.relative_strength_20d) for i in inputs if i.relative_strength_20d is not None
        ),
        "spread_quality": sorted(
            i.median_spread_bps for i in inputs if i.median_spread_bps is not None
        ),
    }
    venue = feed.upper()
    scored: list[AssetScore] = []
    for item in inputs:
        factors: list[FactorScore] = []

        def add(
            factor: str,
            raw: float | None,
            score: float | None,
            text: str,
            factors: list[FactorScore] = factors,
        ) -> None:
            weight = config.weights.get(factor, Decimal(0))
            if weight > 0:
                factors.append(FactorScore(factor, raw, score, weight, text))

        def pct(factor: str, raw: float | None) -> float | None:
            return None if raw is None else _percentile(pools[factor], raw)

        v = item.median_dollar_volume
        s = pct("liquidity", v)
        add(
            "liquidity",
            v,
            s,
            "no dollar-volume measurement"
            if s is None
            else f"median {venue} dollar volume {_money(v or 0)} ({s:.0f}th percentile of {n})",
        )
        v = item.relative_volume
        s = pct("activity", v)
        add(
            "activity",
            v,
            s,
            "no relative-volume measurement"
            if s is None
            else f"recent volume {v:.2f}x its 20-session average ({s:.0f}th percentile)",
        )
        v = item.realized_volatility
        s = None if v is None else _band(v, config)
        lo, hi = config.volatility_band
        add(
            "volatility_band",
            v,
            s,
            "no volatility measurement"
            if s is None
            else f"annualized volatility {v:.0%} vs preferred band {float(lo):.0%}-{float(hi):.0%}",
        )
        v = item.return_20d
        s = pct("momentum_magnitude", None if v is None else abs(v))
        add(
            "momentum_magnitude",
            v,
            s,
            "no 20-session return"
            if s is None
            else f"20-session return {v:+.1%} (magnitude {s:.0f}th percentile; direction ignored)",
        )
        v = item.relative_strength_20d
        s = pct("relative_strength_magnitude", None if v is None else abs(v))
        add(
            "relative_strength_magnitude",
            v,
            s,
            "no benchmark-relative return"
            if s is None
            else f"{v:+.1%} vs benchmark over 20 sessions (magnitude {s:.0f}th percentile)",
        )
        v = item.median_spread_bps
        s = None if v is None else 100.0 - _percentile(pools["spread_quality"], v)
        add(
            "spread_quality",
            v,
            s,
            "no stored quotes: spread not scored"
            if s is None
            else f"median quoted {venue} spread {v:.1f} bps "
            f"(tighter than {s:.0f}% of quoted assets)",
        )
        v = item.history_coverage
        if v is None:
            s = None
        else:
            span = 1.0 - min_history_coverage
            s = (
                100.0
                if span <= 0
                else max(0.0, min(100.0, 100.0 * (v - min_history_coverage) / span))
            )
        add(
            "data_reliability",
            v,
            s,
            "no coverage measurement"
            if s is None
            else f"{v:.0%} of expected sessions have a valid bar",
        )

        total = sum((f.weight for f in factors), Decimal(0))
        available = sum((f.weight for f in factors if f.available), Decimal(0))
        coverage = Decimal(0) if total == 0 else (available / total)
        overall: Decimal | None = None
        if available > 0 and coverage >= config.min_weight_coverage:
            value = (
                sum(Decimal(str(f.score)) * f.weight for f in factors if f.available) / available
            )
            overall = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        scored.append(
            AssetScore(
                item.asset_id,
                item.symbol,
                overall,
                coverage.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN),
                tuple(factors),
            )
        )

    liquidity = {i.asset_id: i.median_dollar_volume or 0.0 for i in inputs}
    ranked = sorted(
        (s for s in scored if s.overall is not None),
        key=lambda s: (-(s.overall or 0), -liquidity[s.asset_id], str(s.asset_id)),
    )
    keep_until = config.watchlist_size + config.retention_buffer
    final: list[AssetScore] = []
    for position, entry in enumerate(ranked, start=1):
        eligible = (entry.overall or 0) >= config.min_score
        selected = eligible and (
            position <= config.watchlist_size
            or (members.get(entry.asset_id, False) and position <= keep_until)
        )
        final.append(
            AssetScore(
                entry.asset_id,
                entry.symbol,
                entry.overall,
                entry.weight_coverage,
                entry.factors,
                rank=position,
                selected=selected,
                explanation=_explain(entry, position, len(ranked), selected, config, members),
            )
        )
    unranked = [
        AssetScore(
            s.asset_id,
            s.symbol,
            None,
            s.weight_coverage,
            s.factors,
            explanation=(
                f"Not ranked: only {s.weight_coverage:.0%} of the scoring weight is computable "
                f"(minimum {config.min_weight_coverage:.0%}). {DISCLAIMER}"
            ),
        )
        for s in scored
        if s.overall is None
    ]
    return final + unranked


def _explain(
    item: AssetScore,
    rank: int,
    total: int,
    selected: bool,
    config: ScoringConfig,
    members: Mapping[uuid.UUID, bool],
) -> str:
    strongest = sorted((f for f in item.factors if f.available), key=lambda f: -(f.score or 0))
    parts = [f"{f.factor.replace('_', ' ')}: {f.explanation}" for f in strongest]
    missing = [f.factor.replace("_", " ") for f in item.factors if not f.available]
    if selected and rank > config.watchlist_size:
        status = (
            f"kept on the watchlist (rank {rank} is within size {config.watchlist_size} + "
            f"retention buffer {config.retention_buffer})"
        )
    elif selected:
        status = f"on the watchlist (top {config.watchlist_size})"
    elif members.get(item.asset_id):
        status = "leaves the watchlist (ranked outside size + retention buffer)"
    else:
        status = "not on the watchlist"
    text = (
        f"Rank {rank} of {total} qualified assets, score {item.overall} "
        f"({item.weight_coverage:.0%} of weight computable); {status}. " + "; ".join(parts) + "."
    )
    if missing:
        text += f" Not scored: {', '.join(missing)}."
    return f"{text} {DISCLAIMER}"


__all__ = ["DISCLAIMER", "AssetScore", "FactorScore", "ScoreInput", "score_assets"]
