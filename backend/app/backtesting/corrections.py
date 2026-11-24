"""Corporate-action correction sets in the replay (D-243). Read side only.

A run that names a registered correction set (``BacktestRequest.corporate_actions``) applies
its VERIFIED records. The set's ``content_sha256`` is recomputed from the stored records on
every load and must equal both the registered hash and the one the run recorded, so a run
cannot silently apply different corrections.

Scope today: cash-dividend identity (policy ``ledger-dividend-identity-v1``).

* ``CASH_DIVIDEND`` credits a provider dividend filed under another ticker (a former ticker
  of the asset, verified on the session before the ex-date) to the asset.
* ``CASH_DIVIDEND_VOID`` withholds a provider dividend filed under the asset's own ticker that
  belongs to another security (a reused ticker).

Dividends stay what D-237 made them: cash at the ex-date for positions held over the previous
close, never a decision input. A correction is therefore usable only from its ex-date, the
same moment as a provider dividend, and adds no information to any earlier decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.database.models import CorporateActionEventRecord, CorporateActionSetRecord

POLICY = "ledger-dividend-identity-v1"
CREDIT, VOID, SUCCESSION = "CASH_DIVIDEND", "CASH_DIVIDEND_VOID", "TICKER_SUCCESSION"
VERIFIED = "VERIFIED"


class CorrectionSetError(ValueError):
    """A correction set is missing, not yet registered, or its content changed."""


def event_content(row: CorporateActionEventRecord) -> dict[str, Any]:
    """The fields a set's hash covers: what the record states, not when it was stored."""

    def day(d: date | None) -> str | None:
        return None if d is None else d.isoformat()

    return {
        "code": row.code,
        "revision": row.revision,
        "asset_id": str(row.asset_id),
        "asset_label": row.asset_label,
        "identity_sha256": row.identity_sha256,
        "event_type": row.event_type,
        "provider": row.provider,
        "provider_ticker": row.provider_ticker,
        "provider_action_id": row.provider_action_id,
        "valid_from": day(row.valid_from),
        "valid_to": day(row.valid_to),
        "ex_date": day(row.ex_date),
        "payable_date": day(row.payable_date),
        "cash_amount": None if row.cash_amount is None else str(Decimal(row.cash_amount)),
        "currency": row.currency,
        "special": row.special,
        "foreign": row.foreign,
        "link_code": row.link_code,
        "known_on": day(row.known_on),
        "verification_status": row.verification_status,
        "evidence_sha256": row.evidence_sha256,
    }


def content_sha256(contents: list[dict[str, Any]]) -> str:
    """Canonical hash of a set's member contents (order-independent)."""
    ordered = sorted(contents, key=lambda c: (c["code"], c["revision"]))
    text = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class DividendCorrection:
    action: str  # CREDIT or VOID
    code: str
    asset_id: uuid.UUID
    provider_ticker: str
    provider_action_id: str
    ex_date: date
    rate: Decimal
    special: bool
    foreign: bool


@dataclass(frozen=True)
class LoadedSet:
    code: str
    content_sha256: str
    preregistration_code: str
    events: tuple[CorporateActionEventRecord, ...]

    def dividends(self) -> list[DividendCorrection]:
        out = []
        for e in self.events:
            if e.event_type not in (CREDIT, VOID):
                continue
            assert e.ex_date is not None  # noqa: S101  # database check
            assert e.provider_action_id is not None  # noqa: S101
            assert e.cash_amount is not None  # noqa: S101
            out.append(
                DividendCorrection(
                    action=e.event_type,
                    code=e.code,
                    asset_id=e.asset_id,
                    provider_ticker=e.provider_ticker,
                    provider_action_id=e.provider_action_id,
                    ex_date=e.ex_date,
                    rate=Decimal(e.cash_amount),
                    special=bool(e.special),
                    foreign=bool(e.foreign),
                )
            )
        return sorted(out, key=lambda c: (c.ex_date, str(c.asset_id), c.code))


def load_set(session: Session, code: str, *, known_at: datetime) -> LoadedSet:
    """The set ``code`` as registered by ``known_at``, its members re-hashed."""
    s = session.scalar(
        select(CorporateActionSetRecord).where(
            CorporateActionSetRecord.code == code, CorporateActionSetRecord.created_at <= known_at
        )
    )
    if s is None:
        raise CorrectionSetError(f"correction set {code!r} is not registered by {known_at}")
    keys = [(m["code"], int(m["revision"])) for m in s.members]
    e = CorporateActionEventRecord
    rows = list(
        session.scalars(
            select(e)
            .where(tuple_(e.code, e.revision).in_(keys), e.created_at <= known_at)
            .order_by(e.code, e.revision)
        )
    )
    if len(rows) != len(keys):
        missing = len(keys) - len(rows)
        raise CorrectionSetError(f"correction set {code}: {missing} member(s) missing")
    if any(r.verification_status != VERIFIED for r in rows):
        raise CorrectionSetError(f"correction set {code}: a member is not VERIFIED")
    sha = content_sha256([event_content(r) for r in rows])
    if sha != s.content_sha256:
        raise CorrectionSetError(
            f"correction set {code}: content hash {sha} differs from the registered "
            f"{s.content_sha256}"
        )
    return LoadedSet(code, sha, s.preregistration_code, tuple(rows))


__all__ = [
    "CREDIT",
    "POLICY",
    "SUCCESSION",
    "VERIFIED",
    "VOID",
    "CorrectionSetError",
    "DividendCorrection",
    "LoadedSet",
    "content_sha256",
    "event_content",
    "load_set",
]
