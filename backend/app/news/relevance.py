"""Explainable news relevance (Phase 3C, D-078), methodology ``relevance-v1``.

Relevance orders articles for an asset or a feed. It measures how directly an article
concerns an asset and how recent it is. It is **not** sentiment, importance, surprise or
expected price impact, and it predicts nothing; the weights are documented starting
points, not fitted values.

Static relevance of an article to one mapped asset (stored with its factors):

    0.40                               the provider tagged the asset's symbol (direct link)
  + 0.30 x specificity                 1 / number of symbols the article is tagged with
  + 0.30 x headline_mention            1 if the headline names the asset's symbol or name

so a single-symbol article whose headline names the company scores 1.0, and one of 33
tickers in a market round-up scores about 0.41. Source information is recorded as a factor
but weighted 0: Atlas has no basis yet for rating publishers (``NOT_ASSESSED``).

Recency is a query-time weight, never stored (it depends on when one asks):

    recency = 0.5 ^ (age_hours / NEWS_RELEVANCE_HALF_LIFE_HOURS)   (age from publication)
    combined = static x recency
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

RELEVANCE_VERSION = "relevance-v1"
DIRECT_WEIGHT = Decimal("0.40")
SPECIFICITY_WEIGHT = Decimal("0.30")
HEADLINE_WEIGHT = Decimal("0.30")
_FOUR_PLACES = Decimal("0.0001")

# Corporate suffixes and share-class words removed before looking for a name in a headline.
_NAME_NOISE = re.compile(
    r"\b(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|l\.p|sa|nv|ag|"
    r"se|holdings?|group|common stock|ordinary shares?|class [a-z]|american depositary shares?|"
    r"ads|adr|shares?|units?|etf|trust|the)\b\.?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StaticRelevance:
    score: Decimal
    factors: dict[str, Any]


def name_core(name: str | None) -> str | None:
    """The distinctive part of an issuer name, e.g. 'Apple Inc. Common Stock' -> 'Apple'."""
    if not name:
        return None
    core = _NAME_NOISE.sub(" ", name)
    core = " ".join(re.sub(r"[^\w&'.-]+", " ", core).split()).strip(" .,-")
    return core if len(core) >= 3 else None


def headline_mentions(headline: str, symbol: str | None, name: str | None) -> str | None:
    """What in the headline names the asset ('symbol' or 'name'), if anything."""
    if symbol and re.search(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])", headline):
        return "symbol"
    core = name_core(name)
    if core and re.search(rf"\b{re.escape(core)}\b", headline, re.IGNORECASE):
        return "name"
    return None


def static_relevance(
    *,
    headline: str,
    asset_symbol: str | None,
    asset_name: str | None,
    tagged_symbols: int,
    mapped_assets: int,
    source: str | None,
    has_summary: bool,
) -> StaticRelevance:
    specificity = Decimal(1) / Decimal(max(tagged_symbols, 1))
    mention = headline_mentions(headline, asset_symbol, asset_name)
    score = DIRECT_WEIGHT + SPECIFICITY_WEIGHT * specificity
    if mention is not None:
        score += HEADLINE_WEIGHT
    score = min(Decimal(1), score).quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN)
    return StaticRelevance(
        score=score,
        factors={
            "version": RELEVANCE_VERSION,
            "direct_association": True,
            "tagged_symbols": tagged_symbols,
            "mapped_assets": mapped_assets,
            "specificity": str(specificity.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN)),
            "headline_mention": mention,
            "has_summary": has_summary,
            "source": source,
            "source_reliability": "NOT_ASSESSED",
            "weights": {
                "direct": str(DIRECT_WEIGHT),
                "specificity": str(SPECIFICITY_WEIGHT),
                "headline_mention": str(HEADLINE_WEIGHT),
                "source": "0",
            },
        },
    )


def recency_weight(published_at: datetime, at: datetime, half_life_hours: float) -> float:
    """1.0 at publication, halving every half-life; 1.0 for times before publication."""
    age_hours = max((at - published_at).total_seconds() / 3600.0, 0.0)
    return float(0.5 ** (age_hours / half_life_hours))


__all__ = [
    "RELEVANCE_VERSION",
    "StaticRelevance",
    "headline_mentions",
    "name_core",
    "recency_weight",
    "static_relevance",
]
