"""The accounting posting service (spec 16; decisions D-129, D-130).

One posting = one database transaction:

1. lock the portfolio account row (``SELECT ... FOR UPDATE``): postings to an account are
   serialized, so balances, positions and the gapless sequence are read consistently;
2. idempotency: an ``event_id`` already posted with the same request returns the original
   entry; with a different request it is refused (never silently re-posted);
3. load the ledger state, build the draft with the template, check R1/R2 and the line rules,
   and check that the resulting state introduces no invariant violation;
4. write the entry, its lines and quantity movements, update the balance cache, append an
   audit record; the deferred database trigger re-checks the balance at COMMIT.

Source rules: opening funding and valuations come from SYSTEM; trades, costs and
conversions are CONTROLLED_TEST postings, only on CONTROLLED_TEST accounts; reversals come
from an OPERATOR (or CONTROLLED_TEST on a test account). Phase 7B adds SIMULATED_EXECUTION:
the paper execution engine posts simulated fills and their costs (the trade, fee, financing
and accrual-settlement templates only) on any paper account. Nothing here submits, sizes
or approves an order.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session, sessionmaker

from app.accounting.config import AccountingSettings
from app.accounting.enums import EntrySource, EntryType, FxRateSource, PriceStatus, Side
from app.accounting.ledger import (
    EntryDraft,
    EntryValidationError,
    QuantityMovement,
    validate,
)
from app.accounting.money import RATE_SCALE, ZERO, div, round_to
from app.accounting.reference import (
    TEST_SOURCES,
    VALUATION_SOURCES,
    functional_rate,
    instrument_info,
    record_fx_rate,
)
from app.accounting.state import (
    Balance,
    InsufficientBalanceError,
    LedgerState,
    Position,
    invariant_violations,
)
from app.accounting.templates import (
    TEMPLATES,
    InstrumentInfo,
    RateQuote,
    ReversalParams,
    StoredEntry,
    StoredLine,
    TemplateDef,
    TemplateError,
    reversal,
)
from app.core.clocks import Clock, ensure_utc
from app.core.exceptions import AtlasError
from app.core.logging import get_logger
from app.database.models import ActorType, PortfolioAccount
from app.database.models.accounting import (
    AssetQuantityRecord,
    CurrencyBalanceRecord,
    JournalEntryRecord,
    JournalLineRecord,
    PostingTemplateRecord,
)
from app.database.models.portfolio_account import PortfolioAccountPurpose, PortfolioAccountStatus
from app.database.repositories.audit_log import AuditLogRepository
from app.database.sessions import session_scope

logger = get_logger(__name__)
# Templates the paper execution engine (Phase 7B) posts as SIMULATED_EXECUTION.
SIMULATED_EXECUTION_TEMPLATES = frozenset(
    {
        "long_purchase",
        "long_sale",
        "short_open",
        "short_cover",
        "fee",
        "financing_cost",
        "accrual_settlement",
    }
)


class PostingError(AtlasError):
    """A posting was refused. Nothing was written."""

    code = "posting_refused"
    http_status = 422


class IdempotencyConflictError(PostingError):
    """The event ID was already posted with a different request."""

    code = "idempotency_conflict"
    http_status = 409


@dataclass(frozen=True)
class PostingRequest:
    account_id: uuid.UUID
    template: str
    params: BaseModel | dict[str, Any]
    event_id: str
    effective_at: datetime
    source: EntrySource
    actor: str
    source_ref: str | None = None


@dataclass(frozen=True)
class PostingResult:
    entry_id: uuid.UUID
    sequence: int
    entry_type: EntryType
    created: bool


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ------------------------------------------------------------------ state loading
def load_state(
    session: Session, account_id: uuid.UUID, functional_currency: str, *, up_to: int | None = None
) -> LedgerState:
    """Balances (from journal lines) and positions (from the quantity ledger) of an account."""
    debit = JournalLineRecord.side == Side.DEBIT
    amount_expr = case((debit, JournalLineRecord.amount), else_=-JournalLineRecord.amount)
    functional_expr = case(
        (debit, JournalLineRecord.functional_amount), else_=-JournalLineRecord.functional_amount
    )
    stmt = (
        select(
            JournalLineRecord.account_code,
            JournalLineRecord.currency,
            JournalLineRecord.asset_id,
            func.sum(amount_expr),
            func.sum(functional_expr),
        )
        .where(JournalLineRecord.portfolio_account_id == account_id)
        .group_by(
            JournalLineRecord.account_code, JournalLineRecord.currency, JournalLineRecord.asset_id
        )
    )
    if up_to is not None:
        stmt = stmt.where(JournalLineRecord.sequence <= up_to)
    balances = {
        (code, cur, asset): Balance(Decimal(amount), Decimal(functional))
        for code, cur, asset, amount, functional in session.execute(stmt)
        if amount != 0 or functional != 0
    }
    latest = (
        select(AssetQuantityRecord)
        .where(AssetQuantityRecord.portfolio_account_id == account_id)
        .ext(distinct_on(AssetQuantityRecord.asset_id))
        .order_by(AssetQuantityRecord.asset_id, AssetQuantityRecord.sequence.desc())
    )
    if up_to is not None:
        latest = latest.where(AssetQuantityRecord.sequence <= up_to)
    positions = {
        row.asset_id: Position(
            asset_id=row.asset_id,
            currency=row.currency,
            quantity=row.quantity_after,
            cost=row.cost_after,
            functional_cost=row.functional_cost_after,
        )
        for row in session.scalars(latest)
    }
    return LedgerState(functional_currency, balances, positions)


def stored_entry(session: Session, entry: JournalEntryRecord) -> StoredEntry:
    lines = session.scalars(
        select(JournalLineRecord)
        .where(JournalLineRecord.entry_id == entry.id)
        .order_by(JournalLineRecord.line_number)
    ).all()
    movements = session.scalars(
        select(AssetQuantityRecord).where(AssetQuantityRecord.entry_id == entry.id)
    ).all()
    reversed_already = (
        session.scalar(
            select(func.count())
            .select_from(JournalEntryRecord)
            .where(JournalEntryRecord.reverses_entry_id == entry.id)
        )
        or 0
    ) > 0
    later = session.scalars(
        select(JournalLineRecord.asset_id)
        .where(
            JournalLineRecord.portfolio_account_id == entry.portfolio_account_id,
            JournalLineRecord.sequence > entry.sequence,
            JournalLineRecord.asset_id.is_not(None),
        )
        .distinct()
    ).all()
    return StoredEntry(
        entry_id=entry.id,
        entry_type=entry.entry_type,
        sequence=entry.sequence,
        lines=tuple(
            StoredLine(
                account_code=line.account_code,
                side=line.side,
                currency=line.currency,
                amount=line.amount,
                functional_amount=line.functional_amount,
                rate_basis=line.rate_basis,
                fx_rate=line.fx_rate,
                fx_rate_id=line.fx_rate_id,
                asset_id=line.asset_id,
                memo=line.memo,
            )
            for line in lines
        ),
        movements=tuple(
            QuantityMovement(
                asset_id=m.asset_id,
                movement_type=m.movement_type,
                currency=m.currency,
                quantity_delta=m.quantity_delta,
                quantity_after=m.quantity_after,
                cost_delta=m.cost_delta,
                cost_after=m.cost_after,
                functional_cost_delta=m.functional_cost_delta,
                functional_cost_after=m.functional_cost_after,
                price=m.price,
                realized_pnl=m.realized_pnl,
                realized_pnl_functional=m.realized_pnl_functional,
            )
            for m in movements
        ),
        reversed=reversed_already,
        assets_touched_later=frozenset(a for a in later if a is not None),
    )


# ------------------------------------------------------------------ context
class _Context:
    """What a template may read: fresh rates and instrument specifications at one time."""

    def __init__(
        self,
        session: Session,
        functional_currency: str,
        at: datetime,
        *,
        max_rate_age: timedelta,
        rate_sources: tuple[FxRateSource, ...],
    ) -> None:
        self._session = session
        self._functional = functional_currency
        self._at = at
        self._max_age = max_rate_age
        self._sources = rate_sources
        self._rates: dict[str, RateQuote] = {}
        self._instruments: dict[uuid.UUID, InstrumentInfo] = {}

    @property
    def functional_currency(self) -> str:
        return self._functional

    def spot_rate(self, currency: str) -> RateQuote:
        if currency not in self._rates:
            found = functional_rate(
                self._session,
                currency,
                self._functional,
                self._at,
                max_age=self._max_age,
                sources=self._sources,
            )
            if found.status is not PriceStatus.FRESH or found.quote is None:
                raise TemplateError(
                    f"no fresh {currency}/{self._functional} rate at {self._at.isoformat()}"
                    + (f" ({found.reason})" if found.reason else "")
                )
            self._rates[currency] = found.quote
        return self._rates[currency]

    def instrument(self, asset_id: uuid.UUID) -> InstrumentInfo:
        if asset_id not in self._instruments:
            self._instruments[asset_id] = instrument_info(self._session, asset_id, self._at)
        return self._instruments[asset_id]


# ------------------------------------------------------------------ service
class PostingService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        settings: AccountingSettings,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._settings = settings

    def post(self, request: PostingRequest) -> PostingResult:
        with session_scope(self._session_factory) as session:
            return self.post_in(session, request)

    def post_in(self, session: Session, request: PostingRequest) -> PostingResult:
        """Post within the caller's transaction (the caller commits)."""
        result = self.post_or_skip_in(session, request, skip_if_empty=False)
        if result is None:  # pragma: no cover - skip_if_empty=False never skips
            raise PostingError("nothing to post")
        return result

    def post_or_skip_in(
        self, session: Session, request: PostingRequest, *, skip_if_empty: bool
    ) -> PostingResult | None:
        """Like :meth:`post_in`; with ``skip_if_empty`` a draft without lines posts nothing.

        Used by valuation checkpoints: when no adjustment changed there is no entry.
        """
        template = TEMPLATES.get(request.template)
        if template is None:
            raise PostingError(f"unknown posting template {request.template!r}")
        try:
            params = template.params.model_validate(
                request.params.model_dump()
                if isinstance(request.params, BaseModel)
                else request.params
            )
        except ValidationError as exc:
            raise PostingError(
                f"invalid {template.code} inputs: {exc.errors()[0]['msg']}"
            ) from None
        if not request.event_id or len(request.event_id) > 160:
            raise PostingError("event_id must be 1 to 160 characters")
        effective_at = ensure_utc(request.effective_at)
        payload = params.model_dump(mode="json")
        request_sha256 = hashlib.sha256(
            canonical_json(
                {
                    "template": template.code,
                    "version": template.version,
                    "params": payload,
                    "effective_at": effective_at.isoformat(),
                    "source": request.source.value,
                    "source_ref": request.source_ref,
                }
            ).encode()
        ).hexdigest()

        account = session.scalar(
            select(PortfolioAccount)
            .where(PortfolioAccount.id == request.account_id)
            .with_for_update()
        )
        if account is None:
            raise PostingError(f"portfolio account {request.account_id} does not exist")
        existing = session.scalar(
            select(JournalEntryRecord).where(
                JournalEntryRecord.portfolio_account_id == account.id,
                JournalEntryRecord.event_id == request.event_id,
            )
        )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise IdempotencyConflictError(
                    f"event {request.event_id!r} was already posted with different inputs "
                    f"(entry #{existing.sequence})"
                )
            return PostingResult(existing.id, existing.sequence, existing.entry_type, created=False)
        self._check_source(account, template, request.source)
        if account.status is not PortfolioAccountStatus.ACTIVE:
            raise PostingError(f"the account is {account.status.value}")

        functional = account.base_currency
        state = load_state(session, account.id, functional)
        ctx = _Context(
            session,
            functional,
            effective_at,
            max_rate_age=timedelta(hours=self._settings.fx_rate_max_age_hours),
            rate_sources=(
                TEST_SOURCES
                if account.purpose is PortfolioAccountPurpose.CONTROLLED_TEST
                else VALUATION_SOURCES
            ),
        )
        reverses: JournalEntryRecord | None = None
        try:
            if isinstance(params, ReversalParams):
                reverses = session.scalar(
                    select(JournalEntryRecord).where(
                        JournalEntryRecord.id == params.entry_id,
                        JournalEntryRecord.portfolio_account_id == account.id,
                    )
                )
                if reverses is None:
                    raise PostingError(f"entry {params.entry_id} is not in this account")
                draft = reversal(state, ctx, stored_entry(session, reverses), params.reason)
            else:
                draft = template.build(state, ctx, params)
            if skip_if_empty and not draft.lines and not draft.movements:
                return None
            validate(draft, functional)
        except (TemplateError, EntryValidationError, InsufficientBalanceError) as exc:
            raise PostingError(str(exc)) from None
        self._check_invariants(state, draft)

        template_row = ensure_template(session, template)
        sequence = last_sequence(session, account.id) + 1
        entry = JournalEntryRecord(
            portfolio_account_id=account.id,
            sequence=sequence,
            event_id=request.event_id,
            request_sha256=request_sha256,
            entry_type=draft.entry_type,
            template_id=template_row.id,
            source=request.source,
            source_ref=request.source_ref,
            functional_currency=functional,
            effective_at=effective_at,
            posted_at=self._clock.now(),
            reverses_entry_id=reverses.id if reverses is not None else None,
            description=draft.description,
            payload=payload,
            details=json.loads(canonical_json(draft.details)),
            created_by=request.actor,
        )
        session.add(entry)
        session.flush()
        self._write(session, account.id, entry, draft)
        if draft.entry_type is EntryType.FX_CONVERSION:
            self._record_conversion_rate(session, entry, draft, request.actor)
        AuditLogRepository(session).append(
            actor_type=ActorType.SUBSYSTEM,
            actor_id=request.actor,
            action="accounting.entry_posted",
            entity_type="journal_entry",
            entity_id=str(entry.id),
            details={
                "account_id": str(account.id),
                "sequence": sequence,
                "entry_type": draft.entry_type.value,
                "event_id": request.event_id,
                "source": request.source.value,
            },
        )
        logger.info(
            "accounting.entry_posted",
            account_id=str(account.id),
            sequence=sequence,
            entry_type=draft.entry_type.value,
            lines=len(draft.lines),
        )
        return PostingResult(entry.id, sequence, draft.entry_type, created=True)

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _check_source(
        account: PortfolioAccount, template: TemplateDef, source: EntrySource
    ) -> None:
        test_account = account.purpose is PortfolioAccountPurpose.CONTROLLED_TEST
        if source is EntrySource.CONTROLLED_TEST and not test_account:
            raise PostingError(
                "controlled test transactions are never posted to the primary account"
            )
        if template.entry_type in (EntryType.OPENING_FUNDING, EntryType.VALUATION):
            if source is not EntrySource.SYSTEM:
                raise PostingError(f"{template.code} entries are posted by the system")
        elif template.entry_type is EntryType.REVERSAL:
            if source not in (EntrySource.OPERATOR, EntrySource.CONTROLLED_TEST):
                raise PostingError("reversals are operator corrections")
        elif source is EntrySource.SIMULATED_EXECUTION:
            if template.code not in SIMULATED_EXECUTION_TEMPLATES:
                raise PostingError(f"{template.code} is not posted by the paper execution engine")
        elif template.controlled_test_only and source is not EntrySource.CONTROLLED_TEST:
            raise PostingError(
                f"{template.code} has no production source yet (Phase 6A posts it only as a "
                "controlled test transaction)"
            )

    @staticmethod
    def _check_invariants(state: LedgerState, draft: EntryDraft) -> None:
        after = state.copy()
        for line in draft.lines:
            after.apply_line(line)
        for movement in draft.movements:
            after.apply_movement(movement)
        before = set(invariant_violations(state))
        new = [p for p in invariant_violations(after) if p not in before]
        if new:
            raise PostingError("the posting would leave the books inconsistent: " + "; ".join(new))
        if before:
            logger.error("accounting.preexisting_inconsistency", problems=sorted(before))

    def _write(
        self,
        session: Session,
        account_id: uuid.UUID,
        entry: JournalEntryRecord,
        draft: EntryDraft,
    ) -> None:
        cache: dict[tuple[str, str], list[Decimal | int]] = defaultdict(lambda: [ZERO, ZERO, 0])
        for number, line in enumerate(draft.lines, start=1):
            session.add(
                JournalLineRecord(
                    entry_id=entry.id,
                    portfolio_account_id=account_id,
                    sequence=entry.sequence,
                    line_number=number,
                    account_code=line.account_code,
                    currency=line.currency,
                    side=line.side,
                    amount=line.amount,
                    functional_amount=line.functional_amount,
                    rate_basis=line.rate_basis,
                    fx_rate=line.fx_rate,
                    fx_rate_id=line.fx_rate_id,
                    asset_id=line.asset_id,
                    memo=line.memo,
                )
            )
            totals = cache[(line.account_code, line.currency)]
            totals[0] = Decimal(totals[0]) + line.signed_amount
            totals[1] = Decimal(totals[1]) + line.signed_functional
            totals[2] = int(totals[2]) + 1
        for movement in draft.movements:
            session.add(
                AssetQuantityRecord(
                    portfolio_account_id=account_id,
                    asset_id=movement.asset_id,
                    entry_id=entry.id,
                    sequence=entry.sequence,
                    movement_type=movement.movement_type,
                    currency=movement.currency,
                    quantity_delta=movement.quantity_delta,
                    quantity_after=movement.quantity_after,
                    cost_delta=movement.cost_delta,
                    cost_after=movement.cost_after,
                    functional_cost_delta=movement.functional_cost_delta,
                    functional_cost_after=movement.functional_cost_after,
                    price=movement.price,
                    realized_pnl=movement.realized_pnl,
                    realized_pnl_functional=movement.realized_pnl_functional,
                )
            )
        session.flush()
        now = self._clock.now()
        for (code, cur), (amount, functional, count) in cache.items():
            stmt = insert(CurrencyBalanceRecord).values(
                portfolio_account_id=account_id,
                account_code=code,
                currency=cur,
                balance=amount,
                functional_balance=functional,
                line_count=count,
                last_sequence=entry.sequence,
                updated_at=now,
            )
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["portfolio_account_id", "account_code", "currency"],
                    set_={
                        "balance": CurrencyBalanceRecord.balance + stmt.excluded.balance,
                        "functional_balance": CurrencyBalanceRecord.functional_balance
                        + stmt.excluded.functional_balance,
                        "line_count": CurrencyBalanceRecord.line_count + stmt.excluded.line_count,
                        "last_sequence": stmt.excluded.last_sequence,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )

    def _record_conversion_rate(
        self, session: Session, entry: JournalEntryRecord, draft: EntryDraft, actor: str
    ) -> None:
        """Document the executed rate of a conversion (never used for valuation)."""
        details = draft.details
        sell = Decimal(str(details["sell_amount"]))
        buy = Decimal(str(details["buy_amount"]))
        record_fx_rate(
            session,
            base=str(details["sell_currency"]),
            quote=str(details["buy_currency"]),
            rate=round_to(div(buy, sell), RATE_SCALE),
            observed_at=entry.effective_at,
            source=FxRateSource.CONVERSION,
            provenance=f"executed conversion, journal entry {entry.id} (#{entry.sequence})",
            recorded_at=max(entry.effective_at, self._clock.now()),
            created_by=actor,
        )


def last_sequence(session: Session, account_id: uuid.UUID) -> int:
    """The account's latest entry sequence number (0 before the first entry)."""
    found = session.scalar(
        select(func.max(JournalEntryRecord.sequence)).where(
            JournalEntryRecord.portfolio_account_id == account_id
        )
    )
    return int(found or 0)


def ensure_template(session: Session, template: TemplateDef) -> PostingTemplateRecord:
    """The stored template row for this code/version, written on first use.

    A stored row whose definition differs from the code's is refused: a template's rules
    change only with a new version.
    """
    digest = template.definition_sha256()
    row = session.scalar(
        select(PostingTemplateRecord).where(
            PostingTemplateRecord.code == template.code,
            PostingTemplateRecord.version == template.version,
        )
    )
    if row is None:
        session.execute(
            insert(PostingTemplateRecord)
            .values(
                code=template.code,
                version=template.version,
                entry_type=template.entry_type,
                description=template.description,
                definition=template.definition(),
                definition_sha256=digest,
            )
            .on_conflict_do_nothing(index_elements=["code", "version"])
        )
        row = session.scalar(
            select(PostingTemplateRecord).where(
                PostingTemplateRecord.code == template.code,
                PostingTemplateRecord.version == template.version,
            )
        )
    if row is None or row.definition_sha256 != digest:
        raise PostingError(
            f"stored posting template {template.code} v{template.version} differs from the "
            "code; template rules change only with a new version"
        )
    return row


__all__ = [
    "IdempotencyConflictError",
    "PostingError",
    "PostingRequest",
    "PostingResult",
    "PostingService",
    "canonical_json",
    "ensure_template",
    "last_sequence",
    "load_state",
    "stored_entry",
]
