"""Dashboard notifications (spec §23); Telegram and email channels later.

A notification is a **view of a record Atlas already keeps**, never a second copy of it:

====================  =====================================================================
kind                  source (append-only or final records)
====================  =====================================================================
POSITION_OPENED       ``position_events`` OPENED (simulated paper position)
POSITION_CLOSED       ``position_events`` CLOSED, with its realized and net P/L
LARGE_PNL             a CLOSED position whose net P/L reaches ``large_pnl_fraction`` of the
                      account's contributed capital (gain or loss)
ORDER_REJECTED        ``orders`` that ended REJECTED or FAILED, with the engine's reason
TRADE_REJECTED        ``trade_intents`` RISK_REJECTED / EXECUTION_REFUSED / DEAD: the risk
                      or execution engine refused a prioritized decision
CIRCUIT_BREAKER       ``risk_circuit_breaker_events`` (tripped, recovered, reset)
SYSTEM_ALERT          ``system_health_events`` of HIGH or CRITICAL severity (data outages,
                      provider failures, worker failures, reconciliation discrepancies,
                      emergency stops, recoveries)
====================  =====================================================================

Every notification has a stable id (``<kind>:<source id>``), so a client can page with
``since`` and a channel can deliver each exactly once by remembering the last id it sent.
Minor market updates are never notifications. Nothing here writes to the database.

Channels implement :class:`NotificationChannel`. Only the dashboard channel exists: the
dashboard reads the feed. A Telegram or email channel will be a class with ``deliver``; it
must not be able to do anything but send text (no trading permission of any kind).
"""

from __future__ import annotations

from app.notifications.feed import (
    KINDS,
    Notification,
    NotificationChannel,
    NotificationSettings,
    feed,
)

__all__ = ["KINDS", "Notification", "NotificationChannel", "NotificationSettings", "feed"]
