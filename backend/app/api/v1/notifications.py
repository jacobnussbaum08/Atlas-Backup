"""Dashboard notifications (spec §23): a read-only feed derived from stored records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.v1.portfolio import Context
from app.core.exceptions import InvalidRequestError
from app.database.sessions import session_scope
from app.notifications import KINDS, feed
from app.portfolio import queries

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def notifications(
    ctx: Context,
    account_id: uuid.UUID | None = None,
    since: datetime | None = None,
    kind: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Newest first. ``since`` (exclusive) pages forward; ``kind`` filters (repeatable)."""
    unknown = sorted(set(kind or ()) - set(KINDS))
    if unknown:
        raise InvalidRequestError(f"unknown notification kind(s): {', '.join(unknown)}")
    if since is not None and since.tzinfo is None:
        raise InvalidRequestError("since must include a time zone")
    with session_scope(ctx.session_factory) as session:
        account = queries.resolve_account(session, account_id)
        items = feed(session, account.id, since=since, kinds=set(kind or ()), limit=limit)
        return {
            "account_id": str(account.id),
            "kinds": list(KINDS),
            "items": [n.as_dict() for n in items],
            "note": "Simulated paper trading. Notifications are views of stored records; "
            "nothing here is delivered outside the dashboard yet.",
        }
