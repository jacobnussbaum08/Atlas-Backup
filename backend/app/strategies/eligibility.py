"""Point-in-time asset eligibility for strategy evaluations (Phase 4B).

Eligibility comes from the newest Phase 3A provider reference version
(``asset_reference_versions``) that Atlas had observed by the evaluation's knowledge time,
so a historical evaluation sees the flags known then, not today's. Without such a version
the asset is not eligible: Atlas cannot say it was tradable.

* any direction: the provider listed the instrument ACTIVE and tradable;
* SHORT: additionally shortable and easy to borrow (unknown counts as no).

This gates analytical hypotheses only. It sizes nothing and approves nothing; the risk
engine of a later phase makes its own decisions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import AssetReferenceVersionRecord
from app.strategies.base import Eligibility


def load_eligibility(
    session: Session, asset_id: uuid.UUID, *, provider: str, known_at: datetime
) -> Eligibility:
    v = AssetReferenceVersionRecord
    row = session.scalar(
        select(v)
        .where(v.asset_id == asset_id, v.provider == provider, v.observed_at <= known_at)
        .order_by(v.observed_at.desc(), v.version.desc())
        .limit(1)
    )
    if row is None:
        return Eligibility(known=False, provider=provider)
    return Eligibility(
        known=True,
        provider=provider,
        provider_status=row.status.value,
        tradable=row.tradable,
        shortable=row.shortable,
        easy_to_borrow=row.easy_to_borrow,
        reference_version=row.version,
        observed_at=row.observed_at,
    )


__all__ = ["load_eligibility"]
