"""Token estimates, costs and spending limits for LLM calls (Phase 5B).

* **Estimate** a request's input tokens conservatively from its characters (3 per token,
  above typical English ratios) before sending; a request above
  ``AI_LLM_MAX_INPUT_TOKENS`` is never sent.
* **Reserve** the worst case (estimated input + the full output allowance at the
  configured prices) against the daily and monthly budgets while the request runs; the
  reservation is replaced by the reported usage's cost when it finishes.
* **Check and reserve atomically**: the check and the RUNNING request row are written in
  one transaction under an advisory lock, so concurrent workers cannot overspend.
* **No prices, no calls**: limits in dollars cannot be enforced without prices, so an
  unpriced model is refused (UNPRICED) rather than called blind.

Costs are computed from the configured prices and the provider's reported tokens: an
estimate labelled with its pricing source, not an invoice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_engine.config import AISettings
from app.ai_engine.enums import LLMRequestStatus
from app.database.models import LLMCostRecord, LLMRequestRecord

CHARS_PER_TOKEN = 3
SCHEMA_OVERHEAD_TOKENS = 400
MILLION = Decimal(1_000_000)
USD_QUANTUM = Decimal("0.00000001")
BUDGET_LOCK = "llm-budget"
# Statuses that never reached a provider and so count toward no request limit.
NOT_SENT = (LLMRequestStatus.BUDGET_REFUSED, LLMRequestStatus.UNPRICED)


def estimate_tokens(*texts: str) -> int:
    return SCHEMA_OVERHEAD_TOKENS + sum(math.ceil(len(t) / CHARS_PER_TOKEN) for t in texts)


def cost_usd(
    input_tokens: int, output_tokens: int, input_price: Decimal, output_price: Decimal
) -> Decimal:
    raw = (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / MILLION
    return raw.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def month_start(now: datetime) -> datetime:
    return day_start(now).replace(day=1)


@dataclass(frozen=True)
class BudgetState:
    spent_today_usd: Decimal
    spent_month_usd: Decimal
    reserved_usd: Decimal
    requests_today: int
    daily_budget_usd: Decimal
    monthly_budget_usd: Decimal
    max_requests_per_day: int

    def to_json(self) -> dict[str, object]:
        return {
            "spent_today_usd": str(self.spent_today_usd),
            "spent_month_usd": str(self.spent_month_usd),
            "reserved_usd": str(self.reserved_usd),
            "requests_today": self.requests_today,
            "daily_budget_usd": str(self.daily_budget_usd),
            "monthly_budget_usd": str(self.monthly_budget_usd),
            "max_requests_per_day": self.max_requests_per_day,
        }


def state(session: Session, settings: AISettings, now: datetime) -> BudgetState:
    today, month = day_start(now), month_start(now)
    c, r = LLMCostRecord, LLMRequestRecord
    spent_today = session.scalar(
        select(func.coalesce(func.sum(c.cost_usd), 0)).where(c.recorded_at >= today)
    )
    spent_month = session.scalar(
        select(func.coalesce(func.sum(c.cost_usd), 0)).where(c.recorded_at >= month)
    )
    reserved = session.scalar(
        select(func.coalesce(func.sum(r.reserved_cost_usd), 0)).where(
            r.status == LLMRequestStatus.RUNNING
        )
    )
    requests = session.scalar(
        select(func.count()).where(r.requested_at >= today, r.status.not_in(NOT_SENT))
    )
    return BudgetState(
        Decimal(spent_today or 0),
        Decimal(spent_month or 0),
        Decimal(reserved or 0),
        int(requests or 0),
        settings.llm_daily_budget_usd,
        settings.llm_monthly_budget_usd,
        settings.llm_max_requests_per_day,
    )


@dataclass(frozen=True)
class Admission:
    status: LLMRequestStatus  # RUNNING when admitted
    reserved_usd: Decimal | None
    reason: str | None


def admit(
    session: Session, settings: AISettings, *, estimated_input: int, now: datetime
) -> Admission:
    """Decide under the budget lock whether a request may be sent (caller inserts it in
    the same transaction)."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(BUDGET_LOCK, 0))))
    if not settings.priced:
        return Admission(
            LLMRequestStatus.UNPRICED,
            None,
            "no prices are configured for the model (AI_LLM_INPUT_USD_PER_MTOK / "
            "AI_LLM_OUTPUT_USD_PER_MTOK): spending limits cannot be enforced",
        )
    if estimated_input > settings.llm_max_input_tokens:
        return Admission(
            LLMRequestStatus.BUDGET_REFUSED,
            None,
            f"estimated {estimated_input} input tokens exceed AI_LLM_MAX_INPUT_TOKENS "
            f"({settings.llm_max_input_tokens})",
        )
    assert settings.llm_input_usd_per_mtok is not None  # noqa: S101  # priced
    assert settings.llm_output_usd_per_mtok is not None  # noqa: S101
    reserve = cost_usd(
        estimated_input,
        settings.llm_max_output_tokens,
        settings.llm_input_usd_per_mtok,
        settings.llm_output_usd_per_mtok,
    )
    current = state(session, settings, now)
    if current.requests_today + 1 > settings.llm_max_requests_per_day:
        return Admission(
            LLMRequestStatus.BUDGET_REFUSED,
            None,
            f"daily request limit {settings.llm_max_requests_per_day} reached",
        )
    committed_today = current.spent_today_usd + current.reserved_usd + reserve
    if committed_today > settings.llm_daily_budget_usd:
        return Admission(
            LLMRequestStatus.BUDGET_REFUSED,
            None,
            f"daily budget ${settings.llm_daily_budget_usd} would be exceeded "
            f"(spent ${current.spent_today_usd}, reserved ${current.reserved_usd}, this "
            f"request up to ${reserve})",
        )
    if current.spent_month_usd + current.reserved_usd + reserve > settings.llm_monthly_budget_usd:
        return Admission(
            LLMRequestStatus.BUDGET_REFUSED,
            None,
            f"monthly budget ${settings.llm_monthly_budget_usd} would be exceeded",
        )
    return Admission(LLMRequestStatus.RUNNING, reserve, None)


def interrupted_before(now: datetime, timeout_seconds: float, retries: int) -> datetime:
    """RUNNING requests older than every attempt's timeout plus backoff died with their
    process; their reservation is released by marking them INTERRUPTED."""
    return now - timedelta(seconds=timeout_seconds * (retries + 1) + 120)


__all__ = [
    "Admission",
    "BudgetState",
    "admit",
    "cost_usd",
    "estimate_tokens",
    "interrupted_before",
    "state",
]
