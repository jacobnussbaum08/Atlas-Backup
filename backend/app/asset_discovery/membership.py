"""Point-in-time universe membership (Phase 3A).

Membership of a layer (discovery pool, qualified universe, active watchlist) is a chain of
immutable ADDED/REMOVED events per asset. Rules:

* Transitions are idempotent: asking for the state an asset is already in records nothing.
* Each event names its predecessor; the unique constraint on ``(asset, layer,
  previous_event_id)`` turns two concurrent transitions from the same state into one
  commit and one ``IntegrityError`` (the loser re-reads and retries, or finds it done).
* Events are never back-dated before their predecessor, and ``effective_at`` is never
  after ``recorded_at``.
* ``members_at(T, known_at)`` reconstructs a layer from events effective at or before T
  **and recorded by** ``known_at`` (default T). Today's membership therefore never leaks
  into a historical query, and an asset that later became inactive or delisted is still
  a member of every period it actually belonged to: there is no survivorship bias.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.asset_discovery.enums import MembershipAction, UniverseLayer
from app.core.clocks import ensure_utc
from app.database.models import UniverseMembershipEventRecord as Event


class MembershipError(ValueError):
    pass


@dataclass(frozen=True)
class MemberState:
    asset_id: uuid.UUID
    layer: UniverseLayer
    member: bool
    since: datetime
    event_id: int
    sequence: int
    reason_code: str
    reason: str


def _state(event: Event) -> MemberState:
    return MemberState(
        asset_id=event.asset_id,
        layer=event.layer,
        member=event.action is MembershipAction.ADDED,
        since=event.effective_at,
        event_id=event.id,
        sequence=event.sequence,
        reason_code=event.reason_code,
        reason=event.reason,
    )


def _latest(
    session: Session,
    layer: UniverseLayer,
    *,
    asset_ids: Iterable[uuid.UUID] | None = None,
    at: datetime | None = None,
    known_at: datetime | None = None,
) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.layer == layer)
        .ext(distinct_on(Event.asset_id))
        .order_by(Event.asset_id, Event.sequence.desc())
    )
    if asset_ids is not None:
        ids = list(set(asset_ids))
        if not ids:
            return []
        stmt = stmt.where(Event.asset_id.in_(ids))
    if at is not None:
        stmt = stmt.where(Event.effective_at <= ensure_utc(at))
    if known_at is not None:
        stmt = stmt.where(Event.recorded_at <= ensure_utc(known_at))
    return list(session.scalars(stmt))


def current_states(
    session: Session, layer: UniverseLayer, asset_ids: Iterable[uuid.UUID] | None = None
) -> dict[uuid.UUID, MemberState]:
    """Newest recorded state per asset (members and former members)."""
    return {e.asset_id: _state(e) for e in _latest(session, layer, asset_ids=asset_ids)}


def current_members(session: Session, layer: UniverseLayer) -> dict[uuid.UUID, MemberState]:
    return {k: v for k, v in current_states(session, layer).items() if v.member}


def members_at(
    session: Session,
    layer: UniverseLayer,
    at: datetime,
    *,
    known_at: datetime | None = None,
    asset_ids: Iterable[uuid.UUID] | None = None,
) -> dict[uuid.UUID, MemberState]:
    """The layer as Atlas knew it at ``known_at`` (default ``at``) for time ``at``."""
    at = ensure_utc(at)
    events = _latest(session, layer, asset_ids=asset_ids, at=at, known_at=known_at or at)
    return {e.asset_id: _state(e) for e in events if e.action is MembershipAction.ADDED}


def history(
    session: Session, asset_id: uuid.UUID, layer: UniverseLayer | None = None
) -> Sequence[Event]:
    stmt = select(Event).where(Event.asset_id == asset_id)
    if layer is not None:
        stmt = stmt.where(Event.layer == layer)
    return session.scalars(stmt.order_by(Event.layer, Event.sequence)).all()


def transition(
    session: Session,
    *,
    asset_id: uuid.UUID,
    layer: UniverseLayer,
    action: MembershipAction,
    effective_at: datetime,
    recorded_at: datetime,
    reason_code: str,
    reason: str,
    job_id: uuid.UUID | None = None,
    evaluation_id: uuid.UUID | None = None,
    score_id: int | None = None,
    details: dict[str, Any] | None = None,
    previous: MemberState | bool | None = True,
    flush: bool = True,
) -> Event | None:
    """Move ``asset_id`` into ``action``'s state; None if it is already there.

    ``previous`` may pass the state the caller read (``None`` = no events yet) to avoid a
    query; by default the newest event is read here. Either way the database rejects the
    insert if another transaction appended an event in between. ``flush=False`` leaves
    the insert to the caller's next flush (batched writes).
    """
    effective_at, recorded_at = ensure_utc(effective_at), ensure_utc(recorded_at)
    if effective_at > recorded_at:
        raise MembershipError("membership cannot become effective after it is recorded")
    if previous is True:
        found = current_states(session, layer, [asset_id])
        state = found.get(asset_id)
    else:
        state = previous if isinstance(previous, MemberState) else None
    if state is None:
        if action is MembershipAction.REMOVED:
            return None  # never a member: nothing to remove
    else:
        if state.member == (action is MembershipAction.ADDED):
            return None
        if effective_at < state.since:
            raise MembershipError(
                f"membership change at {effective_at.isoformat()} precedes the previous "
                f"change at {state.since.isoformat()} (no back-dating)"
            )
    event = Event(
        asset_id=asset_id,
        layer=layer,
        action=action,
        sequence=1 if state is None else state.sequence + 1,
        previous_event_id=None if state is None else state.event_id,
        effective_at=effective_at,
        recorded_at=recorded_at,
        reason_code=reason_code,
        reason=reason,
        job_id=job_id,
        evaluation_id=evaluation_id,
        score_id=score_id,
        details=details or {},
    )
    session.add(event)
    if flush:
        session.flush()
    return event


__all__ = [
    "MemberState",
    "MembershipError",
    "current_members",
    "current_states",
    "history",
    "members_at",
    "transition",
]
