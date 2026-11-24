"""Date-bounded research exclusions in the replay (D-242). Pure calendar arithmetic.

A WINDOW exclusion names an asset's contaminated sessions ``contaminated_from`` ..
``contaminated_to``: observations that misstate what a holder had (an unrecorded spin-off,
an uncredited special dividend, an unadjusted reverse split, an identity splice). The
replay never holds the asset over them and never decides on history that contains them:

* ``blocked_from``: the session before ``contaminated_from``. A position still open at its
  close is closed at that close (the last clean observation), after costs; no entry is
  decided from that session on (an entry decided then would fill on the contaminated
  session).
* ``blocked_through``: the last session whose decision reads ``contaminated_to``: a decision
  at the close of session D reads the ``read_sessions`` sessions ending with D, so that is
  ``contaminated_to`` advanced by ``read_sessions - 1`` sessions. For an identity splice
  inside a membership span the registered ``blocked_through`` (the span's end: the
  membership was formed on the predecessor's data) extends it.

Outside ``blocked_from``..``blocked_through`` the asset is untouched: earlier and later
history stays in use.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date

from app.asset_discovery.sessions import next_trading_day, previous_trading_day
from app.backtesting.config import ExclusionWindowSpec
from app.backtesting.data import CALENDAR
from app.research.store import ExclusionWindow


def blocked_range(window: ExclusionWindow, read_sessions: int) -> tuple[date, date]:
    """(first, last) session on which the asset is out of the decision universe."""
    if read_sessions < 1:
        raise ValueError("read_sessions must be at least 1")
    first = previous_trading_day(CALENDAR, window.contaminated_from).day
    last = window.contaminated_to
    for _ in range(read_sessions - 1):
        last = next_trading_day(CALENDAR, last).day
    if window.blocked_through is not None:
        last = max(last, window.blocked_through)
    return first, last


def specs(
    windows: dict[uuid.UUID, list[ExclusionWindow]],
    asset_ids: Iterable[uuid.UUID],
    read_sessions: int,
) -> tuple[ExclusionWindowSpec, ...]:
    """The windows of ``asset_ids``, resolved for a run reading ``read_sessions`` sessions
    (ordered by asset, then event: deterministic)."""
    out: list[ExclusionWindowSpec] = []
    for asset_id in sorted(set(asset_ids), key=str):
        for w in windows.get(asset_id, []):
            first, last = blocked_range(w, read_sessions)
            out.append(
                ExclusionWindowSpec(
                    code=w.code,
                    asset_id=str(asset_id),
                    symbol=w.symbol,
                    event_session=w.event_session,
                    contaminated_from=w.contaminated_from,
                    contaminated_to=w.contaminated_to,
                    blocked_from=first,
                    blocked_through=last,
                )
            )
    return tuple(sorted(out, key=lambda s: (s.asset_id, s.contaminated_from, s.code)))


def blocked_ranges(
    windows: dict[uuid.UUID, list[ExclusionWindow]], read_sessions: int
) -> dict[uuid.UUID, list[tuple[date, date]]]:
    return {a: [blocked_range(w, read_sessions) for w in ws] for a, ws in windows.items()}


def blocked_on(specs_: Iterable[ExclusionWindowSpec], day: date) -> ExclusionWindowSpec | None:
    """The first window blocking ``day`` (None: the asset is usable on ``day``)."""
    for s in specs_:
        if s.blocked_from <= day <= s.blocked_through:
            return s
    return None


__all__ = ["blocked_on", "blocked_range", "blocked_ranges", "specs"]
