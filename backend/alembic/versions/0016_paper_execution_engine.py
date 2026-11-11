"""paper execution engine (Phase 7A)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-06 04:24:12.925276+00:00

Paper execution and order management (decisions D-156 to D-163,
docs/architecture/execution.md). **Simulated orders only**: no table describes a real
brokerage order, account or credential; the paper broker rows pin ``simulated = true``.

* ``orders``: current-state projection. ``atlas_order_insert_guard`` inserts it PROPOSED
  and unfilled, and checks an ENTRY order against its 6C reservation (APPROVED, same
  account/asset/direction, quantity within the approval, protected at the approval's worst
  entry price, carrying its assessment and origin) and an EXIT order against its ENTRY
  parent; ``atlas_order_update_guard`` lets only the event trigger change it; the deferred
  ``atlas_order_decided`` refuses to commit a PROPOSED order or an approved ENTRY order
  whose reservation is not attached to it.
* ``order_events``: append-only; ``atlas_order_event_apply`` is the state machine (legal
  transitions from ``atlas_order_transition_allowed``, gapless numbering with SQLSTATE
  40001 on a stale writer, broker-originated events applied once and in broker order,
  ACCEPTED/FILL/CANCEL_CONFIRMED/CANCEL_REJECTED only from a paper broker event, no
  engine-side finish of an order the broker is working, fill arithmetic) and updates the
  projection.
* ``order_fills``: append-only; ``atlas_order_fill_guard`` ties each fill to its paper
  broker FILL event, to a 6A journal entry moving exactly the filled quantity, and (ENTRY)
  to the 6C consumption written in the same transaction.
* ``paper_broker_events``: append-only, gapless per book row; FILL only from
  CONTROLLED_TEST (Phase 7A has no fill algorithm).
* ``execution_requests``, ``order_requests``: append-only idempotency records.
* ``paper_broker_orders``, ``paper_broker_state``, ``outbox_events``: never deleted;
  guarded updates (identity immutable, forward-only).

Downgrading is possible only before any 7A row exists (append-only history).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "execution_requests",
    "order_events",
    "order_fills",
    "order_requests",
    "paper_broker_events",
)
NO_DELETE_TABLES = ("orders", "paper_broker_orders", "paper_broker_state", "outbox_events")
FINAL_ORDER = "('FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'FAILED')"

# The legal order transitions (app/execution/state_machine.py TRANSITIONS, frozen here as
# this migration's definition; test_order_state_machine compares the two).
TRANSITIONS = (
    (None, "CREATED", "PROPOSED"),
    ("PROPOSED", "APPROVED", "APPROVED"),
    ("PROPOSED", "REJECTED", "REJECTED"),
    ("APPROVED", "ACCEPTED", "OPEN"),
    ("APPROVED", "REJECTED", "REJECTED"),
    ("APPROVED", "CANCEL_REQUESTED", "CANCEL_PENDING"),
    ("APPROVED", "EXPIRED", "EXPIRED"),
    ("APPROVED", "FAILED", "FAILED"),
    ("OPEN", "FILL", "PARTIALLY_FILLED"),
    ("OPEN", "FILL", "FILLED"),
    ("OPEN", "CANCEL_REQUESTED", "CANCEL_PENDING"),
    ("OPEN", "EXPIRED", "EXPIRED"),
    ("OPEN", "FAILED", "FAILED"),
    ("PARTIALLY_FILLED", "FILL", "PARTIALLY_FILLED"),
    ("PARTIALLY_FILLED", "FILL", "FILLED"),
    ("PARTIALLY_FILLED", "CANCEL_REQUESTED", "CANCEL_PENDING"),
    ("PARTIALLY_FILLED", "EXPIRED", "EXPIRED"),
    ("PARTIALLY_FILLED", "FAILED", "FAILED"),
    ("CANCEL_PENDING", "ACCEPTED", "CANCEL_PENDING"),
    ("CANCEL_PENDING", "FILL", "CANCEL_PENDING"),
    ("CANCEL_PENDING", "FILL", "FILLED"),
    ("CANCEL_PENDING", "CANCEL_CONFIRMED", "CANCELLED"),
    ("CANCEL_PENDING", "CANCEL_REJECTED", "OPEN"),
    ("CANCEL_PENDING", "CANCEL_REJECTED", "PARTIALLY_FILLED"),
    ("CANCEL_PENDING", "EXPIRED", "EXPIRED"),
    ("CANCEL_PENDING", "FAILED", "FAILED"),
)
_values = ",\n            ".join(
    f"({'NULL::text' if f is None else repr(f)}, '{e}', '{t}')" for f, e, t in TRANSITIONS
)
TRANSITION_FUNCTION = f"""
CREATE FUNCTION atlas_order_transition_allowed(from_status text, event_type text,
                                               to_status text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM (VALUES
            {_values}
        ) AS t(f, e, s)
        WHERE t.f IS NOT DISTINCT FROM from_status AND t.e = event_type AND t.s = to_status
    )
$$;
"""  # noqa: S608  # constant transition literals only

# Order columns that never change after the insert.
ORDER_IMMUTABLE = (
    "id",
    "portfolio_account_id",
    "execution_request_id",
    "idempotency_key",
    "request_sha256",
    "asset_id",
    "symbol",
    "asset_class",
    "quote_currency",
    "instrument_spec_id",
    "quantity_increment",
    "price_increment",
    "purpose",
    "side",
    "direction",
    "order_type",
    "quantity",
    "limit_price",
    "stop_price",
    "trail_amount",
    "trail_percent",
    "protection_price",
    "reservation_id",
    "risk_assessment_id",
    "parent_order_id",
    "strategy_proposal_id",
    "ai_decision_id",
    "requested_by",
    "submitted_at",
    "expires_at",
    "created_txid",
    "created_at",
)
BOOK_IMMUTABLE = (
    "id",
    "broker_id",
    "simulated",
    "client_order_id",
    "portfolio_account_id",
    "asset_id",
    "side",
    "order_type",
    "quantity",
    "limit_price",
    "stop_price",
    "expires_at",
    "received_at",
    "created_at",
)
OUTBOX_IMMUTABLE = (
    "id",
    "topic",
    "aggregate_type",
    "aggregate_id",
    "dedupe_key",
    "payload",
    "created_txid",
    "created_at",
)


def _changed(columns: tuple[str, ...]) -> str:
    return " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in columns)


# Built from constant column names only (no input reaches this SQL).
GUARD_FUNCTIONS = {
    "atlas_order_insert_guard": """
    DECLARE
        r record;
        ap record;
        p record;
    BEGIN
        NEW.created_txid := txid_current();
        IF NEW.status <> 'PROPOSED' OR NEW.last_event_sequence <> 0
           OR NEW.filled_quantity <> 0 OR NEW.remaining_quantity <> NEW.quantity
           OR NEW.average_fill_price IS NOT NULL OR NEW.filled_value <> 0
           OR NEW.applied_broker_sequence <> 0 OR NEW.broker_order_id IS NOT NULL
           OR NEW.closed_at IS NOT NULL THEN
            RAISE EXCEPTION 'an order is inserted PROPOSED and unfilled; its events change it'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT portfolio_account_id, asset_id, direction, status, assessment_id,
               approved_quantity, strategy_proposal_id, ai_decision_id
            INTO r FROM risk_reservations WHERE id = NEW.reservation_id;
        IF NEW.purpose = 'ENTRY' THEN
            IF r.portfolio_account_id <> NEW.portfolio_account_id OR r.asset_id <> NEW.asset_id
               OR r.direction <> NEW.direction THEN
                RAISE EXCEPTION 'an entry order executes a reservation of its own account, '
                    'asset and direction' USING ERRCODE = 'check_violation';
            END IF;
            IF r.status <> 'APPROVED' THEN
                RAISE EXCEPTION 'reservation % is %: only an APPROVED reservation is executed',
                    NEW.reservation_id, r.status USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.quantity > r.approved_quantity THEN
                RAISE EXCEPTION 'an order of % exceeds its reservation of %', NEW.quantity,
                    r.approved_quantity USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.risk_assessment_id IS DISTINCT FROM r.assessment_id
               OR NEW.strategy_proposal_id IS DISTINCT FROM r.strategy_proposal_id
               OR NEW.ai_decision_id IS DISTINCT FROM r.ai_decision_id THEN
                RAISE EXCEPTION 'an entry order carries its reservation''s assessment and origin'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT worst_entry_price INTO ap FROM risk_reservation_approvals
                WHERE reservation_id = NEW.reservation_id;
            IF ap.worst_entry_price IS NULL
               OR NEW.protection_price IS DISTINCT FROM ap.worst_entry_price THEN
                RAISE EXCEPTION 'an entry order is protected at its approval''s worst entry '
                    'price' USING ERRCODE = 'check_violation';
            END IF;
        ELSE
            SELECT portfolio_account_id, asset_id, direction, purpose, reservation_id
                INTO p FROM orders WHERE id = NEW.parent_order_id;
            IF p.purpose IS DISTINCT FROM 'ENTRY'
               OR p.portfolio_account_id <> NEW.portfolio_account_id
               OR p.asset_id <> NEW.asset_id OR p.direction <> NEW.direction
               OR p.reservation_id <> NEW.reservation_id THEN
                RAISE EXCEPTION 'an exit order reduces the position of its own ENTRY order'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_order_update_guard": f"""
    BEGIN
        IF pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION 'orders is a projection: it changes only through order_events'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF {_changed(ORDER_IMMUTABLE)} THEN
            RAISE EXCEPTION 'immutable order fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
    # The order state machine: numbering, legal transitions, broker origin and order of
    # application, fill arithmetic; then the projection update.
    "atlas_order_event_apply": f"""
    DECLARE
        o orders%ROWTYPE;
        f record;
        b record;
        book_id uuid := NULL;
        new_filled numeric;
        new_value numeric;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT * INTO o FROM orders WHERE id = NEW.order_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'order % does not exist', NEW.order_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF NEW.sequence <> o.last_event_sequence + 1 THEN
            RAISE EXCEPTION 'order % events are numbered without gaps: expected %, got %',
                o.id, o.last_event_sequence + 1, NEW.sequence
                USING ERRCODE = 'serialization_failure';
        END IF;
        IF (o.last_event_sequence = 0 AND NEW.from_status IS NOT NULL)
           OR (o.last_event_sequence > 0 AND NEW.from_status IS DISTINCT FROM o.status) THEN
            RAISE EXCEPTION 'order % is %, not %', o.id, o.status, NEW.from_status
                USING ERRCODE = 'serialization_failure';
        END IF;
        IF NOT atlas_order_transition_allowed(NEW.from_status, NEW.event_type,
                                              NEW.to_status) THEN
            RAISE EXCEPTION 'illegal order transition % --%--> %',
                COALESCE(NEW.from_status, '(new)'), NEW.event_type, NEW.to_status
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.broker_event_id IS NOT NULL THEN
            SELECT client_order_id, broker_order_id, sequence, event_type
                INTO b FROM paper_broker_events WHERE id = NEW.broker_event_id;
            IF b.client_order_id <> o.id OR b.sequence <> NEW.broker_sequence
               OR b.event_type <> NEW.event_type THEN
                RAISE EXCEPTION 'order event does not match paper broker event %',
                    NEW.broker_event_id USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.broker_sequence <> o.applied_broker_sequence + 1 THEN
                RAISE EXCEPTION 'paper broker events of order % are applied once and in order: '
                    'expected %, got %', o.id, o.applied_broker_sequence + 1,
                    NEW.broker_sequence USING ERRCODE = 'check_violation';
            END IF;
            book_id := b.broker_order_id;
        ELSIF NEW.event_type IN ('ACCEPTED', 'FILL', 'CANCEL_CONFIRMED', 'CANCEL_REJECTED') THEN
            RAISE EXCEPTION '% must come from a paper broker event', NEW.event_type
                USING ERRCODE = 'check_violation';
        ELSIF NEW.event_type IN ('EXPIRED', 'REJECTED', 'FAILED')
              AND EXISTS (SELECT 1 FROM paper_broker_orders
                          WHERE client_order_id = o.id
                            AND status IN ('WORKING', 'PARTIALLY_FILLED')) THEN
            RAISE EXCEPTION 'order % is being worked by the paper broker: only the broker can '
                'finish it', o.id USING ERRCODE = 'check_violation';
        END IF;

        new_filled := o.filled_quantity;
        new_value := o.filled_value;
        IF NEW.event_type = 'FILL' THEN
            IF NEW.quantity <= 0 OR NEW.quantity > o.remaining_quantity THEN
                RAISE EXCEPTION 'a fill of % does not fit the % remaining (over-fill)',
                    NEW.quantity, o.remaining_quantity USING ERRCODE = 'check_violation';
            END IF;
            SELECT order_id, quantity, price, broker_event_id
                INTO f FROM order_fills WHERE id = NEW.fill_id;
            IF f.order_id IS DISTINCT FROM o.id OR f.quantity <> NEW.quantity
               OR f.price <> NEW.price OR f.broker_event_id IS DISTINCT FROM NEW.broker_event_id
            THEN
                RAISE EXCEPTION 'the fill event must equal its fill record'
                    USING ERRCODE = 'check_violation';
            END IF;
            new_filled := new_filled + NEW.quantity;
            new_value := new_value + NEW.quantity * NEW.price;
            IF (new_filled = o.quantity) <> (NEW.to_status = 'FILLED') THEN
                RAISE EXCEPTION 'an order is FILLED exactly when nothing remains'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.quantity <> 0 OR NEW.price IS NOT NULL THEN
            RAISE EXCEPTION '% events move no quantity', NEW.event_type
                USING ERRCODE = 'check_violation';
        ELSIF NEW.event_type = 'CANCEL_REJECTED'
              AND (o.filled_quantity = 0) <> (NEW.to_status = 'OPEN') THEN
            RAISE EXCEPTION 'a rejected cancellation returns to OPEN only when nothing filled'
                USING ERRCODE = 'check_violation';
        END IF;

        UPDATE orders SET
            status = NEW.to_status,
            last_event_sequence = NEW.sequence,
            filled_quantity = new_filled,
            remaining_quantity = o.quantity - new_filled,
            filled_value = new_value,
            average_fill_price = CASE WHEN new_filled > 0
                                      THEN round(new_value / new_filled, 10) END,
            applied_broker_sequence = COALESCE(NEW.broker_sequence, o.applied_broker_sequence),
            broker_order_id = COALESCE(o.broker_order_id, book_id),
            status_reason = NEW.reason,
            closed_at = CASE WHEN NEW.to_status IN {FINAL_ORDER} THEN NEW.occurred_at END
        WHERE id = o.id;
        RETURN NEW;
    END;
    """,  # noqa: S608  # constant names only
    # A fill equals its paper broker event, posts exactly its quantity to the 6A asset
    # quantity ledger, and (ENTRY) consumes its reservation in the same transaction.
    "atlas_order_fill_guard": """
    DECLARE
        o record;
        b record;
        moved numeric;
        expected numeric;
        rev record;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT portfolio_account_id, asset_id, side, quote_currency, purpose, reservation_id
            INTO o FROM orders WHERE id = NEW.order_id;
        IF o.portfolio_account_id <> NEW.portfolio_account_id OR o.asset_id <> NEW.asset_id
           OR o.side <> NEW.side OR o.quote_currency <> NEW.currency THEN
            RAISE EXCEPTION 'a fill belongs to its order''s account, asset, side and currency'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT client_order_id, event_type, quantity, price, commission, fees, source
            INTO b FROM paper_broker_events WHERE id = NEW.broker_event_id;
        IF b.client_order_id <> NEW.order_id OR b.event_type <> 'FILL'
           OR b.quantity <> NEW.quantity OR b.price <> NEW.price
           OR b.commission <> NEW.commission OR b.fees <> NEW.fees OR b.source <> NEW.source THEN
            RAISE EXCEPTION 'a fill must equal its paper broker FILL event'
                USING ERRCODE = 'check_violation';
        END IF;
        SELECT sum(quantity_delta) INTO moved FROM asset_quantity_ledger
            WHERE entry_id = NEW.journal_entry_id AND asset_id = NEW.asset_id
              AND portfolio_account_id = NEW.portfolio_account_id;
        expected := CASE WHEN NEW.side IN ('BUY', 'BUY_TO_COVER')
                         THEN NEW.quantity ELSE -NEW.quantity END;
        IF moved IS NULL OR moved <> expected THEN
            RAISE EXCEPTION 'journal entry % does not move the % filled units',
                NEW.journal_entry_id, NEW.quantity USING ERRCODE = 'check_violation';
        END IF;
        IF o.purpose = 'ENTRY' THEN
            SELECT reservation_id, event_type, quantity, journal_entry_id, created_txid
                INTO rev FROM risk_reservation_events WHERE id = NEW.reservation_event_id;
            IF rev.reservation_id IS DISTINCT FROM o.reservation_id
               OR rev.event_type IS DISTINCT FROM 'CONSUMED' OR rev.quantity <> NEW.quantity
               OR rev.journal_entry_id IS DISTINCT FROM NEW.journal_entry_id
               OR rev.created_txid <> txid_current() THEN
                RAISE EXCEPTION 'an entry fill consumes its reservation in its own transaction'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.reservation_event_id IS NOT NULL THEN
            RAISE EXCEPTION 'an exit fill consumes no reservation'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_paper_broker_order_guard": f"""
    BEGIN
        IF {_changed(BOOK_IMMUTABLE)} THEN
            RAISE EXCEPTION 'immutable paper broker order fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.filled_quantity < OLD.filled_quantity
           OR NEW.last_event_sequence NOT IN (OLD.last_event_sequence,
                                              OLD.last_event_sequence + 1) THEN
            RAISE EXCEPTION 'a paper broker order only moves forward'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status IN ('FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')
           AND NEW.status <> OLD.status THEN
            RAISE EXCEPTION 'paper broker order % is final (%)', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
    "atlas_paper_broker_event_guard": """
    DECLARE
        bk record;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT client_order_id, broker_id, last_event_sequence
            INTO bk FROM paper_broker_orders WHERE id = NEW.broker_order_id;
        IF bk.client_order_id <> NEW.client_order_id OR bk.broker_id <> NEW.broker_id THEN
            RAISE EXCEPTION 'a paper broker event belongs to its book row'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.sequence <> bk.last_event_sequence THEN
            RAISE EXCEPTION 'paper broker events are numbered by their book without gaps'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_outbox_update_guard": f"""
    BEGIN
        IF {_changed(OUTBOX_IMMUTABLE)} THEN
            RAISE EXCEPTION 'outbox event identity and payload cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status <> 'PENDING' AND NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'outbox event % is %: final', OLD.id, OLD.status
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.attempts < OLD.attempts THEN
            RAISE EXCEPTION 'outbox attempts never decrease'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
}

DECIDED_CHECK = """
CREATE FUNCTION atlas_order_decided() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    o record;
    r record;
BEGIN
    SELECT status, purpose, reservation_id INTO o FROM orders WHERE id = NEW.id;
    IF o.status = 'PROPOSED' THEN
        RAISE EXCEPTION 'order % was not decided in its own transaction', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF o.purpose = 'ENTRY' AND o.status <> 'REJECTED' THEN
        SELECT execution_link, order_ref INTO r FROM risk_reservations
            WHERE id = o.reservation_id;
        IF r.execution_link = 'NONE' OR r.order_ref IS DISTINCT FROM NEW.id::text THEN
            RAISE EXCEPTION 'approved entry order % must have its reservation attached', NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
"""


def _guards() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
    for table in APPEND_ONLY_TABLES + NO_DELETE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )
    for table in NO_DELETE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
    op.execute(TRANSITION_FUNCTION)
    for name, body in GUARD_FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$;")
    for trigger, timing, table, function in (
        ("orders_insert_guard", "BEFORE INSERT", "orders", "atlas_order_insert_guard"),
        ("orders_update_guard", "BEFORE UPDATE", "orders", "atlas_order_update_guard"),
        ("order_events_apply", "BEFORE INSERT", "order_events", "atlas_order_event_apply"),
        ("order_fills_guard", "BEFORE INSERT", "order_fills", "atlas_order_fill_guard"),
        (
            "paper_broker_orders_guard",
            "BEFORE UPDATE",
            "paper_broker_orders",
            "atlas_paper_broker_order_guard",
        ),
        (
            "paper_broker_events_guard",
            "BEFORE INSERT",
            "paper_broker_events",
            "atlas_paper_broker_event_guard",
        ),
        (
            "outbox_events_update_guard",
            "BEFORE UPDATE",
            "outbox_events",
            "atlas_outbox_update_guard",
        ),
    ):
        op.execute(
            f"CREATE TRIGGER {trigger} {timing} ON {table} FOR EACH ROW "
            f"EXECUTE FUNCTION {function}()"
        )
    op.execute(DECIDED_CHECK)
    op.execute(
        "CREATE CONSTRAINT TRIGGER orders_decided AFTER INSERT ON orders "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION atlas_order_decided()"
    )


def _drop_guards() -> None:
    op.execute("DROP TRIGGER IF EXISTS orders_decided ON orders")
    op.execute("DROP FUNCTION IF EXISTS atlas_order_decided()")
    for trigger, table in (
        ("orders_insert_guard", "orders"),
        ("orders_update_guard", "orders"),
        ("order_events_apply", "order_events"),
        ("order_fills_guard", "order_fills"),
        ("paper_broker_orders_guard", "paper_broker_orders"),
        ("paper_broker_events_guard", "paper_broker_events"),
        ("outbox_events_update_guard", "outbox_events"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for name in GUARD_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    op.execute("DROP FUNCTION IF EXISTS atlas_order_transition_allowed(text, text, text)")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for table in APPEND_ONLY_TABLES + NO_DELETE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    for table in NO_DELETE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("topic", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DELIVERED",
                "DEAD",
                name="outbox_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'DEAD') = (dead_at IS NOT NULL)", name=op.f("ck_outbox_events_dead_at")
        ),
        sa.CheckConstraint(
            "(status = 'DELIVERED') = (delivered_at IS NOT NULL)",
            name=op.f("ck_outbox_events_delivered_at"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_events_attempts_non_negative")),
        sa.CheckConstraint("length(btrim(topic)) >= 3", name=op.f("ck_outbox_events_topic_given")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_outbox_events_dedupe_key")),
    )
    op.create_index(
        op.f("ix_outbox_events_aggregate_type_aggregate_id"),
        "outbox_events",
        ["aggregate_type", "aggregate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_outbox_events_status_available_at"),
        "outbox_events",
        ["status", "available_at"],
        unique=False,
    )
    op.create_table(
        "paper_broker_state",
        sa.Column("broker_id", sa.String(length=64), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "PAUSED",
                "STOPPED",
                name="paper_broker_run_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("fill_model", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_recovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "broker_id ~ '^atlas-paper-[a-z0-9-]+$'",
            name=op.f("ck_paper_broker_state_paper_broker_id"),
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_paper_broker_state_always_simulated")),
        sa.PrimaryKeyConstraint("broker_id", name=op.f("pk_paper_broker_state")),
    )
    op.create_table(
        "execution_requests",
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "ENTRY",
                "EXIT",
                name="execution_request_purpose",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "ORDER_CREATED",
                "REFUSED",
                name="execution_request_decision",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "reasons", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "decision <> 'REFUSED' OR jsonb_array_length(reasons) > 0",
            name=op.f("ck_execution_requests_refusal_explained"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:@/-]{8,160}$'",
            name=op.f("ck_execution_requests_key_format"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_execution_requests_request_sha256_format"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_execution_requests_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_execution_requests_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_execution_requests_idempotency_key")),
    )
    op.create_index(
        op.f("ix_execution_requests_decision_requested_at"),
        "execution_requests",
        ["decision", "requested_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_requests_portfolio_account_id_requested_at"),
        "execution_requests",
        ["portfolio_account_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_requests_reservation_id"),
        "execution_requests",
        ["reservation_id"],
        unique=False,
    )
    op.create_table(
        "orders",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("execution_request_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("instrument_spec_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity_increment", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price_increment", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "purpose",
            sa.Enum(
                "ENTRY", "EXIT", name="order_purpose", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column(
            "side",
            sa.Enum(
                "BUY",
                "SELL",
                "SELL_SHORT",
                "BUY_TO_COVER",
                name="order_side",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "direction",
            sa.Enum(
                "LONG",
                "SHORT",
                name="order_position_direction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "order_type",
            sa.Enum(
                "MARKET",
                "LIMIT",
                "STOP",
                "STOP_LIMIT",
                "TAKE_PROFIT",
                "TRAILING_STOP",
                name="order_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("trail_amount", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("trail_percent", sa.Numeric(precision=9, scale=4), nullable=True),
        sa.Column("protection_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PROPOSED",
                "APPROVED",
                "OPEN",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCEL_PENDING",
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="order_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "filled_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column("remaining_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("average_fill_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "filled_value", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("risk_assessment_id", sa.UUID(), nullable=False),
        sa.Column("parent_order_id", sa.UUID(), nullable=True),
        sa.Column("strategy_proposal_id", sa.UUID(), nullable=True),
        sa.Column("ai_decision_id", sa.UUID(), nullable=True),
        sa.Column("broker_order_id", sa.UUID(), nullable=True),
        sa.Column("applied_broker_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("last_event_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(NOT (status IN ('FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'FAILED'))) = (closed_at IS NULL)",
            name=op.f("ck_orders_closed_iff_final"),
        ),
        sa.CheckConstraint(
            "(limit_price IS NOT NULL) = (order_type IN ('LIMIT', 'STOP_LIMIT', 'TAKE_PROFIT'))",
            name=op.f("ck_orders_limit_price_matches_type"),
        ),
        sa.CheckConstraint(
            "(order_type = 'TRAILING_STOP') = ((trail_amount IS NOT NULL) <> (trail_percent IS NOT NULL))",
            name=op.f("ck_orders_trailing_has_one_offset"),
        ),
        sa.CheckConstraint(
            "(purpose = 'ENTRY' AND parent_order_id IS NULL AND protection_price IS NOT NULL AND ((direction = 'LONG' AND side = 'BUY')   OR (direction = 'SHORT' AND side = 'SELL_SHORT'))) OR (purpose = 'EXIT' AND parent_order_id IS NOT NULL AND ((direction = 'LONG' AND side = 'SELL')   OR (direction = 'SHORT' AND side = 'BUY_TO_COVER')))",
            name=op.f("ck_orders_purpose_side_direction"),
        ),
        sa.CheckConstraint(
            "(stop_price IS NOT NULL) = (order_type IN ('STOP', 'STOP_LIMIT'))",
            name=op.f("ck_orders_stop_price_matches_type"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:@/-]{8,160}$'", name=op.f("ck_orders_key_format")
        ),
        sa.CheckConstraint(
            "order_type <> 'TAKE_PROFIT' OR purpose = 'EXIT'",
            name=op.f("ck_orders_take_profit_is_exit"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_orders_request_sha256_format")
        ),
        sa.CheckConstraint(
            "status <> 'FILLED' OR remaining_quantity = 0",
            name=op.f("ck_orders_filled_is_complete"),
        ),
        sa.CheckConstraint(
            "status <> 'PARTIALLY_FILLED' OR (filled_quantity > 0 AND remaining_quantity > 0)",
            name=op.f("ck_orders_partial_is_partial"),
        ),
        sa.CheckConstraint(
            "(filled_quantity = 0) = (average_fill_price IS NULL)",
            name=op.f("ck_orders_average_iff_filled"),
        ),
        sa.CheckConstraint(
            "(limit_price IS NULL OR limit_price > 0) AND (stop_price IS NULL OR stop_price > 0) AND (trail_amount IS NULL OR trail_amount > 0) AND (trail_percent IS NULL OR (trail_percent > 0 AND trail_percent < 100)) AND (protection_price IS NULL OR protection_price > 0)",
            name=op.f("ck_orders_prices_positive"),
        ),
        sa.CheckConstraint(
            "applied_broker_sequence >= 0", name=op.f("ck_orders_broker_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "expires_at > submitted_at", name=op.f("ck_orders_expires_after_submission")
        ),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND remaining_quantity >= 0 AND filled_quantity + remaining_quantity = quantity",
            name=op.f("ck_orders_quantity_conservation"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0", name=op.f("ck_orders_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "price_increment IS NULL OR price_increment > 0", name=op.f("ck_orders_tick_positive")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_orders_quantity_positive")),
        sa.CheckConstraint("quantity_increment > 0", name=op.f("ck_orders_increment_positive")),
        sa.ForeignKeyConstraint(
            ["ai_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_orders_ai_decision_id_ai_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name=op.f("fk_orders_asset_id_assets"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["execution_request_id"],
            ["execution_requests.id"],
            name=op.f("fk_orders_execution_request_id_execution_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_spec_id"],
            ["instrument_specs.id"],
            name=op.f("fk_orders_instrument_spec_id_instrument_specs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_order_id"],
            ["orders.id"],
            name=op.f("fk_orders_parent_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_orders_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_currency"],
            ["currencies.code"],
            name=op.f("fk_orders_quote_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_orders_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["risk_assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_orders_risk_assessment_id_risk_assessments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_proposal_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_orders_strategy_proposal_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("execution_request_id", name=op.f("uq_orders_execution_request_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_orders_idempotency_key")),
    )
    op.create_index(
        op.f("ix_orders_asset_id_status"), "orders", ["asset_id", "status"], unique=False
    )
    op.create_index(op.f("ix_orders_parent_order_id"), "orders", ["parent_order_id"], unique=False)
    op.create_index(
        op.f("ix_orders_portfolio_account_id_status"),
        "orders",
        ["portfolio_account_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_orders_portfolio_account_id_submitted_at"),
        "orders",
        ["portfolio_account_id", "submitted_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_orders_status_expires_at"), "orders", ["status", "expires_at"], unique=False
    )
    op.create_index(
        "uq_orders_entry_reservation",
        "orders",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("purpose = 'ENTRY'"),
    )
    op.create_table(
        "order_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column(
            "command",
            sa.Enum("CANCEL", name="order_command", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "result_status",
            sa.Enum(
                "PROPOSED",
                "APPROVED",
                "OPEN",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCEL_PENDING",
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="order_request_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:@/-]{8,160}$'",
            name=op.f("ck_order_requests_key_format"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_order_requests_request_sha256_format"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_requests_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_order_requests_idempotency_key")),
    )
    op.create_index(
        op.f("ix_order_requests_order_id"), "order_requests", ["order_id"], unique=False
    )
    op.create_table(
        "paper_broker_orders",
        sa.Column("broker_id", sa.String(length=64), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("client_order_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column(
            "side",
            sa.Enum(
                "BUY",
                "SELL",
                "SELL_SHORT",
                "BUY_TO_COVER",
                name="paper_broker_order_side",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "order_type",
            sa.Enum(
                "MARKET",
                "LIMIT",
                "STOP",
                "STOP_LIMIT",
                "TAKE_PROFIT",
                "TRAILING_STOP",
                name="paper_broker_order_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "filled_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column("limit_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "WORKING",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
                name="paper_broker_order_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "status <> 'FILLED' OR filled_quantity = quantity",
            name=op.f("ck_paper_broker_orders_filled_complete"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0", name=op.f("ck_paper_broker_orders_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
            name=op.f("ck_paper_broker_orders_quantities_valid"),
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_paper_broker_orders_always_simulated")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_paper_broker_orders_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["broker_id"],
            ["paper_broker_state.broker_id"],
            name=op.f("fk_paper_broker_orders_broker_id_paper_broker_state"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["orders.id"],
            name=op.f("fk_paper_broker_orders_client_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_paper_broker_orders_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_broker_orders")),
        sa.UniqueConstraint("client_order_id", name=op.f("uq_paper_broker_orders_client_order_id")),
    )
    op.create_index(
        op.f("ix_paper_broker_orders_status_expires_at"),
        "paper_broker_orders",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "paper_broker_events",
        sa.Column("broker_id", sa.String(length=64), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("broker_order_id", sa.UUID(), nullable=False),
        sa.Column("client_order_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "ACCEPTED",
                "REJECTED",
                "FILL",
                "CANCEL_CONFIRMED",
                "CANCEL_REJECTED",
                "EXPIRED",
                name="paper_broker_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "SYSTEM",
                "CONTROLLED_TEST",
                name="paper_broker_event_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "quantity", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column("price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "commission", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("fees", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False),
        sa.Column("external_ref", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "event_type <> 'FILL' OR (source = 'CONTROLLED_TEST' AND quantity > 0 AND price > 0)",
            name=op.f("ck_paper_broker_events_fills_are_controlled_tests"),
        ),
        sa.CheckConstraint(
            "event_type = 'FILL' OR (quantity = 0 AND price IS NULL)",
            name=op.f("ck_paper_broker_events_only_fills_move"),
        ),
        sa.CheckConstraint(
            "source <> 'CONTROLLED_TEST' OR external_ref IS NOT NULL",
            name=op.f("ck_paper_broker_events_test_event_keyed"),
        ),
        sa.CheckConstraint(
            "commission >= 0 AND fees >= 0", name=op.f("ck_paper_broker_events_costs_non_negative")
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_paper_broker_events_reason_given")
        ),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_paper_broker_events_sequence_positive")),
        sa.CheckConstraint("simulated", name=op.f("ck_paper_broker_events_always_simulated")),
        sa.ForeignKeyConstraint(
            ["broker_id"],
            ["paper_broker_state.broker_id"],
            name=op.f("fk_paper_broker_events_broker_id_paper_broker_state"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["broker_order_id"],
            ["paper_broker_orders.id"],
            name=op.f("fk_paper_broker_events_broker_order_id_paper_broker_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["client_order_id"],
            ["orders.id"],
            name=op.f("fk_paper_broker_events_client_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_broker_events")),
        sa.UniqueConstraint(
            "broker_order_id",
            "sequence",
            name=op.f("uq_paper_broker_events_broker_order_id_sequence"),
        ),
        sa.UniqueConstraint("external_ref", name=op.f("uq_paper_broker_events_external_ref")),
    )
    op.create_index(
        op.f("ix_paper_broker_events_client_order_id"),
        "paper_broker_events",
        ["client_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_paper_broker_events_occurred_at"),
        "paper_broker_events",
        ["occurred_at"],
        unique=False,
    )
    op.create_table(
        "order_fills",
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("broker_event_id", sa.UUID(), nullable=False),
        sa.Column("journal_entry_id", sa.UUID(), nullable=False),
        sa.Column("reservation_event_id", sa.BigInteger(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "side",
            sa.Enum(
                "BUY",
                "SELL",
                "SELL_SHORT",
                "BUY_TO_COVER",
                name="order_fill_side",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column(
            "commission", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("fees", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "SYSTEM",
                "CONTROLLED_TEST",
                name="order_fill_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "source = 'CONTROLLED_TEST'", name=op.f("ck_order_fills_controlled_test_only")
        ),
        sa.CheckConstraint(
            "commission >= 0 AND fees >= 0", name=op.f("ck_order_fills_costs_non_negative")
        ),
        sa.CheckConstraint("quantity > 0 AND price > 0", name=op.f("ck_order_fills_fill_positive")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_order_fills_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_order_fills_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["broker_event_id"],
            ["paper_broker_events.id"],
            name=op.f("fk_order_fills_broker_event_id_paper_broker_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_order_fills_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_order_fills_journal_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_fills_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_order_fills_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_event_id"],
            ["risk_reservation_events.id"],
            name=op.f("fk_order_fills_reservation_event_id_risk_reservation_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_fills")),
        sa.UniqueConstraint("broker_event_id", name=op.f("uq_order_fills_broker_event_id")),
        sa.UniqueConstraint("journal_entry_id", name=op.f("uq_order_fills_journal_entry_id")),
        sa.UniqueConstraint("order_id", "sequence", name=op.f("uq_order_fills_order_id_sequence")),
    )
    op.create_index(
        op.f("ix_order_fills_portfolio_account_id_filled_at"),
        "order_fills",
        ["portfolio_account_id", "filled_at"],
        unique=False,
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "CREATED",
                "APPROVED",
                "REJECTED",
                "ACCEPTED",
                "FILL",
                "CANCEL_REQUESTED",
                "CANCEL_CONFIRMED",
                "CANCEL_REJECTED",
                "EXPIRED",
                "FAILED",
                name="order_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "from_status",
            sa.Enum(
                "PROPOSED",
                "APPROVED",
                "OPEN",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCEL_PENDING",
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="order_event_from_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "PROPOSED",
                "APPROVED",
                "OPEN",
                "PARTIALLY_FILLED",
                "FILLED",
                "CANCEL_PENDING",
                "CANCELLED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="order_event_to_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "quantity", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column("price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("fill_id", sa.UUID(), nullable=True),
        sa.Column("broker_event_id", sa.UUID(), nullable=True),
        sa.Column("broker_sequence", sa.Integer(), nullable=True),
        sa.Column(
            "source",
            sa.Enum(
                "EXECUTION_ENGINE",
                "PAPER_BROKER",
                "OPERATOR",
                "EXPIRY",
                "RECOVERY",
                name="order_event_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_txid",
            sa.BigInteger(),
            server_default=sa.text("txid_current()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event_type = 'FILL') = (fill_id IS NOT NULL) AND (event_type = 'FILL') = (quantity > 0) AND (event_type = 'FILL') = (price IS NOT NULL)",
            name=op.f("ck_order_events_fill_fields_match_type"),
        ),
        sa.CheckConstraint(
            "(broker_event_id IS NULL) = (broker_sequence IS NULL)",
            name=op.f("ck_order_events_broker_ref_complete"),
        ),
        sa.CheckConstraint("length(btrim(actor)) >= 1", name=op.f("ck_order_events_actor_given")),
        sa.CheckConstraint("length(btrim(reason)) >= 3", name=op.f("ck_order_events_reason_given")),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_order_events_quantity_non_negative")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_order_events_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["broker_event_id"],
            ["paper_broker_events.id"],
            name=op.f("fk_order_events_broker_event_id_paper_broker_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fill_id"],
            ["order_fills.id"],
            name=op.f("fk_order_events_fill_id_order_fills"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_events_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_events")),
        sa.UniqueConstraint("broker_event_id", name=op.f("uq_order_events_broker_event_id")),
        sa.UniqueConstraint("fill_id", name=op.f("uq_order_events_fill_id")),
        sa.UniqueConstraint("order_id", "sequence", name=op.f("uq_order_events_order_id_sequence")),
    )
    op.create_index(
        op.f("ix_order_events_event_type_occurred_at"),
        "order_events",
        ["event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_events_occurred_at"), "order_events", ["occurred_at"], unique=False
    )
    # ### end Alembic commands ###
    _guards()


def downgrade() -> None:
    _drop_guards()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_order_events_occurred_at"), table_name="order_events")
    op.drop_index(op.f("ix_order_events_event_type_occurred_at"), table_name="order_events")
    op.drop_table("order_events")
    op.drop_index(op.f("ix_order_fills_portfolio_account_id_filled_at"), table_name="order_fills")
    op.drop_table("order_fills")
    op.drop_index(op.f("ix_paper_broker_events_occurred_at"), table_name="paper_broker_events")
    op.drop_index(op.f("ix_paper_broker_events_client_order_id"), table_name="paper_broker_events")
    op.drop_table("paper_broker_events")
    op.drop_index(
        op.f("ix_paper_broker_orders_status_expires_at"), table_name="paper_broker_orders"
    )
    op.drop_table("paper_broker_orders")
    op.drop_index(op.f("ix_order_requests_order_id"), table_name="order_requests")
    op.drop_table("order_requests")
    op.drop_index(
        "uq_orders_entry_reservation",
        table_name="orders",
        postgresql_where=sa.text("purpose = 'ENTRY'"),
    )
    op.drop_index(op.f("ix_orders_status_expires_at"), table_name="orders")
    op.drop_index(op.f("ix_orders_portfolio_account_id_submitted_at"), table_name="orders")
    op.drop_index(op.f("ix_orders_portfolio_account_id_status"), table_name="orders")
    op.drop_index(op.f("ix_orders_parent_order_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_asset_id_status"), table_name="orders")
    op.drop_table("orders")
    op.drop_index(op.f("ix_execution_requests_reservation_id"), table_name="execution_requests")
    op.drop_index(
        op.f("ix_execution_requests_portfolio_account_id_requested_at"),
        table_name="execution_requests",
    )
    op.drop_index(
        op.f("ix_execution_requests_decision_requested_at"), table_name="execution_requests"
    )
    op.drop_table("execution_requests")
    op.drop_table("paper_broker_state")
    op.drop_index(op.f("ix_outbox_events_status_available_at"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_aggregate_type_aggregate_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
    # ### end Alembic commands ###
