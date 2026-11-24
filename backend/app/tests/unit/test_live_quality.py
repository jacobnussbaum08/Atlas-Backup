"""Live data: quote/trade quality rules, timestamps, health derivation, stream settings.

Constructed test values only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    StreamChannel,
    StreamHealth,
    StreamSubscriptionState,
)
from app.data_engine.providers.alpaca.normalize import BarFormatError, parse_timestamp_ns
from app.data_engine.schemas import ConnectionState, ProviderBar, ProviderQuote, ProviderTrade
from app.data_engine.stream_health import (
    MarketSession,
    StatusView,
    derive_health,
    us_equity_session,
)
from app.data_engine.validation import assess_quote, assess_trade

T0 = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)  # 09:30 New York (regular session)
COMMON = {
    "provider": "alpaca",
    "feed": "iex",
    "provider_symbol": "TEST",
    "source": DataSource.LIVE_STREAM,
    "event_time": T0,
    "received_at": T0,
    "available_at": T0,
}


def _quote(**values) -> ProviderQuote:
    base = {"bid_price": "10.00", "ask_price": "10.02", "bid_size": "1", "ask_size": "2"}
    return ProviderQuote(**(COMMON | base | values))


def _trade(**values) -> ProviderTrade:
    return ProviderTrade(**(COMMON | {"price": "10.01", "size": "5"} | values))


def _types(assessment) -> list[DataQualityEventType]:
    return [i.event_type for i in assessment.issues]


# ------------------------------------------------------------------ quotes
def test_normal_quote_is_valid():
    assessment = assess_quote(_quote())
    assert assessment.status is DataQualityStatus.VALID
    assert assessment.issues == ()


def test_crossed_quote_is_quarantined_locked_quote_is_a_warning():
    crossed = assess_quote(_quote(bid_price="10.05"))
    assert crossed.status is DataQualityStatus.QUARANTINED
    assert "crossed" in crossed.issues[0].message
    locked = assess_quote(_quote(bid_price="10.02"))
    assert locked.status is DataQualityStatus.VALID
    assert "locked" in locked.issues[0].message


def test_empty_book_side_is_kept_not_invented():
    # price 0 and size 0: no order on that side of the venue's book.
    assessment = assess_quote(_quote(bid_price="0", bid_size="0"))
    assert assessment.status is DataQualityStatus.VALID


@pytest.mark.parametrize(
    "values",
    [
        {"bid_price": "-1"},
        {"ask_size": "-3"},
        {"bid_price": "0", "bid_size": "4"},
    ],
)
def test_impossible_quotes_are_quarantined(values):
    assessment = assess_quote(_quote(**values))
    assert assessment.status is DataQualityStatus.QUARANTINED
    assert DataQualityEventType.INVALID_QUOTE in _types(assessment)


def test_missing_quote_price_is_a_warning_and_stays_missing():
    quote = _quote(bid_price=None, bid_size=None)
    assessment = assess_quote(quote)
    assert quote.bid_price is None
    assert assessment.status is DataQualityStatus.VALID
    assert _types(assessment) == [DataQualityEventType.MISSING_FIELD]


# ------------------------------------------------------------------ trades
def test_trade_rules():
    assert assess_trade(_trade()).status is DataQualityStatus.VALID
    assert assess_trade(_trade(price="0")).status is DataQualityStatus.QUARANTINED
    assert assess_trade(_trade(size="-1")).status is DataQualityStatus.QUARANTINED
    missing = assess_trade(_trade(size=None))
    assert missing.status is DataQualityStatus.VALID
    assert _types(missing) == [DataQualityEventType.MISSING_FIELD]


# ------------------------------------------------------------------ timestamps
def test_nanosecond_timestamps_are_exact():
    at, ns = parse_timestamp_ns("2026-01-02T14:30:00.999999999Z")
    assert ns == 1767364200999999999
    assert at == datetime(2026, 1, 2, 14, 30, 0, 999999, tzinfo=UTC)  # truncated, not rounded
    at, ns = parse_timestamp_ns("2026-01-02T09:30:00.5-05:00")
    assert at == datetime(2026, 1, 2, 14, 30, 0, 500000, tzinfo=UTC)
    assert ns == 1767364200500000000
    with pytest.raises(BarFormatError):
        parse_timestamp_ns("2026-01-02 14:30:00")
    with pytest.raises(BarFormatError):
        parse_timestamp_ns(1767364200)


def test_event_time_ns_must_agree_with_event_time():
    with pytest.raises(ValidationError, match="event_time_ns"):
        _trade(event_time_ns=1767364200999999999 + 10**9)
    assert _trade(event_time_ns=1767364200000000999).event_time_ns == 1767364200000000999


def test_live_bar_kind_must_match_source():
    bar = {
        **COMMON,
        "timeframe": "1Min",
        "adjustment": "RAW",
        "open": "1",
        "high": "1",
        "low": "1",
        "close": "1",
        "is_complete": True,
    }
    with pytest.raises(ValidationError, match="observation_kind"):
        ProviderBar(**bar)  # LIVE_STREAM with the default HISTORICAL kind
    live = ProviderBar(**bar, observation_kind=BarObservationKind.LIVE_INITIAL)
    assert live.observation_kind is BarObservationKind.LIVE_INITIAL
    with pytest.raises(ValidationError, match="observation_kind"):
        ProviderBar(
            **(bar | {"source": DataSource.HISTORICAL_REST}),
            observation_kind=BarObservationKind.LIVE_UPDATED,
        )


def test_live_records_keep_available_after_received():
    with pytest.raises(ValidationError, match="available_at"):
        _trade(available_at=T0 - timedelta(seconds=1))
    assert _trade(available_at=T0 + timedelta(seconds=1)).price == Decimal("10.01")


# ------------------------------------------------------------------ market sessions
@pytest.mark.parametrize(
    ("at", "session"),
    [
        (datetime(2026, 1, 2, 14, 30, tzinfo=UTC), MarketSession.REGULAR),  # Fri 09:30 EST
        (datetime(2026, 1, 2, 14, 29, tzinfo=UTC), MarketSession.PRE_MARKET),
        (datetime(2026, 1, 2, 21, 0, tzinfo=UTC), MarketSession.AFTER_HOURS),  # 16:00 EST
        (datetime(2026, 1, 3, 1, 0, tzinfo=UTC), MarketSession.CLOSED),  # Fri 20:00 EST
        (datetime(2026, 1, 3, 15, 0, tzinfo=UTC), MarketSession.CLOSED),  # Saturday
        (datetime(2026, 7, 1, 13, 30, tzinfo=UTC), MarketSession.REGULAR),  # 09:30 EDT
        (datetime(2026, 7, 1, 13, 29, tzinfo=UTC), MarketSession.PRE_MARKET),
    ],
)
def test_us_equity_sessions(at, session):
    assert us_equity_session(at) is session


# ------------------------------------------------------------------ health
NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)  # regular session
STALE = timedelta(seconds=180)


def _view(**values) -> StatusView:
    base: dict[str, Any] = {
        "enabled": True,
        "connection_state": ConnectionState.SUBSCRIBED,
        "subscription_state": StreamSubscriptionState.ACTIVE,
        "heartbeat_at": NOW - timedelta(seconds=2),
        "last_subscribed_at": NOW - timedelta(minutes=30),
        "last_message_at": NOW - timedelta(seconds=5),
        "last_persisted_at": NOW - timedelta(seconds=4),
        "metrics": {},
    }
    return StatusView(**(base | values))


def _health(view, now=NOW, enabled=True):
    return derive_health(view, enabled=enabled, now=now, stale_after=STALE)


def test_health_healthy_only_when_subscribed_and_persisting():
    assert _health(_view()).health is StreamHealth.HEALTHY


def test_health_without_worker():
    assert _health(None, enabled=False).health is StreamHealth.DISABLED
    assert _health(None).health is StreamHealth.NOT_RUNNING
    stopped = _view(connection_state=ConnectionState.STOPPED)
    assert _health(stopped).health is StreamHealth.NOT_RUNNING


def test_connected_but_dead_worker_is_not_healthy():
    report = _health(_view(heartbeat_at=NOW - timedelta(minutes=5)))
    assert report.health is StreamHealth.NOT_RUNNING
    assert "heartbeat" in report.reasons[0]


@pytest.mark.parametrize(
    ("state", "subscribed_before", "expected"),
    [
        (ConnectionState.CONNECTING, None, StreamHealth.STARTING),
        (ConnectionState.RECONNECTING, NOW, StreamHealth.RECONNECTING),
        (ConnectionState.DISCONNECTED, NOW, StreamHealth.RECONNECTING),
        (ConnectionState.FAILED, None, StreamHealth.FAILED),
    ],
)
def test_connection_states_map_to_health(state, subscribed_before, expected):
    view = _view(connection_state=state, last_subscribed_at=subscribed_before)
    assert _health(view).health is expected


def test_quiet_stream_is_stale_in_the_regular_session_but_idle_when_closed():
    quiet = _view(
        last_message_at=NOW - timedelta(minutes=10), last_persisted_at=NOW - timedelta(minutes=10)
    )
    assert _health(quiet).health is StreamHealth.STALE
    saturday = datetime(2026, 1, 3, 15, 0, tzinfo=UTC)
    quiet_weekend = _view(
        heartbeat_at=saturday,
        last_message_at=saturday - timedelta(hours=10),
        last_persisted_at=saturday - timedelta(hours=10),
    )
    report = _health(quiet_weekend, now=saturday)
    assert report.health is StreamHealth.IDLE
    assert report.session is MarketSession.CLOSED
    assert "holidays" in report.calendar


def test_degraded_conditions():
    partial = _view(subscription_state=StreamSubscriptionState.PARTIAL)
    assert _health(partial).health is StreamHealth.DEGRADED
    dropped = _view(metrics={"last_drop_at": (NOW - timedelta(minutes=1)).isoformat()})
    assert _health(dropped).health is StreamHealth.DEGRADED
    old_drop = _view(metrics={"last_drop_at": (NOW - timedelta(hours=2)).isoformat()})
    assert _health(old_drop).health is StreamHealth.HEALTHY
    failing = _view(metrics={"persistence_failing": True})
    assert _health(failing).health is StreamHealth.DEGRADED
    not_persisting = _view(
        last_message_at=NOW - timedelta(minutes=5), last_persisted_at=NOW - timedelta(minutes=30)
    )
    assert _health(not_persisting).health is StreamHealth.DEGRADED


# ------------------------------------------------------------------ settings
def test_stream_channel_settings(clean_env):
    default = MarketDataSettings(_env_file=None)
    assert default.market_data_stream_channels == (
        StreamChannel.BARS,
        StreamChannel.UPDATED_BARS,
        StreamChannel.TRADES,
        StreamChannel.QUOTES,
    )
    assert default.market_data_stream_enabled is False
    custom = MarketDataSettings(_env_file=None, market_data_stream_channels="Trades, daily_bars")
    assert custom.market_data_stream_channels == (StreamChannel.TRADES, StreamChannel.DAILY_BARS)
    for bad in ("trades,trades", "orders", ""):
        with pytest.raises(ValidationError):
            MarketDataSettings(_env_file=None, market_data_stream_channels=bad)
    with pytest.raises(ValidationError):
        MarketDataSettings(_env_file=None, market_data_stream_queue_size=1)
    with pytest.raises(ValidationError):
        MarketDataSettings(_env_file=None, market_data_stream_stale_after=1)


def test_a_fresh_subscription_without_data_is_never_reported_healthy():
    fresh = _view(
        last_subscribed_at=NOW - timedelta(seconds=10), last_message_at=None, last_persisted_at=None
    )
    report = _health(fresh)
    assert report.health is StreamHealth.IDLE
    assert "waiting for the first observation" in report.reasons[0]
    closed = datetime(2026, 1, 3, 15, 0, tzinfo=UTC)
    fresh_weekend = _view(
        heartbeat_at=closed,
        last_subscribed_at=closed - timedelta(seconds=10),
        last_message_at=None,
        last_persisted_at=None,
    )
    assert _health(fresh_weekend, now=closed).health is StreamHealth.IDLE
