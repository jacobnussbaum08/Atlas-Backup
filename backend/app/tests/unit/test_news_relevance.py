"""Explainable relevance ``relevance-v1`` (Phase 3C)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.news.relevance import (
    RELEVANCE_VERSION,
    headline_mentions,
    name_core,
    recency_weight,
    static_relevance,
)

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)


def _score(**overrides):
    values: dict[str, Any] = {
        "headline": "Fixture Corp Reports Results",
        "asset_symbol": "FXC",
        "asset_name": "Fixture Corp Common Stock",
        "tagged_symbols": 1,
        "mapped_assets": 1,
        "source": "fixturewire",
        "has_summary": False,
    } | overrides
    return static_relevance(**values)


def test_single_symbol_article_naming_the_company_scores_one():
    result = _score()
    assert result.score == Decimal("1.0000")
    assert result.factors["headline_mention"] == "name"
    assert result.factors["version"] == RELEVANCE_VERSION
    assert result.factors["source_reliability"] == "NOT_ASSESSED"
    assert result.factors["weights"]["source"] == "0"


def test_specificity_falls_with_the_number_of_tagged_symbols():
    roundup = _score(headline="Stocks Moving In Thursday's Session", tagged_symbols=33)
    assert roundup.score == Decimal("0.4091")  # 0.40 + 0.30 / 33, no headline mention
    assert roundup.factors["headline_mention"] is None
    pair = _score(headline="Two Names Rally", tagged_symbols=2)
    assert pair.score == Decimal("0.5500")


def test_headline_mention_by_symbol_or_distinctive_name():
    assert headline_mentions("FXC Shares Jump", "FXC", None) == "symbol"
    assert headline_mentions("Shares of AFXC jump", "FXC", None) is None  # whole token only
    assert headline_mentions("Apple CFO Says", "AAPL", "Apple Inc. Common Stock") == "name"
    assert name_core("Apple Inc. Common Stock") == "Apple"
    assert name_core("SPDR S&P 500 ETF Trust") == "SPDR S&P 500"
    assert name_core("Co") is None


def test_recency_halves_every_half_life_and_never_exceeds_one():
    assert recency_weight(T0, T0, 24) == 1.0
    assert recency_weight(T0, T0 + timedelta(hours=24), 24) == 0.5
    assert recency_weight(T0, T0 + timedelta(hours=48), 24) == 0.25
    assert recency_weight(T0, T0 - timedelta(hours=1), 24) == 1.0
