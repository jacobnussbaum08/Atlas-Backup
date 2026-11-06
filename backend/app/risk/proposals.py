"""Proposals for the risk engine from stored strategy proposals and AI decisions (D-140).

Only levels and provenance cross this boundary: asset, direction, entry, stop, target,
holding horizon, expiry and the source identifiers. Signal strength, heuristic scores,
calibrated probabilities, expected values, rankings, LLM analyses and explanations are
**never read**: a strategy or model cannot make a trade larger, smaller or more acceptable
to the risk engine. This module reads database rows only; it imports no strategy, ML or AI
code.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import InvalidRequestError, NotFoundError
from app.database.models import AIDecisionRecord, StrategyProposalRecord
from app.risk.enums import ProposalSource, TradeDirection
from app.risk.models import Proposal

DAILY = "1Day"
PRIORITIZED = "PRIORITIZE_FOR_REVIEW"


def from_strategy_proposal(
    session: Session, proposal_id: uuid.UUID, *, requested_quantity: Decimal | None = None
) -> Proposal:
    row = session.get(StrategyProposalRecord, proposal_id)
    if row is None:
        raise NotFoundError(f"strategy proposal {proposal_id} does not exist")
    return Proposal(
        source=ProposalSource.STRATEGY_PROPOSAL,
        asset_id=row.asset_id,
        direction=TradeDirection(row.direction.value),
        entry=row.reference_entry,
        stop=row.invalidation_level,
        target=row.target_reference,
        requested_quantity=requested_quantity,
        expected_holding_days=min(row.holding_horizon_periods, 365)
        if row.timeframe == DAILY
        else None,
        expires_at=row.expires_at,
        strategy_proposal_id=row.id,
    )


def from_ai_decision(
    session: Session, decision_id: uuid.UUID, *, requested_quantity: Decimal | None = None
) -> Proposal:
    """The levels of the decision's lead strategy proposal. Only decisions the coordinator
    prioritized for review are accepted; its scores are not read."""
    decision = session.get(AIDecisionRecord, decision_id)
    if decision is None:
        raise NotFoundError(f"AI decision {decision_id} does not exist")
    if decision.outcome.value != PRIORITIZED:
        raise InvalidRequestError(
            f"AI decision {decision_id} is {decision.outcome.value}: only decisions "
            f"{PRIORITIZED} are offered to the risk engine"
        )
    lead_ref = (decision.expected or {}).get("lead_proposal_id")
    lead_id = uuid.UUID(str(lead_ref)) if lead_ref else None
    if lead_id is None or lead_id not in decision.proposal_ids:
        raise InvalidRequestError(f"AI decision {decision_id} names no lead strategy proposal")
    levels = from_strategy_proposal(session, lead_id, requested_quantity=requested_quantity)
    if decision.direction is not None and decision.direction.value != levels.direction.value:
        raise InvalidRequestError("the decision's direction differs from its lead proposal")
    expiries = [t for t in (decision.expires_at, levels.expires_at) if t is not None]
    return levels.model_copy(
        update={
            "source": ProposalSource.AI_DECISION,
            "ai_decision_id": decision.id,
            "expires_at": min(expiries) if expiries else None,
        }
    )


__all__ = ["from_ai_decision", "from_strategy_proposal"]
