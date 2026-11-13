"""The notification feed (spec §23). See the package docstring for the sources."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.execution import OrderRecord, PaperPositionRecord, PositionEventRecord
from app.database.models.orchestration import TradeIntentRecord
from app.database.models.portfolio import PortfolioSnapshotRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.database.models.risk import RiskCircuitBreakerEventRecord
from app.execution.enums import OrderStatus, PositionEventType
from app.orchestration.enums import IntentStatus
from app.risk.enums import Severity

KINDS = (
    "POSITION_OPENED",
    "POSITION_CLOSED",
    "LARGE_PNL",
    "ORDER_REJECTED",
    "TRADE_REJECTED",
    "CIRCUIT_BREAKER",
    "SYSTEM_ALERT",
)
REJECTED_INTENTS = (IntentStatus.RISK_REJECTED, IntentStatus.EXECUTION_REFUSED, IntentStatus.DEAD)
REJECTED_ORDERS = (OrderStatus.REJECTED, OrderStatus.FAILED)
ALERT_SEVERITIES = (Severity.HIGH, Severity.CRITICAL)


@dataclass(frozen=True)
class NotificationSettings:
    # A closed position whose net P/L is at least this fraction of the contributed capital
    # (gain or loss) is also an "important P/L event". 0.5% = one full initial risk budget.
    large_pnl_fraction: Decimal = Decimal("0.005")
    per_source_limit: int = 200


@dataclass(frozen=True)
class Notification:
    id: str
    kind: str
    severity: str  # INFO, WARNING, HIGH, CRITICAL
    occurred_at: datetime
    title: str
    message: str
    subject: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "occurred_at": self.occurred_at.isoformat(),
            "title": self.title,
            "message": self.message,
            "subject": self.subject,
            "simulated": True,
        }


class NotificationChannel(Protocol):
    """Delivers notifications outside the dashboard (Telegram, email: not built yet). A
    channel only sends text; it has no access to trading, risk or account controls."""

    name: str

    def deliver(self, notifications: list[Notification]) -> int:
        """Send these (oldest first); return how many were sent."""
        ...


def _reasons(reasons: Any) -> str:
    if not reasons:
        return "no reason recorded"
    codes = [str(r.get("code")) for r in reasons if isinstance(r, dict) and r.get("code")]
    return ", ".join(codes[:5]) or "no reason recorded"


def _positions(
    session: Session, account_id: uuid.UUID, since: datetime | None, cfg: NotificationSettings
) -> list[Notification]:
    e, p = PositionEventRecord, PaperPositionRecord
    q = (
        select(e, p)
        .join(p, p.id == e.position_id)
        .where(
            p.portfolio_account_id == account_id,
            e.event_type.in_([PositionEventType.OPENED, PositionEventType.CLOSED]),
        )
        .order_by(e.occurred_at.desc())
        .limit(cfg.per_source_limit)
    )
    if since is not None:
        q = q.where(e.occurred_at > since)
    snap = PortfolioSnapshotRecord
    capital = session.scalar(
        select(snap.contributed_capital)
        .where(snap.portfolio_account_id == account_id)
        .order_by(snap.as_of.desc())
        .limit(1)
    )
    out: list[Notification] = []
    for event, position in session.execute(q).all():
        subject = {
            "position_id": str(position.id),
            "symbol": position.symbol,
            "direction": position.direction.value,
            "strategy_id": position.strategy_id,
        }
        if event.event_type is PositionEventType.OPENED:
            out.append(
                Notification(
                    id=f"POSITION_OPENED:{event.id}",
                    kind="POSITION_OPENED",
                    severity="INFO",
                    occurred_at=event.occurred_at,
                    title=f"Opened {position.direction.value} {position.symbol} (paper)",
                    message=event.reason,
                    subject=subject,
                )
            )
            continue
        net = Decimal(str(event.details.get("net_pnl", position.realized_pnl)))
        out.append(
            Notification(
                id=f"POSITION_CLOSED:{event.id}",
                kind="POSITION_CLOSED",
                severity="INFO",
                occurred_at=event.occurred_at,
                title=f"Closed {position.symbol} (paper): net {net:+.2f} {position.currency}",
                message=event.reason,
                subject=subject
                | {
                    "net_pnl": str(net),
                    "exit_reason": None
                    if position.exit_reason is None
                    else position.exit_reason.value,
                },
            )
        )
        base = capital if capital is not None and capital > 0 else None
        if base is not None and abs(net) >= base * cfg.large_pnl_fraction:
            out.append(
                Notification(
                    id=f"LARGE_PNL:{event.id}",
                    kind="LARGE_PNL",
                    severity="WARNING",
                    occurred_at=event.occurred_at,
                    title=f"Large {'gain' if net > 0 else 'loss'} on {position.symbol}: "
                    f"{net:+.2f} {position.currency}",
                    message=f"Net P/L of {net:+.2f} is at least "
                    f"{cfg.large_pnl_fraction:%} of the contributed capital (simulated).",
                    subject=subject | {"net_pnl": str(net)},
                )
            )
    return out


def _orders(
    session: Session, account_id: uuid.UUID, since: datetime | None, cfg: NotificationSettings
) -> list[Notification]:
    o = OrderRecord
    at = o.updated_at
    q = (
        select(o)
        .where(o.portfolio_account_id == account_id, o.status.in_(REJECTED_ORDERS))
        .order_by(at.desc())
        .limit(cfg.per_source_limit)
    )
    if since is not None:
        q = q.where(at > since)
    return [
        Notification(
            id=f"ORDER_REJECTED:{order.id}",
            kind="ORDER_REJECTED",
            severity="WARNING",
            occurred_at=order.updated_at,
            title=f"Paper order {order.status.value.lower()}: {order.purpose.value} "
            f"{order.side.value} {order.symbol}",
            message=order.status_reason or "no reason recorded",
            subject={"order_id": str(order.id), "symbol": order.symbol},
        )
        for order in session.scalars(q).all()
    ]


def _intents(
    session: Session, account_id: uuid.UUID, since: datetime | None, cfg: NotificationSettings
) -> list[Notification]:
    t = TradeIntentRecord
    q = (
        select(t)
        .where(
            t.portfolio_account_id == account_id,
            t.status.in_(REJECTED_INTENTS),
            t.decided_at.is_not(None),
        )
        .order_by(t.decided_at.desc())
        .limit(cfg.per_source_limit)
    )
    if since is not None:
        q = q.where(t.decided_at > since)
    out = []
    for intent in session.scalars(q).all():
        assert intent.decided_at is not None  # noqa: S101  # filtered above
        out.append(
            Notification(
                id=f"TRADE_REJECTED:{intent.id}",
                kind="TRADE_REJECTED",
                severity="HIGH" if intent.status is IntentStatus.DEAD else "INFO",
                occurred_at=intent.decided_at,
                title=f"{intent.status.value.replace('_', ' ').capitalize()}: "
                f"{intent.direction or ''} {intent.symbol or ''}".strip(),
                message=intent.error or _reasons(intent.reasons),
                subject={"intent_id": str(intent.id), "symbol": intent.symbol},
            )
        )
    return out


def _breakers(
    session: Session, account_id: uuid.UUID, since: datetime | None, cfg: NotificationSettings
) -> list[Notification]:
    b = RiskCircuitBreakerEventRecord
    q = (
        select(b)
        .where(b.portfolio_account_id == account_id)
        .order_by(b.occurred_at.desc())
        .limit(cfg.per_source_limit)
    )
    if since is not None:
        q = q.where(b.occurred_at > since)
    return [
        Notification(
            id=f"CIRCUIT_BREAKER:{row.id}",
            kind="CIRCUIT_BREAKER",
            severity="HIGH" if row.new_status.value != "CLEAR" else "INFO",
            occurred_at=row.occurred_at,
            title=f"Circuit breaker {row.breaker_type.value}: {row.event.value}",
            message=row.reason,
            subject={
                "breaker": row.breaker_type.value,
                "status": row.new_status.value,
                "actor": row.actor,
            },
        )
        for row in session.scalars(q).all()
    ]


def _alerts(
    session: Session, account_id: uuid.UUID, since: datetime | None, cfg: NotificationSettings
) -> list[Notification]:
    h = SystemHealthEventRecord
    q = (
        select(h)
        .where(
            h.severity.in_(ALERT_SEVERITIES),
            (h.portfolio_account_id == account_id) | h.portfolio_account_id.is_(None),
        )
        .order_by(h.occurred_at.desc())
        .limit(cfg.per_source_limit)
    )
    if since is not None:
        q = q.where(h.occurred_at > since)
    return [
        Notification(
            id=f"SYSTEM_ALERT:{row.id}",
            kind="SYSTEM_ALERT",
            severity=row.severity.value,
            occurred_at=row.occurred_at,
            title=f"{row.component}: {row.event_type}",
            message=row.summary,
            subject={
                "component": row.component,
                "event_type": row.event_type,
                "action": (row.details or {}).get("action"),
            },
        )
        for row in session.scalars(q).all()
    ]


SOURCES = (_positions, _orders, _intents, _breakers, _alerts)


def feed(
    session: Session,
    account_id: uuid.UUID,
    *,
    since: datetime | None = None,
    kinds: set[str] | None = None,
    limit: int = 100,
    settings: NotificationSettings | None = None,
) -> list[Notification]:
    """Notifications of the account, newest first (at most ``limit``)."""
    cfg = settings or NotificationSettings()
    found: list[Notification] = []
    for source in SOURCES:
        found.extend(source(session, account_id, since, cfg))
    if kinds:
        found = [n for n in found if n.kind in kinds]
    found.sort(key=lambda n: (n.occurred_at, n.id), reverse=True)
    return found[:limit]


__all__ = [
    "KINDS",
    "Notification",
    "NotificationChannel",
    "NotificationSettings",
    "feed",
]
