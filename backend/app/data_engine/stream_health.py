"""Stream health derivation (Deliverable 2C).

A connected stream is not necessarily healthy. Health is derived from the worker's
persisted status, the time, and the US equity session:

* No recent worker heartbeat -> ``NOT_RUNNING`` (stopped or crashed), whatever the last
  recorded state says.
* Permanent failure -> ``FAILED``; transport down -> ``RECONNECTING``.
* Subscribed: partial subscription, dropped frames or failing writes -> ``DEGRADED``;
  during the regular session, nothing persisted for ``stale_after`` -> ``STALE``;
  outside it, quiet is expected (IEX is sparse in extended hours) -> ``IDLE``.

Sessions come from the US equity calendar (``app.data_engine.calendar``, Deliverable 2D):
weekends, NYSE holidays and early closes are closed or shortened, so a quiet stream on a
holiday is ``IDLE``, not ``STALE``. Holiday dates are verified for 2025-2027; ``calendar``
in the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.data_engine.calendar import MarketSession, session_at
from app.data_engine.enums import StreamHealth, StreamSubscriptionState
from app.data_engine.schemas import ConnectionState

CALENDAR_NOTE = "NYSE holidays and early closes (rules verified against NYSE for 2025-2027)"
# The worker writes its status every few seconds; this much silence means it is gone.
WORKER_HEARTBEAT_STALE_AFTER = timedelta(seconds=30)
# Data loss or write failures this recent keep the stream DEGRADED.
DEGRADED_WINDOW = timedelta(minutes=15)


def us_equity_session(at: datetime) -> MarketSession:
    return session_at(at)


@dataclass(frozen=True)
class StatusView:
    """The fields of a stream status row that health depends on."""

    enabled: bool
    connection_state: ConnectionState
    subscription_state: StreamSubscriptionState
    heartbeat_at: datetime
    last_subscribed_at: datetime | None
    last_message_at: datetime | None
    last_persisted_at: datetime | None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    health: StreamHealth
    session: MarketSession
    reasons: tuple[str, ...]
    calendar: str = CALENDAR_NOTE


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def derive_health(
    status: StatusView | None, *, enabled: bool, now: datetime, stale_after: timedelta
) -> HealthReport:
    session = us_equity_session(now)

    def report(health: StreamHealth, *reasons: str) -> HealthReport:
        return HealthReport(health=health, session=session, reasons=reasons)

    if status is None:
        if not enabled:
            return report(StreamHealth.DISABLED, "MARKET_DATA_STREAM_ENABLED is false")
        return report(StreamHealth.NOT_RUNNING, "no stream worker has reported for this feed")
    state = status.connection_state
    if state is ConnectionState.DISABLED:
        return report(StreamHealth.DISABLED, "the worker reported streaming disabled")
    if state is ConnectionState.STOPPED:
        return report(StreamHealth.NOT_RUNNING, "the worker stopped")
    if now - status.heartbeat_at > WORKER_HEARTBEAT_STALE_AFTER:
        age = int((now - status.heartbeat_at).total_seconds())
        return report(
            StreamHealth.NOT_RUNNING,
            f"no worker heartbeat for {age}s (last state {state.value}); the worker is not running",
        )
    if state is ConnectionState.FAILED:
        return report(StreamHealth.FAILED, "permanent failure; the worker is not retrying")
    if state is not ConnectionState.SUBSCRIBED:
        if status.last_subscribed_at is None:
            return report(StreamHealth.STARTING, f"not yet subscribed ({state.value})")
        return report(StreamHealth.RECONNECTING, f"connection is {state.value}")

    degraded: list[str] = []
    if status.subscription_state is not StreamSubscriptionState.ACTIVE:
        degraded.append(f"subscription is {status.subscription_state.value}")
    last_drop = _parse(status.metrics.get("last_drop_at"))
    if last_drop is not None and now - last_drop <= DEGRADED_WINDOW:
        degraded.append(f"received data was dropped under backpressure at {last_drop.isoformat()}")
    if status.metrics.get("persistence_failing"):
        degraded.append("writes to PostgreSQL are failing; data is queued in memory")
    if (
        status.last_message_at is not None
        and (status.last_persisted_at is None or status.last_persisted_at < status.last_message_at)
        and now - status.last_message_at > stale_after
    ):
        degraded.append("received data has not been persisted")
    if degraded:
        return report(StreamHealth.DEGRADED, *degraded)

    persisted = status.last_persisted_at
    if persisted is not None and now - persisted <= stale_after:
        return report(StreamHealth.HEALTHY, "subscribed and persisting data")
    # Nothing persisted recently. HEALTHY is never assumed from a subscription alone.
    quiet_since = persisted or status.last_subscribed_at
    threshold = int(stale_after.total_seconds())
    if session is MarketSession.REGULAR:
        if quiet_since is None or now - quiet_since > stale_after:
            since = quiet_since.isoformat() if quiet_since else "subscription"
            return report(
                StreamHealth.STALE,
                f"no observation persisted since {since} during the regular session "
                f"(threshold {threshold}s)",
            )
        return report(
            StreamHealth.IDLE,
            f"subscribed; waiting for the first observation (STALE after {threshold}s)",
        )
    return report(
        StreamHealth.IDLE,
        f"subscribed; no recent data, which is expected in session {session.value} on this feed",
    )


__all__ = [
    "CALENDAR_NOTE",
    "HealthReport",
    "MarketSession",
    "StatusView",
    "derive_health",
    "us_equity_session",
]
