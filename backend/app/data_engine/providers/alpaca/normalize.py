"""Normalize Alpaca JSON (REST bars and live stream messages) into ``Provider*`` records.

Alpaca bar fields: ``t`` (RFC-3339 bar start, UTC), ``o``/``h``/``l``/``c`` (prices),
``v`` (volume), ``n`` (trade count), ``vw`` (VWAP). Numbers must arrive as
``Decimal``/``int`` (the response is parsed with ``parse_float=Decimal``) so no
binary floating point is involved. Absent optional fields stay ``None``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.core.config.settings import describe_validation_error
from app.data_engine.enums import BarObservationKind, DataSource, PriceAdjustment
from app.data_engine.schemas import ProviderBar, ProviderQuote, ProviderTrade, epoch_microseconds
from app.data_engine.timeframes import DAY_1, MINUTE_1, Timeframe, bar_interval_end

_RFC3339 = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{1,9}))?(?P<tz>Z|[+-]\d{2}:\d{2})$"
)


class BarFormatError(ValueError):
    pass


def parse_timestamp(text: object) -> datetime:
    """RFC-3339 to aware UTC. Sub-microsecond precision is rejected rather than truncated."""
    if not isinstance(text, str):
        raise BarFormatError("timestamp is not a string")
    match = _RFC3339.match(text)
    if not match:
        raise BarFormatError(f"invalid RFC-3339 timestamp {text!r}")
    fraction = match["fraction"] or ""
    if len(fraction) > 6 and int(fraction[6:]) != 0:
        raise BarFormatError(f"timestamp {text!r} has sub-microsecond precision")
    micro = (fraction[:6]).ljust(6, "0") if fraction else "000000"
    tz = "+00:00" if match["tz"] == "Z" else match["tz"]
    return datetime.fromisoformat(f"{match['base']}.{micro}{tz}").astimezone(UTC)


def _number(item: dict[str, Any], key: str, *, required: bool) -> Decimal | None:
    value = item.get(key)
    if value is None:
        if required:
            raise BarFormatError(f"missing required field '{key}'")
        return None
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise BarFormatError(f"field '{key}' is not a number")
    return Decimal(value)


def normalize_bar(
    item: object,
    *,
    provider_symbol: str,
    feed: str,
    timeframe: Timeframe,
    adjustment: PriceAdjustment,
    received_at: datetime,
) -> ProviderBar:
    if not isinstance(item, dict):
        raise BarFormatError("bar is not an object")
    event_time = parse_timestamp(item.get("t"))
    trade_count = item.get("n")
    if trade_count is not None and (
        isinstance(trade_count, bool) or not isinstance(trade_count, int)
    ):
        raise BarFormatError("field 'n' is not an integer")
    end = bar_interval_end(event_time, timeframe)
    try:
        return ProviderBar(
            provider="alpaca",
            feed=feed,
            provider_symbol=provider_symbol,
            source=DataSource.HISTORICAL_REST,
            timeframe=timeframe,
            adjustment=adjustment,
            event_time=event_time,
            received_at=received_at,
            available_at=received_at,  # available to Atlas on retrieval; original time unknown
            open=_number(item, "o", required=True),
            high=_number(item, "h", required=True),
            low=_number(item, "l", required=True),
            close=_number(item, "c", required=True),
            volume=_number(item, "v", required=False),
            vwap=_number(item, "vw", required=False),
            trade_count=trade_count,
            # A bar whose interval had not ended when retrieved may still change.
            is_complete=end is not None and end <= received_at,
        )
    except ValidationError as exc:
        raise BarFormatError(describe_validation_error(exc, "bar")) from None


# ------------------------------------------------------------------ live stream messages
# Stream message schemas (https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data):
#   bars "b"/"u"/"d": S, o, h, l, c, v, vw, n, t (bar start, minute resolution)
#   trade "t": S, i (trade ID), x (exchange), p, s, c (conditions), t (ns), z (tape)
#   quote "q": S, bx, bp, bs, ax, ap, as, c, t (ns), z
# Corrections ("c") and cancel/errors ("x") are reported as notices by the stream.

_BAR_KINDS = {
    "b": (MINUTE_1, BarObservationKind.LIVE_INITIAL),
    "u": (MINUTE_1, BarObservationKind.LIVE_UPDATED),
    "d": (DAY_1, BarObservationKind.LIVE_RUNNING),
}
BAR_MESSAGE_TYPES = frozenset(_BAR_KINDS)


def parse_timestamp_ns(text: object) -> tuple[datetime, int]:
    """RFC-3339 with up to nanosecond precision -> (UTC datetime truncated to µs, exact ns).

    Truncation only affects the ``datetime`` (PostgreSQL's resolution); the exact value
    is returned as integer nanoseconds since the Unix epoch.
    """
    if not isinstance(text, str):
        raise BarFormatError("timestamp is not a string")
    match = _RFC3339.match(text)
    if not match:
        raise BarFormatError(f"invalid RFC-3339 timestamp {text!r}")
    fraction = (match["fraction"] or "").ljust(9, "0")
    tz = "+00:00" if match["tz"] == "Z" else match["tz"]
    whole = datetime.fromisoformat(f"{match['base']}{tz}").astimezone(UTC)
    nanos = epoch_microseconds(whole) * 1000 + int(fraction)
    return whole + timedelta(microseconds=int(fraction[:6])), nanos


def _text(item: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = item.get(key)
    if value is None:
        if required:
            raise BarFormatError(f"missing required field '{key}'")
        return None
    if not isinstance(value, str):
        raise BarFormatError(f"field '{key}' is not a string")
    return value


def _conditions(item: dict[str, Any]) -> tuple[str, ...]:
    value = item.get("c")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise BarFormatError("field 'c' is not a list of strings")
    return tuple(value)


def _symbol(item: dict[str, Any]) -> str:
    value = item.get("S")
    if not isinstance(value, str) or not value:
        raise BarFormatError("missing required field 'S'")
    return value


def normalize_stream_bar(item: dict[str, Any], *, feed: str, received_at: datetime) -> ProviderBar:
    """A live bar. Never assumed final: see ``BarObservationKind``."""
    timeframe, kind = _BAR_KINDS[str(item.get("T"))]
    symbol = _symbol(item)
    event_time = parse_timestamp(item.get("t"))
    trade_count = item.get("n")
    if trade_count is not None and (
        isinstance(trade_count, bool) or not isinstance(trade_count, int)
    ):
        raise BarFormatError("field 'n' is not an integer")
    end = bar_interval_end(event_time, timeframe)
    try:
        return ProviderBar(
            provider="alpaca",
            feed=feed,
            provider_symbol=symbol,
            source=DataSource.LIVE_STREAM,
            observation_kind=kind,
            timeframe=timeframe,
            # Stream bars are built from trades as reported: unadjusted.
            adjustment=PriceAdjustment.RAW,
            event_time=event_time,
            received_at=received_at,
            available_at=received_at,
            original_available_at=received_at,
            open=_number(item, "o", required=True),
            high=_number(item, "h", required=True),
            low=_number(item, "l", required=True),
            close=_number(item, "c", required=True),
            volume=_number(item, "v", required=False),
            vwap=_number(item, "vw", required=False),
            trade_count=trade_count,
            is_complete=end is not None and end <= received_at,
        )
    except ValidationError as exc:
        raise BarFormatError(describe_validation_error(exc, "bar")) from None


def _delivery(source: DataSource, received_at: datetime) -> datetime | None:
    """Original delivery time: the stream's arrival, unknown for history retrieved later."""
    return received_at if source is DataSource.LIVE_STREAM else None


def normalize_quote(
    item: dict[str, Any],
    *,
    feed: str,
    received_at: datetime,
    source: DataSource = DataSource.LIVE_STREAM,
) -> ProviderQuote:
    """A stream quote, or a historical one (REST items carry no ``S``: pass it in)."""
    symbol = _symbol(item)
    event_time, nanos = parse_timestamp_ns(item.get("t"))
    try:
        return ProviderQuote(
            provider="alpaca",
            feed=feed,
            provider_symbol=symbol,
            source=source,
            event_time=event_time,
            event_time_ns=nanos,
            received_at=received_at,
            available_at=received_at,
            original_available_at=_delivery(source, received_at),
            bid_price=_number(item, "bp", required=False),
            ask_price=_number(item, "ap", required=False),
            bid_size=_number(item, "bs", required=False),
            ask_size=_number(item, "as", required=False),
            bid_exchange=_text(item, "bx"),
            ask_exchange=_text(item, "ax"),
            conditions=_conditions(item),
            tape=_text(item, "z"),
        )
    except ValidationError as exc:
        raise BarFormatError(describe_validation_error(exc, "quote")) from None


def normalize_trade(
    item: dict[str, Any],
    *,
    feed: str,
    received_at: datetime,
    source: DataSource = DataSource.LIVE_STREAM,
) -> ProviderTrade:
    symbol = _symbol(item)
    event_time, nanos = parse_timestamp_ns(item.get("t"))
    trade_id = item.get("i")
    if trade_id is not None and (isinstance(trade_id, bool) or not isinstance(trade_id, int)):
        raise BarFormatError("field 'i' is not an integer")
    try:
        return ProviderTrade(
            provider="alpaca",
            feed=feed,
            provider_symbol=symbol,
            source=source,
            event_time=event_time,
            event_time_ns=nanos,
            received_at=received_at,
            available_at=received_at,
            original_available_at=_delivery(source, received_at),
            price=_number(item, "p", required=True),
            size=_number(item, "s", required=False),
            provider_trade_id=None if trade_id is None else str(trade_id),
            exchange=_text(item, "x"),
            conditions=_conditions(item),
            tape=_text(item, "z"),
        )
    except ValidationError as exc:
        raise BarFormatError(describe_validation_error(exc, "trade")) from None
