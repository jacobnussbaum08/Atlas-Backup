"""Data-quality rules for bars, quotes and trades (spec §7.5, Phase 2 Part 8).

Two kinds of finding:

* **Rejection** (``check_request_consistency``): the record does not belong to the
  request (wrong symbol, outside the requested range). It is not stored; a quality
  event records why.
* **Assessment** (``assess_bar``): the record belongs to the request but its content
  is checked. Impossible values make it ``QUARANTINED``: stored for audit, never
  served as normal data. A bar whose interval had not ended when retrieved is
  ``INCOMPLETE``. Otherwise ``VALID``; warnings do not change the status.

Nothing is ever corrected silently: a finding either stops the record or labels it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.data_engine.enums import DataQualityEventType, DataQualityStatus, Severity
from app.data_engine.schemas import ProviderBar, ProviderQuote, ProviderTrade
from app.data_engine.timeframes import is_aligned


@dataclass(frozen=True)
class QualityIssue:
    event_type: DataQualityEventType
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BarAssessment:
    status: DataQualityStatus
    issues: tuple[QualityIssue, ...]


def _values(bar: ProviderBar) -> dict[str, str | None]:
    return {
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": None if bar.volume is None else str(bar.volume),
        "vwap": None if bar.vwap is None else str(bar.vwap),
        "trade_count": None if bar.trade_count is None else str(bar.trade_count),
    }


def check_request_consistency(
    bar: ProviderBar,
    *,
    expected_symbol: str,
    expected_feed: str,
    range_start: datetime,
    range_end: datetime,
) -> QualityIssue | None:
    """A reason to reject ``bar`` because it does not belong to the request, or ``None``."""
    if bar.provider_symbol != expected_symbol:
        return QualityIssue(
            DataQualityEventType.UNEXPECTED_SYMBOL,
            Severity.ERROR,
            f"bar for '{bar.provider_symbol}' in a response for '{expected_symbol}'",
        )
    if bar.feed != expected_feed:
        return QualityIssue(
            DataQualityEventType.REJECTED_MESSAGE,
            Severity.ERROR,
            f"bar labelled with feed '{bar.feed}' in a request for feed '{expected_feed}'",
        )
    if not range_start <= bar.event_time <= range_end:
        return QualityIssue(
            DataQualityEventType.OUT_OF_REQUESTED_RANGE,
            Severity.ERROR,
            f"bar at {bar.event_time.isoformat()} is outside the requested range",
            {"range_start": range_start.isoformat(), "range_end": range_end.isoformat()},
        )
    return None


def assess_bar(bar: ProviderBar) -> BarAssessment:
    quarantine: list[QualityIssue] = []
    warnings: list[QualityIssue] = []
    values = _values(bar)

    prices = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
    non_positive = [name for name, value in prices.items() if value <= 0]
    if non_positive:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_PRICE,
                Severity.ERROR,
                f"non-positive price(s): {', '.join(non_positive)}",
                values,
            )
        )

    ohlc_violations = []
    if bar.high < bar.open:
        ohlc_violations.append("high < open")
    if bar.high < bar.close:
        ohlc_violations.append("high < close")
    if bar.low > bar.open:
        ohlc_violations.append("low > open")
    if bar.low > bar.close:
        ohlc_violations.append("low > close")
    if bar.high < bar.low:
        ohlc_violations.append("high < low")
    if ohlc_violations:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_OHLC,
                Severity.ERROR,
                f"impossible OHLC: {', '.join(ohlc_violations)}",
                values,
            )
        )

    if bar.volume is not None and bar.volume < 0:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_VOLUME, Severity.ERROR, "negative volume", values
            )
        )
    if bar.trade_count is not None and bar.trade_count < 0:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_VOLUME, Severity.ERROR, "negative trade count", values
            )
        )

    if bar.vwap is not None:
        if bar.vwap <= 0:
            quarantine.append(
                QualityIssue(
                    DataQualityEventType.INVALID_PRICE, Severity.ERROR, "non-positive VWAP", values
                )
            )
        elif not bar.low <= bar.vwap <= bar.high:
            warnings.append(
                QualityIssue(
                    DataQualityEventType.INVALID_PRICE,
                    Severity.WARNING,
                    "VWAP lies outside the bar's low-high range",
                    values,
                )
            )

    if is_aligned(bar.event_time, bar.timeframe) is False:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.TIMEFRAME_MISALIGNED,
                Severity.ERROR,
                f"bar start {bar.event_time.isoformat()} is not on the {bar.timeframe} grid",
            )
        )

    if bar.volume is None:
        warnings.append(
            QualityIssue(
                DataQualityEventType.MISSING_FIELD,
                Severity.WARNING,
                "provider reported no volume; stored as missing (NULL), not zero",
            )
        )

    if quarantine:
        status = DataQualityStatus.QUARANTINED
    elif not bar.is_complete:
        status = DataQualityStatus.INCOMPLETE
    else:
        status = DataQualityStatus.VALID
    return BarAssessment(status=status, issues=tuple(quarantine + warnings))


def same_observation(bar: ProviderBar, stored: Any) -> bool:
    """True when ``bar`` carries exactly the values of a stored bar row."""
    return bool(
        bar.open == stored.open
        and bar.high == stored.high
        and bar.low == stored.low
        and bar.close == stored.close
        and bar.volume == stored.volume
        and bar.vwap == stored.vwap
        and bar.trade_count == stored.trade_count
        and bar.is_complete == stored.is_complete
    )


# ------------------------------------------------------------------ quotes and trades
@dataclass(frozen=True)
class ObservationAssessment:
    status: DataQualityStatus  # VALID or QUARANTINED
    issues: tuple[QualityIssue, ...]


def _assessment(
    quarantine: list[QualityIssue], warnings: list[QualityIssue]
) -> ObservationAssessment:
    status = DataQualityStatus.QUARANTINED if quarantine else DataQualityStatus.VALID
    return ObservationAssessment(status=status, issues=tuple(quarantine + warnings))


def assess_quote(quote: ProviderQuote) -> ObservationAssessment:
    """Quote rules. A side with price 0 and size 0 means "no order on that side" at the
    venue (empty book side); it is kept as reported, not treated as a price."""
    quarantine: list[QualityIssue] = []
    warnings: list[QualityIssue] = []
    values = {
        "bid_price": None if quote.bid_price is None else str(quote.bid_price),
        "ask_price": None if quote.ask_price is None else str(quote.ask_price),
        "bid_size": None if quote.bid_size is None else str(quote.bid_size),
        "ask_size": None if quote.ask_size is None else str(quote.ask_size),
    }
    sides = {
        "bid": (quote.bid_price, quote.bid_size),
        "ask": (quote.ask_price, quote.ask_size),
    }
    for side, (price, size) in sides.items():
        if price is None:
            warnings.append(
                QualityIssue(
                    DataQualityEventType.MISSING_FIELD,
                    Severity.WARNING,
                    f"quote has no {side} price; stored as missing",
                )
            )
        elif price < 0:
            quarantine.append(
                QualityIssue(
                    DataQualityEventType.INVALID_QUOTE,
                    Severity.ERROR,
                    f"negative {side} price",
                    values,
                )
            )
        elif price == 0 and size is not None and size > 0:
            quarantine.append(
                QualityIssue(
                    DataQualityEventType.INVALID_QUOTE,
                    Severity.ERROR,
                    f"{side} size quoted at a price of zero",
                    values,
                )
            )
        if size is not None and size < 0:
            quarantine.append(
                QualityIssue(
                    DataQualityEventType.INVALID_QUOTE,
                    Severity.ERROR,
                    f"negative {side} size",
                    values,
                )
            )
    bid, ask = quote.bid_price, quote.ask_price
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        if bid > ask:
            quarantine.append(
                QualityIssue(
                    DataQualityEventType.INVALID_QUOTE,
                    Severity.ERROR,
                    "crossed quote: bid above ask",
                    values,
                )
            )
        elif bid == ask:
            warnings.append(
                QualityIssue(
                    DataQualityEventType.INVALID_QUOTE,
                    Severity.WARNING,
                    "locked quote: bid equals ask",
                    values,
                )
            )
    return _assessment(quarantine, warnings)


def assess_trade(trade: ProviderTrade) -> ObservationAssessment:
    quarantine: list[QualityIssue] = []
    warnings: list[QualityIssue] = []
    values = {
        "price": str(trade.price),
        "size": None if trade.size is None else str(trade.size),
    }
    if trade.price <= 0:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_PRICE,
                Severity.ERROR,
                "non-positive trade price",
                values,
            )
        )
    if trade.size is None:
        warnings.append(
            QualityIssue(
                DataQualityEventType.MISSING_FIELD,
                Severity.WARNING,
                "trade has no size; stored as missing (NULL), not zero",
            )
        )
    elif trade.size < 0:
        quarantine.append(
            QualityIssue(
                DataQualityEventType.INVALID_VOLUME, Severity.ERROR, "negative trade size", values
            )
        )
    return _assessment(quarantine, warnings)
