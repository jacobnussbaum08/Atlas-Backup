"""Read side of the research data foundation, used by the backtest (D-239).

* :func:`universe_members`: the member spans of a research universe known by a time, less
  the assets excluded by then (:func:`exclusions`, D-240).
* :func:`check_exclusions`: refuses a run on reserved data that names an excluded asset or
  whose decisions would read an asset's contaminated observations (D-242).
* :func:`exclusion_windows`: date-bounded exclusions (scope ``WINDOW``, D-242): the
  contaminated sessions of one asset, from which the replay derives what it must not read.
* :func:`exclusion_sets`: each registered exclusion set's code and content hash.
* :func:`delistings`: the delisting dates the security master knew by a time.
* :func:`check_reservations`: refuses a backtest that touches reserved data unless it names
  a registered pre-registration covering every reservation it touches.

Everything filters on ``created_at <= known_at``: rows written after a run's knowledge
time never change it, so stored runs reproduce exactly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    ResearchDataReservationRecord,
    ResearchExclusionRecord,
    ResearchPreregistrationRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
)

PERIOD, UNIVERSE, EXPLORATION_ONLY = "PERIOD", "UNIVERSE", "EXPLORATION_ONLY"
ASSET_SCOPE, WINDOW_SCOPE = "ASSET", "WINDOW"


class ReservationError(ValueError):
    """A run touches reserved data without a pre-registration covering it."""


@dataclass(frozen=True)
class Span:
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def exclusions(session: Session, *, known_at: datetime) -> dict[uuid.UUID, list[str]]:
    """Asset id -> the exclusion codes (D-240) registered by ``known_at`` that remove it.

    An exclusion removes the asset from every research universe and from every run on
    reserved data. Exclusions are append-only and never lifted: a later registration can
    only add assets."""
    x = ResearchExclusionRecord
    out: dict[uuid.UUID, list[str]] = defaultdict(list)
    for asset_id, code in session.execute(
        select(x.asset_id, x.code)
        .where(x.created_at <= known_at, x.scope == ASSET_SCOPE)
        .order_by(x.asset_id, x.code)
    ):
        out[asset_id].append(code)
    return dict(out)


@dataclass(frozen=True)
class ExclusionWindow:
    """A date-bounded exclusion (D-242): ``asset_id``'s observations on the sessions
    ``contaminated_from``..``contaminated_to`` misstate what a holder had. The replay must
    not hold the asset over them nor decide on any history that contains them; for an
    identity splice it also stays out through ``blocked_through``."""

    code: str
    asset_id: uuid.UUID
    symbol: str
    event_session: date
    contaminated_from: date
    contaminated_to: date
    blocked_through: date | None


def exclusion_windows(
    session: Session, *, known_at: datetime
) -> dict[uuid.UUID, list[ExclusionWindow]]:
    """Asset id -> its WINDOW exclusions registered by ``known_at`` (oldest event first)."""
    x = ResearchExclusionRecord
    out: dict[uuid.UUID, list[ExclusionWindow]] = defaultdict(list)
    for row in session.scalars(
        select(x)
        .where(x.created_at <= known_at, x.scope == WINDOW_SCOPE)
        .order_by(x.asset_id, x.contaminated_from, x.code, x.id)
    ):
        assert row.contaminated_from is not None  # noqa: S101  # database check
        assert row.contaminated_to is not None  # noqa: S101
        out[row.asset_id].append(
            ExclusionWindow(
                code=row.code,
                asset_id=row.asset_id,
                symbol=row.symbol,
                event_session=row.event_session,
                contaminated_from=row.contaminated_from,
                contaminated_to=row.contaminated_to,
                blocked_through=row.blocked_through,
            )
        )
    return dict(out)


def exclusion_entry(row: ResearchExclusionRecord) -> dict[str, str | None]:
    """The content of one exclusion row that its set's hash covers."""

    def day(d: date | None) -> str | None:
        return None if d is None else d.isoformat()

    return {
        "code": row.code,
        "asset_id": str(row.asset_id),
        "symbol": row.symbol,
        "universe_code": row.universe_code,
        "scope": row.scope,
        "event_session": row.event_session.isoformat(),
        "contaminated_from": day(row.contaminated_from),
        "contaminated_to": day(row.contaminated_to),
        "blocked_through": day(row.blocked_through),
        "preregistration_code": row.preregistration_code,
        "document_sha256": row.document_sha256,
    }


def set_sha256(entries: Iterable[dict[str, str | None]]) -> str:
    """Content hash of one exclusion set: its entries in a fixed order, canonical JSON
    (independent of insertion order and of ``created_at``)."""
    ordered = sorted(entries, key=lambda e: (e["asset_id"] or "", e["event_session"] or ""))
    text = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def exclusion_sets(session: Session, *, known_at: datetime) -> dict[str, str]:
    """Code -> content hash of every exclusion set registered by ``known_at`` (D-242). A
    run records them, so its exclusions are pinned by content as well as by time."""
    x = ResearchExclusionRecord
    by_code: dict[str, list[dict[str, str | None]]] = defaultdict(list)
    for row in session.scalars(select(x).where(x.created_at <= known_at).order_by(x.id)):
        by_code[row.code].append(exclusion_entry(row))
    return {code: set_sha256(entries) for code, entries in sorted(by_code.items())}


def universe_members(
    session: Session, code: str, *, known_at: datetime, apply_exclusions: bool = True
) -> dict[uuid.UUID, list[Span]]:
    """Asset id -> the spans it was a member of ``code`` (as known by ``known_at``).

    Assets excluded by ``known_at`` (:func:`exclusions`) are left out, so the replay never
    loads their bars, decides on them or holds them. ``apply_exclusions=False`` returns the
    universe as formed (evidence collection only, never a replay)."""
    c = ResearchUniverseCandidateRecord
    excluded = exclusions(session, known_at=known_at) if apply_exclusions else {}
    out: dict[uuid.UUID, list[Span]] = defaultdict(list)
    for row in session.scalars(
        select(c)
        .where(
            c.universe_code == code,
            c.member.is_(True),
            c.asset_id.is_not(None),
            c.created_at <= known_at,
        )
        .order_by(c.asset_id, c.effective_from)
    ):
        assert row.asset_id is not None  # noqa: S101  # filtered
        assert row.effective_from is not None  # noqa: S101
        assert row.effective_to is not None  # noqa: S101
        if row.asset_id in excluded:
            continue
        out[row.asset_id].append(Span(row.effective_from, row.effective_to))
    return dict(out)


def check_exclusions(
    session: Session,
    asset_ids: Iterable[uuid.UUID],
    symbols: dict[uuid.UUID, str],
    *,
    at: datetime,
    blocked: dict[uuid.UUID, list[tuple[date, date]]] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> None:
    """Refuse a run on reserved data that would trade an excluded asset (D-240), or one
    whose sessions ``start``..``end`` overlap an asset's blocked range (``blocked``: the
    ranges the replay derives from WINDOW exclusions, D-242).

    A research-universe run never reaches this with one (:func:`universe_members` leaves
    ASSET exclusions out; the replay applies WINDOW ones); an explicit or layer run naming
    one is refused, not silently narrowed."""
    excluded = exclusions(session, known_at=at)
    ids = set(asset_ids)
    hit = sorted(
        f"{symbols.get(a, str(a))} ({', '.join(excluded[a])})" for a in ids if a in excluded
    )
    if hit:
        raise ReservationError(
            "the run would trade assets excluded from research use: " + ", ".join(hit)
        )
    if blocked and start is not None and end is not None:
        overlap = sorted(
            f"{symbols.get(a, str(a))} ({first.isoformat()}..{last.isoformat()})"
            for a in ids
            for first, last in blocked.get(a, [])
            if first <= end and last >= start
        )
        if overlap:
            raise ReservationError(
                "the run would hold or decide on contaminated corporate-action observations "
                "(date-bounded exclusions): " + ", ".join(overlap)
            )


def universe_exists(session: Session, code: str, *, known_at: datetime) -> bool:
    r = ResearchUniverseRecord
    return (
        session.scalar(select(r.id).where(r.code == code, r.created_at <= known_at).limit(1))
        is not None
    )


def delistings(
    session: Session, asset_ids: Iterable[uuid.UUID], *, known_at: datetime
) -> dict[uuid.UUID, date]:
    """Asset id -> last trading date of a delisted security (the newest record known).

    Records are ordered by creation, then revision: an asset can carry revisions of the
    provider-ticker master and, later, its point-in-time identity record (revision 0 of
    its own chain), which is the newer knowledge."""
    s = ResearchSecurityRecord
    newest: dict[uuid.UUID, ResearchSecurityRecord] = {}
    for row in session.scalars(
        select(s)
        .where(s.asset_id.in_(list(asset_ids)), s.created_at <= known_at)
        .order_by(s.asset_id, s.created_at, s.revision, s.id)
    ):
        assert row.asset_id is not None  # noqa: S101  # filtered by in_
        newest[row.asset_id] = row
    return {a: r.delisted_on for a, r in newest.items() if r.delisted_on is not None}


def check_reservations(
    session: Session,
    *,
    start: date,
    end: date,
    symbols: Iterable[str],
    universe_code: str | None,
    preregistration: str | None,
    at: datetime,
) -> dict[str, str] | None:
    """Refuse a run on reserved data without a covering pre-registration.

    Returns the pre-registration it runs under (``{code, document_sha256}``), or None when
    it touches no reservation. ``EXPLORATION_ONLY`` reservations document data that may be
    explored but not used to confirm; they never block a run.
    """
    names = {s.upper() for s in symbols}
    touched: list[str] = []
    r = ResearchDataReservationRecord
    for res in session.scalars(select(r).where(r.created_at <= at).order_by(r.code)):
        if res.scope == PERIOD:
            assert res.start_date is not None  # noqa: S101
            assert res.end_date is not None  # noqa: S101
            if start <= res.end_date and end >= res.start_date:
                touched.append(res.code)
        elif res.scope == UNIVERSE:
            excluded = {s.upper() for s in res.symbols}
            if universe_code is not None and universe_code == res.universe_code:
                touched.append(res.code)
                continue
            c = ResearchUniverseCandidateRecord
            members = set(
                session.scalars(
                    select(c.symbol).where(c.universe_code == res.universe_code, c.member.is_(True))
                )
            )
            if (names & members) - excluded:
                touched.append(res.code)
    if not touched:
        return None
    if preregistration is None:
        raise ReservationError(
            "the run touches reserved research data ("
            + ", ".join(touched)
            + "): name a registered pre-registration covering it"
        )
    p = ResearchPreregistrationRecord
    prereg = session.scalar(select(p).where(p.code == preregistration, p.created_at <= at))
    if prereg is None:
        raise ReservationError(f"pre-registration {preregistration!r} is not registered")
    missing = sorted(set(touched) - set(prereg.reservation_codes))
    if missing:
        raise ReservationError(
            f"pre-registration {preregistration!r} does not cover the reservations "
            + ", ".join(missing)
        )
    return {"code": prereg.code, "document_sha256": prereg.document_sha256}


__all__ = [
    "ASSET_SCOPE",
    "EXPLORATION_ONLY",
    "PERIOD",
    "UNIVERSE",
    "WINDOW_SCOPE",
    "ExclusionWindow",
    "ReservationError",
    "Span",
    "check_exclusions",
    "check_reservations",
    "delistings",
    "exclusion_entry",
    "exclusion_sets",
    "exclusion_windows",
    "exclusions",
    "set_sha256",
    "universe_exists",
    "universe_members",
]
