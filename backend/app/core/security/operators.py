"""Named operator credentials (Phase 10B, D-218).

Before 10B every API action was attributed to one shared token (``operator:api``). Now each
operator has a credential of their own:

* **Issue** (CLI only): a random 256-bit token is generated, shown **once**, and only its
  SHA-256 digest is stored (``operator_credentials``). The token cannot be recovered; a
  lost token is revoked and a new one issued.
* **Authenticate** (API): the ``X-Atlas-Operator-Token`` header (or ``Authorization:
  Bearer``) is hashed and looked up; a match with an unrevoked credential identifies
  ``operator:<name>``, which every audited action then records. Tokens are high-entropy
  random strings, so a fast hash is appropriate (no password is involved).
* **Least privilege**: only credentials issued with ``can_control_trading`` may use the
  trading controls (pause, disable, emergency stop, kill switch). No credential can enable
  trading, resume it, clear an emergency or release the kill switch through the API.
* **Revoke** (CLI): immediate and permanent. Credentials are never deleted.

Tokens and digests are never logged or returned by the API.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.audit_log import ActorType, AuditLog
from app.database.models.reliability import OPERATOR_NAME, OperatorCredentialRecord

TOKEN_PREFIX = "atlas_op_"  # noqa: S105  # a public prefix, not a secret
_NAME = re.compile(OPERATOR_NAME)

Method = Literal["NAMED", "SHARED", "DEVELOPMENT"]


class OperatorCredentialError(ValueError):
    """An operator credential action was refused (the message says why, never a token)."""


@dataclass(frozen=True)
class OperatorIdentity:
    actor: str  # "operator:<name>" (or "operator:api" for the shared/dev identity)
    method: Method
    can_control_trading: bool

    @property
    def named(self) -> bool:
        return self.method == "NAMED"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _actor(value: str) -> str:
    if not value.startswith("operator:") or not _NAME.match(value.removeprefix("operator:")):
        raise OperatorCredentialError("the actor must be operator:<name>")
    return value


def active(session: Session, name: str) -> OperatorCredentialRecord | None:
    return session.scalar(
        select(OperatorCredentialRecord).where(
            OperatorCredentialRecord.name == name, OperatorCredentialRecord.revoked_at.is_(None)
        )
    )


def any_active(session: Session) -> bool:
    return (
        session.scalar(
            select(OperatorCredentialRecord.id)
            .where(OperatorCredentialRecord.revoked_at.is_(None))
            .limit(1)
        )
        is not None
    )


def issue(
    session: Session,
    name: str,
    *,
    can_control_trading: bool,
    actor: str,
    reason: str,
    now: datetime,
) -> tuple[OperatorCredentialRecord, str]:
    """Create a credential; returns it and the token (shown once, never stored)."""
    if not _NAME.match(name):
        raise OperatorCredentialError("operator names are 2-64 of A-Z a-z 0-9 _ . @ -")
    actor = _actor(actor)
    if len(reason.strip()) < 3:
        raise OperatorCredentialError("a reason is required")
    existing = session.scalar(
        select(OperatorCredentialRecord).where(OperatorCredentialRecord.name == name)
    )
    if existing is not None:
        raise OperatorCredentialError(
            f"operator {name!r} already exists"
            + (" (revoked: choose a new name)" if existing.revoked_at else "")
        )
    token = new_token()
    record = OperatorCredentialRecord(
        name=name,
        token_sha256=digest(token),
        can_control_trading=can_control_trading,
        reason=reason.strip(),
        created_by=actor,
        created_at=now,
    )
    session.add(record)
    session.add(
        AuditLog(
            actor_type=ActorType.USER,
            actor_id=actor,
            action="operator.credential.issue",
            entity_type="operator_credential",
            entity_id=name,
            reason=reason.strip(),
            details={"can_control_trading": can_control_trading},
        )
    )
    session.flush()
    return record, token


def revoke(
    session: Session, name: str, *, actor: str, reason: str, now: datetime
) -> OperatorCredentialRecord:
    actor = _actor(actor)
    if len(reason.strip()) < 3:
        raise OperatorCredentialError("a reason is required")
    record = active(session, name)
    if record is None:
        raise OperatorCredentialError(f"no active operator named {name!r}")
    record.revoked_at = now
    record.revoked_by = actor
    record.revoke_reason = reason.strip()
    session.add(
        AuditLog(
            actor_type=ActorType.USER,
            actor_id=actor,
            action="operator.credential.revoke",
            entity_type="operator_credential",
            entity_id=name,
            reason=reason.strip(),
        )
    )
    session.flush()
    return record


def authenticate(session: Session, token: str, now: datetime) -> OperatorIdentity | None:
    """The identity a presented token proves, or None. Records the use."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    record = session.scalar(
        select(OperatorCredentialRecord).where(
            OperatorCredentialRecord.token_sha256 == digest(token),
            OperatorCredentialRecord.revoked_at.is_(None),
        )
    )
    if record is None:
        return None
    record.last_used_at = now
    return OperatorIdentity(f"operator:{record.name}", "NAMED", record.can_control_trading)


def listing(session: Session) -> list[dict[str, object]]:
    """Credentials without their digests."""
    rows = session.scalars(
        select(OperatorCredentialRecord).order_by(OperatorCredentialRecord.created_at)
    ).all()
    return [
        {
            "name": r.name,
            "actor": f"operator:{r.name}",
            "can_control_trading": r.can_control_trading,
            "created_at": r.created_at.isoformat(),
            "created_by": r.created_by,
            "reason": r.reason,
            "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
            "revoked_by": r.revoked_by,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        }
        for r in rows
    ]


__all__ = [
    "TOKEN_PREFIX",
    "OperatorCredentialError",
    "OperatorIdentity",
    "active",
    "any_active",
    "authenticate",
    "digest",
    "issue",
    "listing",
    "new_token",
    "revoke",
]
