"""Paper execution, order and position management (spec 15, 21 "Execution"; Phases 7A-7B,
migrations 0016-0017).

**Simulated orders only.** No table here represents a real brokerage order or account; the
only broker is the isolated paper broker (``paper_broker_*``), whose rows carry a
``simulated`` flag that the database pins to true.

* ``execution_requests``: every submission request (idempotency key, request hash, who
  asked, and whether it created an order or was refused, with the reasons). Append-only.
* ``orders``: the current-state projection of a paper order. Inserted PROPOSED; afterwards
  only the ``order_events`` trigger (``atlas_order_event_apply``, the state machine)
  changes it, and a deferred trigger refuses to commit a PROPOSED order. Never deleted.
* ``order_events``: append-only, gapless per order: every state change with its timestamp,
  source, actor and reason.
* ``order_fills``: append-only fills, each tied to exactly one paper broker event and one
  6A journal entry (UNIQUE both ways), so a fill can be applied and posted once.
* ``order_requests``: idempotency records for order commands (cancellation).
* ``paper_broker_state``: the simulator's run state and heartbeat.
* ``paper_broker_orders``: the simulator's own book (one row per client order: duplicate
  submissions are suppressed by UNIQUE ``client_order_id``).
* ``paper_broker_events``: append-only simulator events, gapless per broker order. FILL
  events come from CONTROLLED_TEST (labelled test events) or, since Phase 7B, SIMULATION
  (derived from stored market data under a recorded execution model version).
* ``outbox_events``: the transactional outbox (spec 4.1): written in the transaction that
  changed the state, delivered at least once, consumed idempotently.

Phase 7B (simulated execution and position management):

* ``execution_models``: immutable, content-addressed execution-model versions (fill rules,
  spreads, slippage, impact, participation, costs). A book row records the version that
  works it; every simulated fill records the version that produced it.
* ``execution_attempts``: append-only evaluations of working orders by the simulator (only
  when the outcome changes, a stop triggers or a fill happens), with the market data used.
* ``execution_fill_details``: append-only price and cost breakdown of each simulated fill.
* ``paper_positions``: one simulated position per ENTRY order that filled (the trade it
  opened), its protection (stop, target, trailing stop, invalidation, expiry) and running
  figures. Changed only by the execution engine and the position monitor; never deleted.
* ``position_events``: append-only, gapless per position: everything that happened to it.
* ``position_close_requests``: operator close requests (idempotency key, outcome).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.execution.enums import (
    AttemptOutcome,
    BrokerEventSource,
    BrokerEventType,
    BrokerOrderStatus,
    BrokerRunState,
    CloseRequestStatus,
    DataBasis,
    ExecutionDecision,
    ExitReason,
    OrderCommand,
    OrderEventSource,
    OrderEventType,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    OutboxStatus,
    PositionEventType,
    PositionStatus,
)
from app.risk.enums import TradeDirection

AMOUNT = Numeric(28, 8)
QUANTITY = Numeric(38, 18)
PRICE = Numeric(30, 10)
FINAL = "status IN ('FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'FAILED')"
KEY_FORMAT = "idempotency_key ~ '^[A-Za-z0-9._:@/-]{8,160}$'"
SHA_FORMAT = "request_sha256 ~ '^[0-9a-f]{64}$'"
PAPER_BROKER_ID = "atlas-paper-broker"


class ExecutionRequestRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "execution_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(KEY_FORMAT, name="key_format"),
        CheckConstraint(SHA_FORMAT, name="request_sha256_format"),
        CheckConstraint(
            "decision <> 'REFUSED' OR jsonb_array_length(reasons) > 0", name="refusal_explained"
        ),
        Index(None, "portfolio_account_id", "requested_at"),
        Index(None, "reservation_id"),
        Index(None, "decision", "requested_at"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_sha256: Mapped[str] = mapped_column(String(64))
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    # Set when the referenced reservation exists (an unauthorized request may name none).
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    purpose: Mapped[OrderPurpose] = mapped_column(
        str_enum(OrderPurpose, "execution_request_purpose")
    )
    decision: Mapped[ExecutionDecision] = mapped_column(
        str_enum(ExecutionDecision, "execution_request_decision")
    )
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    requested_by: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime]
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("execution_request_id"),
        CheckConstraint(KEY_FORMAT, name="key_format"),
        CheckConstraint(SHA_FORMAT, name="request_sha256_format"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "filled_quantity >= 0 AND remaining_quantity >= 0 "
            "AND filled_quantity + remaining_quantity = quantity",
            name="quantity_conservation",
        ),
        CheckConstraint(
            "(limit_price IS NOT NULL) = (order_type IN ('LIMIT', 'STOP_LIMIT', 'TAKE_PROFIT'))",
            name="limit_price_matches_type",
        ),
        CheckConstraint(
            "(stop_price IS NOT NULL) = (order_type IN ('STOP', 'STOP_LIMIT'))",
            name="stop_price_matches_type",
        ),
        CheckConstraint(
            "(order_type = 'TRAILING_STOP') = "
            "((trail_amount IS NOT NULL) <> (trail_percent IS NOT NULL))",
            name="trailing_has_one_offset",
        ),
        CheckConstraint(
            "(limit_price IS NULL OR limit_price > 0) AND (stop_price IS NULL OR stop_price > 0) "
            "AND (trail_amount IS NULL OR trail_amount > 0) "
            "AND (trail_percent IS NULL OR (trail_percent > 0 AND trail_percent < 100)) "
            "AND (protection_price IS NULL OR protection_price > 0)",
            name="prices_positive",
        ),
        CheckConstraint(
            "order_type <> 'TAKE_PROFIT' OR purpose = 'EXIT'", name="take_profit_is_exit"
        ),
        CheckConstraint(
            "(purpose = 'ENTRY' AND parent_order_id IS NULL AND protection_price IS NOT NULL "
            "AND ((direction = 'LONG' AND side = 'BUY') "
            "  OR (direction = 'SHORT' AND side = 'SELL_SHORT'))) "
            "OR (purpose = 'EXIT' AND parent_order_id IS NOT NULL "
            "AND ((direction = 'LONG' AND side = 'SELL') "
            "  OR (direction = 'SHORT' AND side = 'BUY_TO_COVER')))",
            name="purpose_side_direction",
        ),
        CheckConstraint("quantity_increment > 0", name="increment_positive"),
        CheckConstraint("price_increment IS NULL OR price_increment > 0", name="tick_positive"),
        CheckConstraint("status <> 'FILLED' OR remaining_quantity = 0", name="filled_is_complete"),
        CheckConstraint(
            "status <> 'PARTIALLY_FILLED' OR (filled_quantity > 0 AND remaining_quantity > 0)",
            name="partial_is_partial",
        ),
        CheckConstraint(
            "(filled_quantity = 0) = (average_fill_price IS NULL)", name="average_iff_filled"
        ),
        CheckConstraint(f"(NOT ({FINAL})) = (closed_at IS NULL)", name="closed_iff_final"),
        CheckConstraint("expires_at > submitted_at", name="expires_after_submission"),
        CheckConstraint("last_event_sequence >= 0", name="sequence_non_negative"),
        CheckConstraint("applied_broker_sequence >= 0", name="broker_sequence_non_negative"),
        CheckConstraint("exit_reason IS NULL OR purpose = 'EXIT'", name="exit_reason_for_exits"),
        # One ENTRY order per reservation (a retry under another key cannot attach twice).
        Index(
            "uq_orders_entry_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("purpose = 'ENTRY'"),
        ),
        Index(None, "portfolio_account_id", "status"),
        Index(None, "portfolio_account_id", "submitted_at"),
        Index(None, "status", "expires_at"),
        Index(None, "parent_order_id"),
        Index(None, "asset_id", "status"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    execution_request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_requests.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_sha256: Mapped[str] = mapped_column(String(64))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    quote_currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    instrument_spec_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument_specs.id", ondelete="RESTRICT")
    )
    # Instrument precision the order was validated against (copied from the spec).
    quantity_increment: Mapped[Decimal] = mapped_column(QUANTITY)
    price_increment: Mapped[Decimal | None] = mapped_column(PRICE)
    purpose: Mapped[OrderPurpose] = mapped_column(str_enum(OrderPurpose, "order_purpose"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, "order_side"))
    direction: Mapped[TradeDirection] = mapped_column(
        str_enum(TradeDirection, "order_position_direction")
    )
    order_type: Mapped[OrderType] = mapped_column(str_enum(OrderType, "order_type"))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    trail_amount: Mapped[Decimal | None] = mapped_column(PRICE)
    trail_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    # ENTRY: the least favourable fill price the reservation was sized for (the approval's
    # worst entry price). A fill beyond it would carry more risk than was reserved.
    protection_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus, "order_status"))
    filled_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    remaining_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    average_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    filled_value: Mapped[Decimal] = mapped_column(Numeric(38, 18), server_default="0")
    # Linkage: the reservation (ENTRY: the one it consumes; EXIT: its parent's, for
    # traceability only), the assessment that approved it, the originating intent.
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    risk_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT")
    )
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT")
    )
    strategy_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="RESTRICT")
    )
    broker_order_id: Mapped[uuid.UUID | None] = mapped_column()
    applied_broker_sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    status_reason: Mapped[str | None] = mapped_column(Text)
    last_event_sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    requested_by: Mapped[str] = mapped_column(String(128))
    # Phase 7B: why an EXIT order was placed (the position monitor sets it; NULL = an
    # operator exit outside the monitor).
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        str_enum(ExitReason, "order_exit_reason")
    )
    # Service clock (``created_at`` is the database wall time).
    submitted_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderFillRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "order_fills"
    __table_args__ = (
        UniqueConstraint("broker_event_id"),
        UniqueConstraint("journal_entry_id"),
        UniqueConstraint("order_id", "sequence"),
        CheckConstraint("quantity > 0 AND price > 0", name="fill_positive"),
        CheckConstraint("commission >= 0 AND fees >= 0", name="costs_non_negative"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        # A labelled controlled test event or (Phase 7B) a simulated fill; never anything else.
        CheckConstraint("source IN ('CONTROLLED_TEST', 'SIMULATION')", name="fill_source"),
        Index(None, "portfolio_account_id", "filled_at"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    broker_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_broker_events.id", ondelete="RESTRICT")
    )
    journal_entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    # ENTRY fills: the reservation consumption written in the same transaction.
    reservation_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("risk_reservation_events.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, "order_fill_side"))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    commission: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    fees: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    source: Mapped[BrokerEventSource] = mapped_column(
        str_enum(BrokerEventSource, "order_fill_source")
    )
    filled_at: Mapped[datetime]
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderEventRecord(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence"),
        UniqueConstraint("broker_event_id"),
        UniqueConstraint("fill_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(event_type = 'FILL') = (fill_id IS NOT NULL) "
            "AND (event_type = 'FILL') = (quantity > 0) "
            "AND (event_type = 'FILL') = (price IS NOT NULL)",
            name="fill_fields_match_type",
        ),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint(
            "(broker_event_id IS NULL) = (broker_sequence IS NULL)", name="broker_ref_complete"
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        CheckConstraint("length(btrim(actor)) >= 1", name="actor_given"),
        Index(None, "event_type", "occurred_at"),
        Index(None, "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[OrderEventType] = mapped_column(str_enum(OrderEventType, "order_event_type"))
    from_status: Mapped[OrderStatus | None] = mapped_column(
        str_enum(OrderStatus, "order_event_from_status")
    )
    to_status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus, "order_event_to_status"))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    fill_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("order_fills.id", ondelete="RESTRICT")
    )
    broker_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_broker_events.id", ondelete="RESTRICT")
    )
    broker_sequence: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[OrderEventSource] = mapped_column(
        str_enum(OrderEventSource, "order_event_source")
    )
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OrderRequestRecord(Base):
    """Idempotency records of order commands (one per key, with the request hash)."""

    __tablename__ = "order_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(KEY_FORMAT, name="key_format"),
        CheckConstraint(SHA_FORMAT, name="request_sha256_format"),
        Index(None, "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    command: Mapped[OrderCommand] = mapped_column(str_enum(OrderCommand, "order_command"))
    request_sha256: Mapped[str] = mapped_column(String(64))
    result_status: Mapped[OrderStatus] = mapped_column(
        str_enum(OrderStatus, "order_request_result")
    )
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaperBrokerStateRecord(Base):
    __tablename__ = "paper_broker_state"
    __table_args__ = (
        CheckConstraint("simulated", name="always_simulated"),
        CheckConstraint("broker_id ~ '^atlas-paper-[a-z0-9-]+$'", name="paper_broker_id"),
    )

    broker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Pinned to true by a CHECK: no row can describe a real brokerage connection.
    simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    status: Mapped[BrokerRunState] = mapped_column(
        str_enum(BrokerRunState, "paper_broker_run_state")
    )
    fill_model: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None]
    stopped_at: Mapped[datetime | None]
    last_heartbeat_at: Mapped[datetime | None]
    last_recovery_at: Mapped[datetime | None]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaperBrokerOrderRecord(UUIDPrimaryKeyMixin, Base):
    """The simulator's own book: what the paper broker believes about each order."""

    __tablename__ = "paper_broker_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id"),
        CheckConstraint("simulated", name="always_simulated"),
        CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name="quantities_valid",
        ),
        CheckConstraint("status <> 'FILLED' OR filled_quantity = quantity", name="filled_complete"),
        CheckConstraint("last_event_sequence >= 0", name="sequence_non_negative"),
        Index(None, "status", "expires_at"),
    )

    broker_id: Mapped[str] = mapped_column(
        ForeignKey("paper_broker_state.broker_id", ondelete="RESTRICT")
    )
    simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    client_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, "paper_broker_order_side"))
    order_type: Mapped[OrderType] = mapped_column(str_enum(OrderType, "paper_broker_order_type"))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    filled_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[BrokerOrderStatus] = mapped_column(
        str_enum(BrokerOrderStatus, "paper_broker_order_status")
    )
    expires_at: Mapped[datetime]
    received_at: Mapped[datetime]
    last_event_sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    # Phase 7B simulation state. The execution model version working the order (set at the
    # first evaluation, never changed); stop trigger and trailing-stop state; the market
    # data cursors (each observation's liquidity is used at most once); costs so far.
    execution_model_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execution_models.id", ondelete="RESTRICT")
    )
    arrival_price: Mapped[Decimal | None] = mapped_column(PRICE)
    triggered_at: Mapped[datetime | None]
    trigger_price: Mapped[Decimal | None] = mapped_column(PRICE)
    trail_reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    current_stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    quote_cursor_id: Mapped[int | None] = mapped_column(BigInteger)
    trade_cursor_at: Mapped[datetime | None]
    trade_cursor_id: Mapped[int | None] = mapped_column(BigInteger)
    bar_cursor_at: Mapped[datetime | None]
    market_cursor_at: Mapped[datetime | None]
    commission_charged: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    evaluations: Mapped[int] = mapped_column(Integer, server_default="0")
    last_evaluated_at: Mapped[datetime | None]
    last_attempt_outcome: Mapped[AttemptOutcome | None] = mapped_column(
        str_enum(AttemptOutcome, "paper_broker_attempt_outcome")
    )
    last_attempt_code: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaperBrokerEventRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "paper_broker_events"
    __table_args__ = (
        UniqueConstraint("broker_order_id", "sequence"),
        UniqueConstraint("external_ref"),
        CheckConstraint("simulated", name="always_simulated"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "event_type <> 'FILL' OR (source IN ('CONTROLLED_TEST', 'SIMULATION') "
            "AND quantity > 0 AND price > 0)",
            name="fills_are_tests_or_simulated",
        ),
        CheckConstraint(
            "source <> 'SIMULATION' OR execution_model_id IS NOT NULL",
            name="simulation_has_model",
        ),
        CheckConstraint(
            "event_type <> 'TRIGGERED' OR source = 'SIMULATION'", name="triggers_are_simulated"
        ),
        CheckConstraint(
            "event_type = 'FILL' OR (quantity = 0 AND price IS NULL)", name="only_fills_move"
        ),
        CheckConstraint("commission >= 0 AND fees >= 0", name="costs_non_negative"),
        CheckConstraint(
            "source <> 'CONTROLLED_TEST' OR external_ref IS NOT NULL", name="test_event_keyed"
        ),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        Index(None, "client_order_id"),
        Index(None, "occurred_at"),
    )

    broker_id: Mapped[str] = mapped_column(
        ForeignKey("paper_broker_state.broker_id", ondelete="RESTRICT")
    )
    simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    broker_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_broker_orders.id", ondelete="RESTRICT")
    )
    client_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[BrokerEventType] = mapped_column(
        str_enum(BrokerEventType, "paper_broker_event_type")
    )
    source: Mapped[BrokerEventSource] = mapped_column(
        str_enum(BrokerEventSource, "paper_broker_event_source")
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    commission: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    fees: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    # Idempotency key of an injected controlled test event (a retry records nothing new).
    external_ref: Mapped[str | None] = mapped_column(String(160))
    # Phase 7B: the execution model version a SIMULATION event was derived under.
    execution_model_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execution_models.id", ondelete="RESTRICT")
    )
    reason: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OutboxEventRecord(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("(status = 'DELIVERED') = (delivered_at IS NOT NULL)", name="delivered_at"),
        CheckConstraint("(status = 'DEAD') = (dead_at IS NOT NULL)", name="dead_at"),
        CheckConstraint("length(btrim(topic)) >= 3", name="topic_given"),
        Index(None, "status", "available_at"),
        Index(None, "aggregate_type", "aggregate_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    aggregate_type: Mapped[str] = mapped_column(String(48))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    # A second write of the same logical event is suppressed (ON CONFLICT DO NOTHING).
    dedupe_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    status: Mapped[OutboxStatus] = mapped_column(str_enum(OutboxStatus, "outbox_status"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None]
    dead_at: Mapped[datetime | None]
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------------- Phase 7B
class ExecutionModelRecord(Base):
    """An immutable execution-model version: the assumptions simulated fills are derived
    under. Content-addressed (``definition_sha256``); a changed assumption is a new row."""

    __tablename__ = "execution_models"
    __table_args__ = (
        UniqueConstraint("definition_sha256"),
        UniqueConstraint("code", "version"),
        CheckConstraint("definition_sha256 ~ '^[0-9a-f]{64}$'", name="sha_format"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("simulated", name="always_simulated"),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    definition_sha256: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExecutionAttemptRecord(Base):
    """One evaluation of a working order by the simulator that decided something new: a
    changed outcome or reason, a stop trigger, or a fill. Append-only."""

    __tablename__ = "execution_attempts"
    __table_args__ = (
        CheckConstraint("length(btrim(code)) >= 2", name="code_given"),
        CheckConstraint(
            "(outcome IN ('FILLED', 'PARTIALLY_FILLED')) = (fill_event_id IS NOT NULL)",
            name="fill_iff_filled",
        ),
        Index(None, "order_id", "evaluated_at"),
        Index(None, "outcome", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    broker_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_broker_orders.id", ondelete="RESTRICT")
    )
    execution_model_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("execution_models.id", ondelete="RESTRICT")
    )
    evaluated_at: Mapped[datetime]
    outcome: Mapped[AttemptOutcome] = mapped_column(
        str_enum(AttemptOutcome, "execution_attempt_outcome")
    )
    code: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    triggered: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    fill_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("paper_broker_events.id", ondelete="RESTRICT")
    )
    # The stored observations the decision used (table, id, times, values): provenance.
    market: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExecutionFillDetailRecord(Base):
    """The price and cost breakdown of one simulated fill (1:1 with its broker FILL event)."""

    __tablename__ = "execution_fill_details"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND price > 0", name="fill_positive"),
        CheckConstraint(
            "commission >= 0 AND regulatory_fees >= 0 AND exchange_fees >= 0",
            name="costs_non_negative",
        ),
        CheckConstraint(
            "reference_price IS NULL OR reference_price > 0", name="reference_positive"
        ),
        CheckConstraint(
            "participation IS NULL OR (participation > 0 AND participation <= 1)",
            name="participation_range",
        ),
        Index(None, "order_id"),
        Index(None, "portfolio_account_id", "filled_at"),
    )

    broker_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_broker_events.id", ondelete="RESTRICT"), primary_key=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    execution_model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("execution_models.id", ondelete="RESTRICT")
    )
    basis: Mapped[DataBasis] = mapped_column(str_enum(DataBasis, "execution_data_basis"))
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, "execution_fill_side"))
    order_type: Mapped[OrderType] = mapped_column(str_enum(OrderType, "execution_fill_order_type"))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    # Mid (quote) or close (bar) when the fill was decided; the touch it executed against.
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    touch_price: Mapped[Decimal | None] = mapped_column(PRICE)
    arrival_price: Mapped[Decimal | None] = mapped_column(PRICE)
    spread: Mapped[Decimal | None] = mapped_column(PRICE)
    # Adverse distance of the fill from the reference, in basis points (negative = better).
    slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    impact_bps: Mapped[Decimal] = mapped_column(Numeric(18, 6), server_default="0")
    shortfall_bps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    liquidity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    participation: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    commission: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    regulatory_fees: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    exchange_fees: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    gap: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    filled_at: Mapped[datetime]
    observations: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    assumptions: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PaperPositionRecord(UUIDPrimaryKeyMixin, Base):
    """A simulated position: the trade one ENTRY order opened, until it is closed."""

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("entry_order_id"),
        CheckConstraint("simulated", name="always_simulated"),
        CheckConstraint(
            "quantity_opened > 0 AND quantity_closed >= 0 AND open_quantity >= 0 "
            "AND open_quantity = quantity_opened - quantity_closed",
            name="quantity_conservation",
        ),
        CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)", name="closed_at_iff_closed"
        ),
        CheckConstraint("status <> 'CLOSED' OR open_quantity = 0", name="closed_is_flat"),
        CheckConstraint(
            "(quantity_closed = 0) = (average_exit_price IS NULL)",
            name="exit_average_iff_exited",
        ),
        CheckConstraint(
            "commissions >= 0 AND fees >= 0 AND borrow_accrued >= 0 AND borrow_paid >= 0 "
            "AND borrow_paid <= borrow_accrued",
            name="costs_non_negative",
        ),
        CheckConstraint(
            "stop_price > 0 AND initial_stop_price > 0 "
            "AND (target_price IS NULL OR target_price > 0) "
            "AND (invalidation_price IS NULL OR invalidation_price > 0) "
            "AND (trailing_stop_price IS NULL OR trailing_stop_price > 0)",
            name="levels_positive",
        ),
        # The stop only ever tightens from the approved stop the reservation was sized with.
        CheckConstraint(
            "(direction = 'LONG' AND stop_price >= initial_stop_price "
            " AND (target_price IS NULL OR target_price > stop_price)) "
            "OR (direction = 'SHORT' AND stop_price <= initial_stop_price "
            " AND (target_price IS NULL OR target_price < stop_price))",
            name="levels_ordered",
        ),
        CheckConstraint(
            "(trail_amount IS NULL OR trail_amount > 0) AND (trail_percent IS NULL OR "
            "(trail_percent > 0 AND trail_percent < 100)) "
            "AND NOT (trail_amount IS NOT NULL AND trail_percent IS NOT NULL)",
            name="trail_valid",
        ),
        CheckConstraint("last_event_sequence >= 0", name="sequence_non_negative"),
        Index(None, "portfolio_account_id", "status"),
        Index(None, "asset_id", "status"),
        Index(None, "status", "expires_at"),
    )

    portfolio_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portfolio_accounts.id", ondelete="RESTRICT")
    )
    simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code", ondelete="RESTRICT")
    )
    direction: Mapped[TradeDirection] = mapped_column(
        str_enum(TradeDirection, "paper_position_direction")
    )
    status: Mapped[PositionStatus] = mapped_column(
        str_enum(PositionStatus, "paper_position_status")
    )
    entry_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_reservations.id", ondelete="RESTRICT")
    )
    strategy_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("strategy_proposals.id", ondelete="RESTRICT")
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="RESTRICT")
    )
    strategy_id: Mapped[str | None] = mapped_column(String(128))
    origin: Mapped[str] = mapped_column(String(32))
    quantity_increment: Mapped[Decimal] = mapped_column(QUANTITY)
    price_increment: Mapped[Decimal | None] = mapped_column(PRICE)
    quantity_opened: Mapped[Decimal] = mapped_column(QUANTITY)
    quantity_closed: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    open_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    entry_value: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    exit_value: Mapped[Decimal] = mapped_column(Numeric(38, 18), server_default="0")
    average_entry_price: Mapped[Decimal] = mapped_column(PRICE)
    average_exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    # Asset-currency figures from the 6A ledger movements and fills of this position.
    realized_pnl: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    commissions: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    fees: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    borrow_accrued: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    borrow_paid: Mapped[Decimal] = mapped_column(AMOUNT, server_default="0")
    borrow_accrued_through: Mapped[datetime | None]
    # Protection. ``initial_stop_price`` is the approved stop the reservation was sized
    # with; ``stop_price`` may only tighten from it (never loosen).
    initial_stop_price: Mapped[Decimal] = mapped_column(PRICE)
    stop_price: Mapped[Decimal] = mapped_column(PRICE)
    target_price: Mapped[Decimal | None] = mapped_column(PRICE)
    invalidation_price: Mapped[Decimal | None] = mapped_column(PRICE)
    trail_amount: Mapped[Decimal | None] = mapped_column(PRICE)
    trail_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    trail_reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    trailing_stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    expires_at: Mapped[datetime | None]
    # The trigger that decided to exit (kept until the position is closed).
    trigger_reason: Mapped[ExitReason | None] = mapped_column(
        str_enum(ExitReason, "paper_position_trigger_reason")
    )
    triggered_at: Mapped[datetime | None]
    trigger_price: Mapped[Decimal | None] = mapped_column(PRICE)
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        str_enum(ExitReason, "paper_position_exit_reason")
    )
    exits_submitted: Mapped[int] = mapped_column(Integer, server_default="0")
    # Monitoring state: market cursor, last fresh mark (a stored observation, never invented).
    monitor_cursor_at: Mapped[datetime | None]
    monitor_trade_id: Mapped[int | None] = mapped_column(BigInteger)
    monitor_bar_at: Mapped[datetime | None]
    monitor_status: Mapped[str] = mapped_column(String(24), server_default="ACTIVE")
    last_mark_price: Mapped[Decimal | None] = mapped_column(PRICE)
    last_mark_at: Mapped[datetime | None]
    last_mark_source: Mapped[str | None] = mapped_column(String(16))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(AMOUNT)
    opened_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    last_event_sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PositionEventRecord(Base):
    __tablename__ = "position_events"
    __table_args__ = (
        UniqueConstraint("position_id", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        CheckConstraint("length(btrim(actor)) >= 1", name="actor_given"),
        Index(None, "event_type", "occurred_at"),
        Index(None, "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_positions.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[PositionEventType] = mapped_column(
        str_enum(PositionEventType, "position_event_type")
    )
    occurred_at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default="0")
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    open_quantity_after: Mapped[Decimal] = mapped_column(QUANTITY)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    fill_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("order_fills.id", ondelete="RESTRICT")
    )
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    details: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_txid: Mapped[int] = mapped_column(BigInteger, server_default=text("txid_current()"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PositionCloseRequestRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "position_close_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(KEY_FORMAT, name="key_format"),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_given"),
        CheckConstraint(
            "(status IN ('COMPLETED', 'REFUSED')) = (resolved_at IS NOT NULL)",
            name="resolved_iff_final",
        ),
        Index(None, "position_id", "status"),
    )

    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("paper_positions.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    requested_by: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[CloseRequestStatus] = mapped_column(
        str_enum(CloseRequestStatus, "position_close_request_status")
    )
    requested_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "PAPER_BROKER_ID",
    "ExecutionAttemptRecord",
    "ExecutionFillDetailRecord",
    "ExecutionModelRecord",
    "ExecutionRequestRecord",
    "OrderEventRecord",
    "OrderFillRecord",
    "OrderRecord",
    "OrderRequestRecord",
    "OutboxEventRecord",
    "PaperBrokerEventRecord",
    "PaperBrokerOrderRecord",
    "PaperBrokerStateRecord",
    "PaperPositionRecord",
    "PositionCloseRequestRecord",
    "PositionEventRecord",
]
