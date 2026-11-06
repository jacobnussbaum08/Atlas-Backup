"""Risk configuration governance (spec 3.6, 14.7; decision D-139).

* A configuration change is a new immutable version: previous configuration, new
  configuration, field-by-field changes, actor, timestamp, reason and version number are
  stored in ``risk_configuration_versions`` and the audit log, in one transaction under the
  portfolio account lock.
* Only an authorized operator can write one. The actor must be ``operator:<name>``; any
  other actor (a strategy, model, LLM, worker or the system) is refused here, and the
  database refuses it again (CHECK ``operator_actor``). The API has no endpoint that writes
  configuration: changes come only from the operator CLI.
* Hard limits and preferences are validated as a whole (ranges, coherence); a "change" that
  changes nothing is refused, so every version records a real change.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AtlasError
from app.core.logging import get_logger
from app.database.models import ActorType, AuditLog, PortfolioAccount
from app.database.models.risk import (
    RiskConfigurationRecord,
    RiskConfigurationVersionRecord,
    RiskEventRecord,
)
from app.risk.config import HardLimits, Preferences, RiskConfiguration, config_diff
from app.risk.enums import ConfigChangeKind, RiskEventType, Severity

logger = get_logger(__name__)

OPERATOR_PATTERN = re.compile(r"^operator:[A-Za-z0-9._@-]+$")
MIN_REASON = 10


class GovernanceError(AtlasError):
    """A risk configuration change was refused."""

    code = "risk_governance_refused"


def require_operator(actor: str) -> str:
    """The actor if it is an authorized operator identity; refuse everything else.

    Strategies, models, LLM tasks and workers identify as ``strategy:``, ``ml:``, ``ai:``,
    ``llm:``, ``worker:`` or ``system:`` and can never pass.
    """
    if not OPERATOR_PATTERN.match(actor or ""):
        raise GovernanceError(
            f"actor {actor!r} is not an authorized operator: risk limits and controls change only "
            "through an explicit operator action (operator:<name>)"
        )
    return actor


def _require_reason(reason: str) -> str:
    if len((reason or "").strip()) < MIN_REASON:
        raise GovernanceError(f"a reason of at least {MIN_REASON} characters is required")
    return reason.strip()


def _lock_account(session: Session, account_id: uuid.UUID) -> PortfolioAccount:
    account = session.scalar(
        select(PortfolioAccount).where(PortfolioAccount.id == account_id).with_for_update()
    )
    if account is None:
        raise GovernanceError(f"portfolio account {account_id} does not exist")
    return account


def latest_version(
    session: Session, account_id: uuid.UUID
) -> RiskConfigurationVersionRecord | None:
    return session.scalar(
        select(RiskConfigurationVersionRecord)
        .where(RiskConfigurationVersionRecord.portfolio_account_id == account_id)
        .order_by(RiskConfigurationVersionRecord.version.desc())
        .limit(1)
    )


def to_configuration(record: RiskConfigurationVersionRecord) -> RiskConfiguration:
    return RiskConfiguration(
        version_id=str(record.id),
        version=record.version,
        hard_limits=HardLimits.model_validate(record.hard_limits),
        preferences=Preferences.model_validate(record.preferences),
    )


def active_configuration(session: Session, account_id: uuid.UUID) -> RiskConfiguration | None:
    found = latest_version(session, account_id)
    return None if found is None else to_configuration(found)


@dataclass(frozen=True)
class VersionResult:
    version_id: uuid.UUID
    version: int
    created: bool
    changes: list[dict[str, Any]]


def _audit(
    session: Session,
    *,
    at: datetime,
    actor: str,
    action: str,
    record: RiskConfigurationVersionRecord,
    previous: RiskConfigurationVersionRecord | None,
    reason: str,
) -> None:
    session.add(
        AuditLog(
            occurred_at=at,
            actor_type=ActorType.USER,
            actor_id=actor,
            action=action,
            entity_type="risk_configuration_version",
            entity_id=str(record.id),
            reason=reason,
            previous_state=None
            if previous is None
            else {
                "version": previous.version,
                "hard_limits": previous.hard_limits,
                "preferences": previous.preferences,
            },
            new_state={
                "version": record.version,
                "hard_limits": record.hard_limits,
                "preferences": record.preferences,
            },
            details={"portfolio_account_id": str(record.portfolio_account_id)},
        )
    )


def initialize(
    session: Session,
    account_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    at: datetime,
    hard_limits: HardLimits | None = None,
    preferences: Preferences | None = None,
) -> VersionResult:
    """Create version 1 (the documented defaults unless given). Idempotent: an account that
    already has a configuration keeps it and nothing is written."""
    require_operator(actor)
    reason = _require_reason(reason)
    _lock_account(session, account_id)
    existing = latest_version(session, account_id)
    if existing is not None:
        return VersionResult(existing.id, existing.version, False, [])
    limits = hard_limits or HardLimits()
    prefs = preferences or Preferences()
    configuration = RiskConfigurationRecord(portfolio_account_id=account_id, created_by=actor)
    session.add(configuration)
    session.flush()
    changes = config_diff(None, (limits, prefs))
    record = RiskConfigurationVersionRecord(
        configuration_id=configuration.id,
        portfolio_account_id=account_id,
        version=1,
        previous_version_id=None,
        change_kind=ConfigChangeKind.INITIAL,
        hard_limits=limits.canonical(),
        preferences=prefs.canonical(),
        hard_limits_sha256=limits.sha256(),
        preferences_sha256=prefs.sha256(),
        changes=changes,
        actor=actor,
        reason=reason,
        created_at=at,
    )
    session.add(record)
    session.flush()
    _audit(
        session,
        at=at,
        actor=actor,
        action="risk.configuration_created",
        record=record,
        previous=None,
        reason=reason,
    )
    session.add(
        RiskEventRecord(
            portfolio_account_id=account_id,
            event_type=RiskEventType.CONFIGURATION_CREATED,
            severity=Severity.INFO,
            occurred_at=at,
            actor=actor,
            subject_kind="risk_configuration_version",
            subject_id=str(record.id),
            summary="risk configuration version 1 created",
            details={"changes": len(changes)},
        )
    )
    session.flush()
    logger.info("risk.configuration_created", account_id=str(account_id), actor=actor)
    return VersionResult(record.id, 1, True, changes)


def change(
    session: Session,
    account_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    at: datetime,
    hard_limit_updates: dict[str, Any] | None = None,
    preference_updates: dict[str, Any] | None = None,
) -> VersionResult:
    """Write the next version with the given field updates (validated as a whole)."""
    require_operator(actor)
    reason = _require_reason(reason)
    hard_limit_updates = hard_limit_updates or {}
    preference_updates = preference_updates or {}
    if not hard_limit_updates and not preference_updates:
        raise GovernanceError("no change requested")
    _lock_account(session, account_id)
    previous = latest_version(session, account_id)
    if previous is None:
        raise GovernanceError("no risk configuration exists: run `init` first")
    old_limits = HardLimits.model_validate(previous.hard_limits)
    old_prefs = Preferences.model_validate(previous.preferences)
    unknown = (set(hard_limit_updates) - set(HardLimits.model_fields)) | (
        set(preference_updates) - set(Preferences.model_fields)
    )
    if unknown:
        raise GovernanceError(f"unknown risk settings: {sorted(unknown)}")
    try:
        limits = HardLimits.model_validate({**old_limits.canonical(), **hard_limit_updates})
        prefs = Preferences.model_validate({**old_prefs.canonical(), **preference_updates})
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise GovernanceError(f"invalid risk configuration: {details}") from None
    changes = config_diff((old_limits, old_prefs), (limits, prefs))
    if not changes:
        raise GovernanceError("the requested values equal the current configuration")
    sections = {c["section"] for c in changes}
    kind = (
        ConfigChangeKind.HARD_LIMITS_AND_PREFERENCES
        if len(sections) == 2
        else ConfigChangeKind.HARD_LIMITS
        if "hard_limits" in sections
        else ConfigChangeKind.PREFERENCES
    )
    record = RiskConfigurationVersionRecord(
        configuration_id=previous.configuration_id,
        portfolio_account_id=account_id,
        version=previous.version + 1,
        previous_version_id=previous.id,
        change_kind=kind,
        hard_limits=limits.canonical(),
        preferences=prefs.canonical(),
        hard_limits_sha256=limits.sha256(),
        preferences_sha256=prefs.sha256(),
        changes=changes,
        actor=actor,
        reason=reason,
        created_at=at,
    )
    session.add(record)
    session.flush()
    _audit(
        session,
        at=at,
        actor=actor,
        action="risk.configuration_changed",
        record=record,
        previous=previous,
        reason=reason,
    )
    session.add(
        RiskEventRecord(
            portfolio_account_id=account_id,
            event_type=RiskEventType.CONFIGURATION_CHANGED,
            severity=Severity.WARNING if "hard_limits" in sections else Severity.INFO,
            occurred_at=at,
            actor=actor,
            subject_kind="risk_configuration_version",
            subject_id=str(record.id),
            summary=f"risk configuration version {record.version} ({kind.value})",
            details={"changes": changes},
        )
    )
    session.flush()
    logger.info(
        "risk.configuration_changed",
        account_id=str(account_id),
        version=record.version,
        kind=kind.value,
        actor=actor,
    )
    return VersionResult(record.id, record.version, True, changes)


__all__ = [
    "OPERATOR_PATTERN",
    "GovernanceError",
    "VersionResult",
    "active_configuration",
    "change",
    "initialize",
    "latest_version",
    "require_operator",
    "to_configuration",
]
