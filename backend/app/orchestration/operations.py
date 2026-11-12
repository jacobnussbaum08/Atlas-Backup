"""Operational checks scheduled by the orchestrator (Phase 10B). Read-mostly.

* **DATA_FRESHNESS** (every ``data_freshness_seconds``, regular session only): every open
  position of the account must have a fresh stored price (6A ``valuation_price``: trade,
  quote mid or completed bar within the accounting freshness limits). Stale or missing
  prices are reported (``FEED_STALE``, HIGH, once per hour). Nothing is invented: a stale
  position keeps its last mark, the risk monitor's DATA_QUALITY breaker halts new entries,
  and its protective levels are evaluated against the observations once they arrive (the
  recovery worker backfills gaps), so a stop crossed during an outage still triggers then.
* **SESSION_OPEN_CHECK** (each trading day, around the regular open): a readiness checklist
  (trading mode, protection gate, tripped breakers, latest reconciliations, dead letters).
  Blockers while trading is ENABLED or PAUSED raise ``SESSION_NOT_READY`` (HIGH).
* **SESSION_CLOSE_REVIEW** (each trading day after the close; missed days are caught up):
  the day's durable operational summary (intents, orders, fills, positions, alerts, jobs).

Each returns a JSON-able summary that the job row stores.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.accounting.enums import PriceStatus
from app.core.clocks import Clock
from app.data_engine.calendar import MarketSession, local_date, session_at
from app.database.models import Asset
from app.database.models.execution import (
    OrderFillRecord,
    OrderRecord,
    OutboxEventRecord,
    PaperPositionRecord,
)
from app.database.models.market_data import StreamStatusRecord
from app.database.models.orchestration import OrchestrationJobRecord, TradeIntentRecord
from app.database.models.reservations import SystemHealthEventRecord
from app.database.sessions import session_scope
from app.execution.enums import OutboxStatus, PositionStatus
from app.orchestration import alerts, controls
from app.orchestration.enums import JobKind, JobStatus, TradingMode
from app.orchestration.jobs import slot_day
from app.orchestration.protection import assess
from app.portfolio.pricing import valuation_price
from app.risk.enums import BreakerStatus, Severity
from app.risk.state import breaker_statuses

NEW_YORK = ZoneInfo("America/New_York")
RECONCILIATIONS = (
    JobKind.ACCOUNTING_RECONCILE,
    JobKind.RESERVATION_RECONCILE,
    JobKind.EXECUTION_RECONCILE,
)


def _day_window(day: Any) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=NEW_YORK).astimezone(UTC)
    return start, start + timedelta(days=1)


def _counts(session: Session, column: Any, *where: Any) -> dict[str, int]:
    rows = session.execute(select(column, func.count()).where(*where).group_by(column)).all()
    return {str(getattr(k, "value", k)): int(n) for k, n in rows}


class OperationsWork:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        account_id: uuid.UUID,
        accounting: AccountingSettings,
        *,
        require_workers: bool = True,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._account = account_id
        self._accounting = accounting
        self._require = require_workers

    # ------------------------------------------------------------------ freshness
    def data_freshness(self) -> dict[str, Any]:
        now = self._clock.now()
        if session_at(now) is not MarketSession.REGULAR:
            return {"skipped": "outside the regular session (no fresh data expected)"}
        with session_scope(self._factory) as session:
            positions = session.scalars(
                select(PaperPositionRecord).where(
                    PaperPositionRecord.portfolio_account_id == self._account,
                    PaperPositionRecord.status == PositionStatus.OPEN,
                )
            ).all()
            stale: list[dict[str, Any]] = []
            for p in positions:
                asset = session.get(Asset, p.asset_id)
                if asset is None:
                    continue
                price = valuation_price(session, asset, now, self._accounting)
                if price.status is not PriceStatus.FRESH:
                    stale.append(
                        {
                            "symbol": p.symbol,
                            "status": price.status.value,
                            "last_mark_at": p.last_mark_at.isoformat() if p.last_mark_at else None,
                        }
                    )
            stream = session.scalars(
                select(StreamStatusRecord).order_by(StreamStatusRecord.heartbeat_at.desc()).limit(1)
            ).first()
            stream_view = (
                None
                if stream is None
                else {
                    "state": stream.connection_state,
                    "heartbeat_age_seconds": round((now - stream.heartbeat_at).total_seconds(), 1),
                    "last_persisted_at": stream.last_persisted_at.isoformat()
                    if stream.last_persisted_at
                    else None,
                }
            )
            if stale:
                alerts.raise_once(
                    session,
                    event_type="FEED_STALE",
                    severity=Severity.HIGH,
                    summary=f"{len(stale)} of {len(positions)} open position(s) have no fresh "
                    "price: " + ", ".join(f"{s['symbol']} ({s['status']})" for s in stale[:10]),
                    now=now,
                    account_id=self._account,
                    subject_kind="portfolio_account",
                    subject_id=str(self._account),
                    details={"stale": stale[:50], "stream": stream_view},
                )
        return {
            "positions": len(positions),
            "stale": stale[:50],
            "fresh": len(positions) - len(stale),
            "stream": stream_view,
        }

    # ------------------------------------------------------------------ session open
    def session_open_check(self) -> dict[str, Any]:
        now = self._clock.now()
        with session_scope(self._factory) as session:
            state = controls.current(session, self._account)
            gate = assess(session, now, require_workers=self._require)
            tripped = sorted(
                b.value
                for b, s in breaker_statuses(session, self._account).items()
                if s is BreakerStatus.TRIPPED
            )
            reconciliations: dict[str, Any] = {}
            for kind in RECONCILIATIONS:
                row = session.scalars(
                    select(OrchestrationJobRecord)
                    .where(
                        OrchestrationJobRecord.kind == kind,
                        OrchestrationJobRecord.status == JobStatus.SUCCEEDED,
                    )
                    .order_by(OrchestrationJobRecord.finished_at.desc())
                    .limit(1)
                ).first()
                reconciliations[kind.value] = (
                    None
                    if row is None
                    else {
                        "passed": (row.result or {}).get("passed"),
                        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    }
                )
            dead_jobs = int(
                session.scalar(
                    select(func.count())
                    .select_from(OrchestrationJobRecord)
                    .where(OrchestrationJobRecord.status == JobStatus.DEAD)
                )
                or 0
            )
            dead_events = int(
                session.scalar(
                    select(func.count())
                    .select_from(OutboxEventRecord)
                    .where(OutboxEventRecord.status == OutboxStatus.DEAD)
                )
                or 0
            )
            blockers: list[dict[str, Any]] = list(gate.reasons)
            blockers += [
                {"code": "BREAKER_TRIPPED", "message": f"{b} is tripped (entries halted)"}
                for b in tripped
            ]
            for name, view in reconciliations.items():
                if view is not None and view.get("passed") is False:
                    blockers.append(
                        {"code": "RECONCILIATION_FAILED", "message": f"latest {name} failed"}
                    )
            warnings: list[dict[str, Any]] = []
            if dead_jobs:
                warnings.append(
                    {"code": "DEAD_JOBS", "message": f"{dead_jobs} dead scheduled job(s)"}
                )
            if dead_events:
                warnings.append(
                    {"code": "DEAD_EVENTS", "message": f"{dead_events} dead outbox event(s)"}
                )
            missing = [k for k, v in reconciliations.items() if v is None]
            if missing:
                warnings.append(
                    {
                        "code": "NO_RECONCILIATION",
                        "message": "no successful run yet: " + ", ".join(missing),
                    }
                )
            day = local_date(now).isoformat()
            if blockers and state.mode in (TradingMode.ENABLED, TradingMode.PAUSED):
                alerts.raise_once(
                    session,
                    event_type="SESSION_NOT_READY",
                    severity=Severity.HIGH,
                    summary=f"Not ready for the {day} open: "
                    + "; ".join(b["message"] for b in blockers[:6]),
                    now=now,
                    account_id=self._account,
                    subject_kind="trading_day",
                    subject_id=day,
                    details={"blockers": blockers, "warnings": warnings},
                    repeat=timedelta(hours=12),
                )
        return {
            "day": day,
            "ready": not blockers,
            "mode": state.mode.value,
            "blockers": blockers,
            "warnings": warnings,
            "breakers_tripped": tripped,
            "reconciliations": reconciliations,
            "protection": gate.as_dict(),
        }

    # ------------------------------------------------------------------ end of day
    def session_close_review(self, slot: str) -> dict[str, Any]:
        now = self._clock.now()
        day = slot_day(slot) or local_date(now)
        start, end = _day_window(day)
        a = self._account
        with session_scope(self._factory) as session:
            intents = _counts(
                session,
                TradeIntentRecord.status,
                TradeIntentRecord.portfolio_account_id == a,
                TradeIntentRecord.received_at >= start,
                TradeIntentRecord.received_at < end,
            )
            orders = _counts(
                session,
                OrderRecord.purpose,
                OrderRecord.portfolio_account_id == a,
                OrderRecord.submitted_at >= start,
                OrderRecord.submitted_at < end,
            )
            fills = int(
                session.scalar(
                    select(func.count())
                    .select_from(OrderFillRecord)
                    .where(
                        OrderFillRecord.portfolio_account_id == a,
                        OrderFillRecord.filled_at >= start,
                        OrderFillRecord.filled_at < end,
                    )
                )
                or 0
            )
            opened = int(
                session.scalar(
                    select(func.count())
                    .select_from(PaperPositionRecord)
                    .where(
                        PaperPositionRecord.portfolio_account_id == a,
                        PaperPositionRecord.opened_at >= start,
                        PaperPositionRecord.opened_at < end,
                    )
                )
                or 0
            )
            closed = session.execute(
                select(func.count(), func.coalesce(func.sum(PaperPositionRecord.realized_pnl), 0))
                .select_from(PaperPositionRecord)
                .where(
                    PaperPositionRecord.portfolio_account_id == a,
                    PaperPositionRecord.closed_at >= start,
                    PaperPositionRecord.closed_at < end,
                )
            ).one()
            open_now = int(
                session.scalar(
                    select(func.count())
                    .select_from(PaperPositionRecord)
                    .where(
                        PaperPositionRecord.portfolio_account_id == a,
                        PaperPositionRecord.status == PositionStatus.OPEN,
                    )
                )
                or 0
            )
            alert_counts = _counts(
                session,
                SystemHealthEventRecord.severity,
                SystemHealthEventRecord.occurred_at >= start,
                SystemHealthEventRecord.occurred_at < end,
                (SystemHealthEventRecord.portfolio_account_id == a)
                | SystemHealthEventRecord.portfolio_account_id.is_(None),
            )
            jobs = _counts(
                session,
                OrchestrationJobRecord.status,
                OrchestrationJobRecord.started_at >= start,
                OrchestrationJobRecord.started_at < end,
            )
        return {
            "day": day.isoformat(),
            "simulated": True,
            "intents": intents,
            "orders_by_purpose": orders,
            "fills": fills,
            "positions_opened": opened,
            "positions_closed": int(closed[0]),
            # Realized P/L of the positions closed that day, as each position recorded it
            # (position currency; simulated paper trading, not a performance claim).
            "realized_pnl_closed": str(closed[1]),
            "positions_open_at_review": open_now,
            "alerts": alert_counts,
            "jobs": jobs,
        }


__all__ = ["OperationsWork"]
