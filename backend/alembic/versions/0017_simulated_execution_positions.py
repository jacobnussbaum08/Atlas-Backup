"""Phase 7B: simulated execution and automated position management.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-06

**Simulated execution only.** Fills are derived by the isolated paper broker from stored
market data under a recorded execution-model version; nothing reaches a brokerage.

* ``execution_models`` (immutable, content-addressed), ``execution_attempts`` and
  ``execution_fill_details`` (append-only): versioned assumptions, evaluations and the
  price/cost breakdown of every simulated fill.
* ``paper_positions`` (never deleted, guarded), ``position_events`` (append-only, gapless
  per position) and ``position_close_requests``.
* ``paper_broker_orders`` gains the simulation state (model version, trigger and trailing
  stop state, market-data cursors, costs so far); ``paper_broker_events`` the model version;
  ``orders`` the exit reason.
* New enum values: journal source SIMULATED_EXECUTION (6A posting by the paper execution
  engine), broker event source SIMULATION, event type TRIGGERED (order and broker). FILL
  events and fills accept SIMULATION besides CONTROLLED_TEST; a SIMULATION event must name
  its execution model; TRIGGERED events are SIMULATION only.
* The order state machine gains the status-preserving TRIGGERED transitions (the function
  ``atlas_order_transition_allowed`` is replaced; ``app/execution/state_machine.py`` is the
  source and ``test_order_state_machine`` compares the two).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "execution_models",
    "execution_attempts",
    "execution_fill_details",
    "position_events",
)
NO_DELETE_TABLES = ("paper_positions", "position_close_requests")
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
    ("OPEN", "TRIGGERED", "OPEN"),
    ("CANCEL_PENDING", "TRIGGERED", "CANCEL_PENDING"),
)
_values = ",\n            ".join(
    f"({'NULL::text' if f is None else repr(f)}, '{e}', '{t}')" for f, e, t in TRANSITIONS
)
TRANSITION_FUNCTION = f"""
CREATE OR REPLACE FUNCTION atlas_order_transition_allowed(from_status text, event_type text,
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


def _enum_check(values: Sequence[str], column: str) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({listed})"


# (table, constraint, condition before, condition after): enum value lists widened by 7B
# and the 7A fill-source rules relaxed to admit SIMULATION.
JOURNAL_SOURCES_7A = ("SYSTEM", "CONTROLLED_TEST", "OPERATOR")
ORDER_EVENTS_7A = (
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
)
BROKER_EVENTS_7A = (
    "ACCEPTED",
    "REJECTED",
    "FILL",
    "CANCEL_CONFIRMED",
    "CANCEL_REJECTED",
    "EXPIRED",
)
BROKER_SOURCES_7A = ("SYSTEM", "CONTROLLED_TEST")
CHECKS = (
    (
        "journal_entries",
        "ck_journal_entries_journal_entry_source",
        _enum_check(JOURNAL_SOURCES_7A, "source"),
        _enum_check((*JOURNAL_SOURCES_7A, "SIMULATED_EXECUTION"), "source"),
    ),
    (
        "order_events",
        "ck_order_events_order_event_type",
        _enum_check(ORDER_EVENTS_7A, "event_type"),
        _enum_check((*ORDER_EVENTS_7A, "TRIGGERED"), "event_type"),
    ),
    (
        "paper_broker_events",
        "ck_paper_broker_events_paper_broker_event_type",
        _enum_check(BROKER_EVENTS_7A, "event_type"),
        _enum_check((*BROKER_EVENTS_7A, "TRIGGERED"), "event_type"),
    ),
    (
        "paper_broker_events",
        "ck_paper_broker_events_paper_broker_event_source",
        _enum_check(BROKER_SOURCES_7A, "source"),
        _enum_check((*BROKER_SOURCES_7A, "SIMULATION"), "source"),
    ),
    (
        "order_fills",
        "ck_order_fills_order_fill_source",
        _enum_check(BROKER_SOURCES_7A, "source"),
        _enum_check((*BROKER_SOURCES_7A, "SIMULATION"), "source"),
    ),
)
# (table, 7A constraint dropped, 7B constraints added as (name, condition)).
RULES = (
    (
        "order_fills",
        ("ck_order_fills_controlled_test_only", "source = 'CONTROLLED_TEST'"),
        (("ck_order_fills_fill_source", "source IN ('CONTROLLED_TEST', 'SIMULATION')"),),
    ),
    (
        "paper_broker_events",
        (
            "ck_paper_broker_events_fills_are_controlled_tests",
            "event_type <> 'FILL' OR (source = 'CONTROLLED_TEST' AND quantity > 0 AND price > 0)",
        ),
        (
            (
                "ck_paper_broker_events_fills_are_tests_or_simulated",
                "event_type <> 'FILL' OR (source IN ('CONTROLLED_TEST', 'SIMULATION') "
                "AND quantity > 0 AND price > 0)",
            ),
            (
                "ck_paper_broker_events_simulation_has_model",
                "source <> 'SIMULATION' OR execution_model_id IS NOT NULL",
            ),
            (
                "ck_paper_broker_events_triggers_are_simulated",
                "event_type <> 'TRIGGERED' OR source = 'SIMULATION'",
            ),
        ),
    ),
    (
        "orders",
        None,
        (("ck_orders_exit_reason_for_exits", "exit_reason IS NULL OR purpose = 'EXIT'"),),
    ),
)

# Columns that never change after the insert (0016's lists plus the 7B additions).
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
    "exit_reason",
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
# Set once (NULL -> value), never changed afterwards.
BOOK_SET_ONCE = ("execution_model_id", "arrival_price", "triggered_at", "trigger_price")
POSITION_IMMUTABLE = (
    "id",
    "portfolio_account_id",
    "simulated",
    "asset_id",
    "symbol",
    "asset_class",
    "currency",
    "direction",
    "entry_order_id",
    "reservation_id",
    "strategy_proposal_id",
    "ai_decision_id",
    "strategy_id",
    "origin",
    "quantity_increment",
    "price_increment",
    "initial_stop_price",
    "opened_at",
    "created_at",
)
CLOSE_REQUEST_IMMUTABLE = (
    "id",
    "position_id",
    "idempotency_key",
    "requested_by",
    "reason",
    "requested_at",
    "created_at",
)


def _changed(columns: Sequence[str]) -> str:
    return " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in columns)


def _reset(columns: Sequence[str]) -> str:
    return " OR ".join(
        f"(OLD.{c} IS NOT NULL AND NEW.{c} IS DISTINCT FROM OLD.{c})" for c in columns
    )


# Built from constant column names only (no input reaches this SQL).
GUARD_FUNCTIONS = {
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
    "atlas_paper_broker_order_guard": f"""
    BEGIN
        IF {_changed(BOOK_IMMUTABLE)} THEN
            RAISE EXCEPTION 'immutable paper broker order fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF {_reset(BOOK_SET_ONCE)} THEN
            RAISE EXCEPTION 'the execution model, arrival price and stop trigger of a paper '
                'broker order are recorded once' USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.filled_quantity < OLD.filled_quantity
           OR NEW.commission_charged < OLD.commission_charged
           OR NEW.evaluations < OLD.evaluations
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
    # 0016's state machine trigger, with TRIGGERED among the broker-only events.
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
        ELSIF NEW.event_type IN ('ACCEPTED', 'FILL', 'CANCEL_CONFIRMED', 'CANCEL_REJECTED',
                                 'TRIGGERED') THEN
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
    # A simulated fill's breakdown equals its paper broker SIMULATION FILL event.
    "atlas_execution_fill_detail_guard": """
    DECLARE
        b record;
        bk record;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT client_order_id, broker_order_id, event_type, source, quantity, price,
               commission, fees, execution_model_id, occurred_at
            INTO b FROM paper_broker_events WHERE id = NEW.broker_event_id;
        SELECT portfolio_account_id, execution_model_id INTO bk
            FROM paper_broker_orders WHERE id = b.broker_order_id;
        IF b.event_type <> 'FILL' OR b.source <> 'SIMULATION' OR b.client_order_id <> NEW.order_id
           OR b.quantity <> NEW.quantity OR b.price <> NEW.price
           OR b.commission <> NEW.commission
           OR b.fees <> NEW.regulatory_fees + NEW.exchange_fees
           OR b.execution_model_id <> NEW.execution_model_id
           OR bk.execution_model_id IS DISTINCT FROM NEW.execution_model_id
           OR bk.portfolio_account_id <> NEW.portfolio_account_id
           OR b.occurred_at <> NEW.filled_at THEN
            RAISE EXCEPTION 'a fill breakdown must equal its simulated paper broker FILL event'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_execution_attempt_guard": """
    DECLARE
        b record;
        bk record;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT client_order_id INTO bk FROM paper_broker_orders WHERE id = NEW.broker_order_id;
        IF bk.client_order_id IS DISTINCT FROM NEW.order_id THEN
            RAISE EXCEPTION 'an execution attempt belongs to its order''s book row'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.fill_event_id IS NOT NULL THEN
            SELECT broker_order_id, event_type INTO b
                FROM paper_broker_events WHERE id = NEW.fill_event_id;
            IF b.broker_order_id <> NEW.broker_order_id OR b.event_type <> 'FILL' THEN
                RAISE EXCEPTION 'an attempt''s fill is a FILL event of the same book row'
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_paper_position_insert_guard": """
    DECLARE
        o record;
    BEGIN
        SELECT purpose, portfolio_account_id, asset_id, direction, reservation_id
            INTO o FROM orders WHERE id = NEW.entry_order_id;
        IF o.purpose IS DISTINCT FROM 'ENTRY' OR o.portfolio_account_id <> NEW.portfolio_account_id
           OR o.asset_id <> NEW.asset_id OR o.direction <> NEW.direction
           OR o.reservation_id <> NEW.reservation_id THEN
            RAISE EXCEPTION 'a position is the trade of its own ENTRY order'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.status <> 'OPEN' OR NEW.quantity_closed <> 0 OR NEW.last_event_sequence <> 0
           OR NEW.stop_price <> NEW.initial_stop_price THEN
            RAISE EXCEPTION 'a position is inserted OPEN, unreduced, at its approved stop'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_paper_position_update_guard": f"""
    BEGIN
        IF {_changed(POSITION_IMMUTABLE)} THEN
            RAISE EXCEPTION 'immutable position fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status = 'CLOSED' AND (NEW.status <> 'CLOSED'
           OR NEW.open_quantity <> OLD.open_quantity
           OR NEW.quantity_opened <> OLD.quantity_opened
           OR NEW.realized_pnl <> OLD.realized_pnl) THEN
            RAISE EXCEPTION 'position % is closed', OLD.id USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.quantity_opened < OLD.quantity_opened OR NEW.quantity_closed < OLD.quantity_closed
           OR NEW.commissions < OLD.commissions OR NEW.fees < OLD.fees
           OR NEW.borrow_accrued < OLD.borrow_accrued OR NEW.borrow_paid < OLD.borrow_paid
           OR NEW.exits_submitted < OLD.exits_submitted
           OR NEW.last_event_sequence NOT IN (OLD.last_event_sequence,
                                              OLD.last_event_sequence + 1) THEN
            RAISE EXCEPTION 'a position''s quantities, costs and history only move forward'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF (NEW.direction = 'LONG' AND NEW.stop_price < OLD.stop_price)
           OR (NEW.direction = 'SHORT' AND NEW.stop_price > OLD.stop_price)
           OR (NEW.direction = 'LONG' AND NEW.trailing_stop_price < OLD.trailing_stop_price)
           OR (NEW.direction = 'SHORT' AND NEW.trailing_stop_price > OLD.trailing_stop_price)
           OR (OLD.trailing_stop_price IS NOT NULL AND NEW.trailing_stop_price IS NULL) THEN
            RAISE EXCEPTION 'a protective stop only tightens' USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.trigger_reason IS NOT NULL AND NEW.trigger_reason IS DISTINCT FROM OLD.trigger_reason
           AND NOT (OLD.trigger_reason = 'TAKE_PROFIT') THEN
            RAISE EXCEPTION 'an exit trigger stays until the position is closed'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
    "atlas_position_event_guard": """
    DECLARE
        p record;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT last_event_sequence, open_quantity INTO p
            FROM paper_positions WHERE id = NEW.position_id;
        IF NEW.sequence <> p.last_event_sequence THEN
            RAISE EXCEPTION 'position events are numbered by their position without gaps'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.open_quantity_after <> p.open_quantity THEN
            RAISE EXCEPTION 'a position event records the position''s open quantity'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_close_request_guard": f"""
    BEGIN
        IF {_changed(CLOSE_REQUEST_IMMUTABLE)} THEN
            RAISE EXCEPTION 'immutable close request fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF OLD.status IN ('COMPLETED', 'REFUSED') AND NEW.status <> OLD.status THEN
            RAISE EXCEPTION 'close request % is final', OLD.id
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
}
# 7B triggers: (name, timing, table, function).
TRIGGERS = (
    (
        "execution_fill_details_guard",
        "BEFORE INSERT",
        "execution_fill_details",
        "atlas_execution_fill_detail_guard",
    ),
    (
        "execution_attempts_guard",
        "BEFORE INSERT",
        "execution_attempts",
        "atlas_execution_attempt_guard",
    ),
    (
        "paper_positions_insert_guard",
        "BEFORE INSERT",
        "paper_positions",
        "atlas_paper_position_insert_guard",
    ),
    (
        "paper_positions_update_guard",
        "BEFORE UPDATE",
        "paper_positions",
        "atlas_paper_position_update_guard",
    ),
    ("position_events_guard", "BEFORE INSERT", "position_events", "atlas_position_event_guard"),
    (
        "position_close_requests_guard",
        "BEFORE UPDATE",
        "position_close_requests",
        "atlas_close_request_guard",
    ),
)
# Functions 0016 created that 0017 replaces (restored from 0016 on downgrade).
REPLACED = ("atlas_order_update_guard", "atlas_paper_broker_order_guard", "atlas_order_event_apply")
NEW_FUNCTIONS = tuple(name for name in GUARD_FUNCTIONS if name not in REPLACED)


def _constraints() -> None:
    op.execute("ALTER TABLE journal_entries ALTER COLUMN source TYPE varchar(19)")
    for table, name, _before, after in CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({after})")
    for table, dropped, added in RULES:
        if dropped is not None:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {dropped[0]}")
        for name, condition in added:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")


def _restore_constraints() -> None:
    for table, dropped, added in RULES:
        for name, _condition in added:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        if dropped is not None:
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {dropped[0]} CHECK ({dropped[1]})")
    for table, name, before, _after in CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({before})")
    op.execute("ALTER TABLE journal_entries ALTER COLUMN source TYPE varchar(15)")


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
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$;"
        )
    for trigger, timing, table, function in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} {timing} ON {table} FOR EACH ROW "
            f"EXECUTE FUNCTION {function}()"
        )


def _drop_guards() -> None:
    import importlib.util
    from pathlib import Path

    for trigger, _timing, table, _function in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for name in NEW_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for table in APPEND_ONLY_TABLES + NO_DELETE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    for table in NO_DELETE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")
    # Restore 0016's definitions of the replaced functions and of the transition table.
    path = Path(__file__).with_name("0016_paper_execution_engine.py")
    spec = importlib.util.spec_from_file_location("migration_0016", path)
    if spec is None or spec.loader is None:  # pragma: no cover  # shipped file
        raise RuntimeError("migration 0016 is missing")
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    op.execute(
        previous.TRANSITION_FUNCTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
    )
    for name in REPLACED:
        op.execute(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql "
            f"AS $${previous.GUARD_FUNCTIONS[name]}$$;"
        )


def upgrade() -> None:
    op.create_table(
        "execution_models",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "definition_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_execution_models_sha_format")
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_execution_models_always_simulated")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_execution_models_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_models")),
        sa.UniqueConstraint("code", "version", name=op.f("uq_execution_models_code_version")),
        sa.UniqueConstraint(
            "definition_sha256", name=op.f("uq_execution_models_definition_sha256")
        ),
    )
    op.create_table(
        "paper_positions",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "LONG",
                "SHORT",
                name="paper_position_direction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN",
                "CLOSED",
                name="paper_position_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("entry_order_id", sa.UUID(), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("strategy_proposal_id", sa.UUID(), nullable=True),
        sa.Column("ai_decision_id", sa.UUID(), nullable=True),
        sa.Column("strategy_id", sa.String(length=128), nullable=True),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("quantity_increment", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price_increment", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("quantity_opened", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "quantity_closed",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column("open_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("entry_value", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column(
            "exit_value", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column("average_entry_price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("average_exit_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "realized_pnl", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "commissions", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("fees", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False),
        sa.Column(
            "borrow_accrued", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "borrow_paid", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("borrow_accrued_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initial_stop_price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("target_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("invalidation_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("trail_amount", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("trail_percent", sa.Numeric(precision=9, scale=4), nullable=True),
        sa.Column("trail_reference_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("trailing_stop_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "trigger_reason",
            sa.Enum(
                "STOP_LOSS",
                "TRAILING_STOP",
                "INVALIDATION",
                "EXPIRATION",
                "CLOSE_REQUEST",
                "TAKE_PROFIT",
                "MANUAL_EXIT",
                name="paper_position_trigger_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "exit_reason",
            sa.Enum(
                "STOP_LOSS",
                "TRAILING_STOP",
                "INVALIDATION",
                "EXPIRATION",
                "CLOSE_REQUEST",
                "TAKE_PROFIT",
                "MANUAL_EXIT",
                name="paper_position_exit_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("exits_submitted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("monitor_cursor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monitor_trade_id", sa.BigInteger(), nullable=True),
        sa.Column("monitor_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("monitor_status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column("last_mark_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("last_mark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_mark_source", sa.String(length=16), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(precision=28, scale=8), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
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
            "(direction = 'LONG' AND stop_price >= initial_stop_price  AND (target_price IS NULL OR target_price > stop_price)) OR (direction = 'SHORT' AND stop_price <= initial_stop_price  AND (target_price IS NULL OR target_price < stop_price))",
            name=op.f("ck_paper_positions_levels_ordered"),
        ),
        sa.CheckConstraint(
            "(status = 'CLOSED') = (closed_at IS NOT NULL)",
            name=op.f("ck_paper_positions_closed_at_iff_closed"),
        ),
        sa.CheckConstraint(
            "status <> 'CLOSED' OR open_quantity = 0",
            name=op.f("ck_paper_positions_closed_is_flat"),
        ),
        sa.CheckConstraint(
            "(quantity_closed = 0) = (average_exit_price IS NULL)",
            name=op.f("ck_paper_positions_exit_average_iff_exited"),
        ),
        sa.CheckConstraint(
            "(trail_amount IS NULL OR trail_amount > 0) AND (trail_percent IS NULL OR (trail_percent > 0 AND trail_percent < 100)) AND NOT (trail_amount IS NOT NULL AND trail_percent IS NOT NULL)",
            name=op.f("ck_paper_positions_trail_valid"),
        ),
        sa.CheckConstraint(
            "commissions >= 0 AND fees >= 0 AND borrow_accrued >= 0 AND borrow_paid >= 0 AND borrow_paid <= borrow_accrued",
            name=op.f("ck_paper_positions_costs_non_negative"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0", name=op.f("ck_paper_positions_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "quantity_opened > 0 AND quantity_closed >= 0 AND open_quantity >= 0 AND open_quantity = quantity_opened - quantity_closed",
            name=op.f("ck_paper_positions_quantity_conservation"),
        ),
        sa.CheckConstraint("simulated", name=op.f("ck_paper_positions_always_simulated")),
        sa.CheckConstraint(
            "stop_price > 0 AND initial_stop_price > 0 AND (target_price IS NULL OR target_price > 0) AND (invalidation_price IS NULL OR invalidation_price > 0) AND (trailing_stop_price IS NULL OR trailing_stop_price > 0)",
            name=op.f("ck_paper_positions_levels_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["ai_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_paper_positions_ai_decision_id_ai_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_paper_positions_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_paper_positions_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entry_order_id"],
            ["orders.id"],
            name=op.f("fk_paper_positions_entry_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_paper_positions_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_paper_positions_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_proposal_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_paper_positions_strategy_proposal_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper_positions")),
        sa.UniqueConstraint("entry_order_id", name=op.f("uq_paper_positions_entry_order_id")),
    )
    op.create_index(
        op.f("ix_paper_positions_asset_id_status"),
        "paper_positions",
        ["asset_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_paper_positions_portfolio_account_id_status"),
        "paper_positions",
        ["portfolio_account_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_paper_positions_status_expires_at"),
        "paper_positions",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "position_close_requests",
        sa.Column("position_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "EXIT_SUBMITTED",
                "COMPLETED",
                "REFUSED",
                name="position_close_request_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
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
            "(status IN ('COMPLETED', 'REFUSED')) = (resolved_at IS NOT NULL)",
            name=op.f("ck_position_close_requests_resolved_iff_final"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9._:@/-]{8,160}$'",
            name=op.f("ck_position_close_requests_key_format"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_position_close_requests_reason_given")
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["paper_positions.id"],
            name=op.f("fk_position_close_requests_position_id_paper_positions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_close_requests")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_position_close_requests_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_position_close_requests_position_id_status"),
        "position_close_requests",
        ["position_id", "status"],
        unique=False,
    )
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("broker_order_id", sa.UUID(), nullable=False),
        sa.Column("execution_model_id", sa.Integer(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "FILLED",
                "PARTIALLY_FILLED",
                "TRIGGERED",
                "NO_FILL",
                "DEFERRED",
                name="execution_attempt_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("triggered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("fill_event_id", sa.UUID(), nullable=True),
        sa.Column(
            "market", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
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
            "(outcome IN ('FILLED', 'PARTIALLY_FILLED')) = (fill_event_id IS NOT NULL)",
            name=op.f("ck_execution_attempts_fill_iff_filled"),
        ),
        sa.CheckConstraint(
            "length(btrim(code)) >= 2", name=op.f("ck_execution_attempts_code_given")
        ),
        sa.ForeignKeyConstraint(
            ["broker_order_id"],
            ["paper_broker_orders.id"],
            name=op.f("fk_execution_attempts_broker_order_id_paper_broker_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_model_id"],
            ["execution_models.id"],
            name=op.f("fk_execution_attempts_execution_model_id_execution_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["fill_event_id"],
            ["paper_broker_events.id"],
            name=op.f("fk_execution_attempts_fill_event_id_paper_broker_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_execution_attempts_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_execution_attempts")),
    )
    op.create_index(
        op.f("ix_execution_attempts_order_id_evaluated_at"),
        "execution_attempts",
        ["order_id", "evaluated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_attempts_outcome_evaluated_at"),
        "execution_attempts",
        ["outcome", "evaluated_at"],
        unique=False,
    )
    op.create_table(
        "execution_fill_details",
        sa.Column("broker_event_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("execution_model_id", sa.Integer(), nullable=False),
        sa.Column(
            "basis",
            sa.Enum(
                "QUOTE",
                "TRADE_THROUGH",
                "BAR_THROUGH",
                "BAR_FALLBACK",
                name="execution_data_basis",
                native_enum=False,
                create_constraint=True,
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
                name="execution_fill_side",
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
                name="execution_fill_order_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("touch_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("arrival_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("spread", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("slippage_bps", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column(
            "impact_bps", sa.Numeric(precision=18, scale=6), server_default="0", nullable=False
        ),
        sa.Column("shortfall_bps", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("liquidity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("participation", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column(
            "commission", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "regulatory_fees", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "exchange_fees", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("gap", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "assumptions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
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
            "commission >= 0 AND regulatory_fees >= 0 AND exchange_fees >= 0",
            name=op.f("ck_execution_fill_details_costs_non_negative"),
        ),
        sa.CheckConstraint(
            "participation IS NULL OR (participation > 0 AND participation <= 1)",
            name=op.f("ck_execution_fill_details_participation_range"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0", name=op.f("ck_execution_fill_details_fill_positive")
        ),
        sa.CheckConstraint(
            "reference_price IS NULL OR reference_price > 0",
            name=op.f("ck_execution_fill_details_reference_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["broker_event_id"],
            ["paper_broker_events.id"],
            name=op.f("fk_execution_fill_details_broker_event_id_paper_broker_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency"],
            ["currencies.code"],
            name=op.f("fk_execution_fill_details_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_model_id"],
            ["execution_models.id"],
            name=op.f("fk_execution_fill_details_execution_model_id_execution_models"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_execution_fill_details_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_execution_fill_details_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("broker_event_id", name=op.f("pk_execution_fill_details")),
    )
    op.create_index(
        op.f("ix_execution_fill_details_order_id"),
        "execution_fill_details",
        ["order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_fill_details_portfolio_account_id_filled_at"),
        "execution_fill_details",
        ["portfolio_account_id", "filled_at"],
        unique=False,
    )
    op.create_table(
        "position_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("position_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "OPENED",
                "INCREASED",
                "REDUCED",
                "CLOSED",
                "PROTECTION_SET",
                "PROTECTION_UPDATED",
                "TRIGGERED",
                "EXIT_SUBMITTED",
                "EXIT_ENDED",
                "ENTRY_CANCEL_REQUESTED",
                "CLOSE_REQUESTED",
                "BORROW_ACCRUED",
                "COSTS_SETTLED",
                "MONITOR_DEFERRED",
                name="position_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "quantity", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column("price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("open_quantity_after", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("fill_id", sa.UUID(), nullable=True),
        sa.Column("journal_entry_id", sa.UUID(), nullable=True),
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
            "length(btrim(actor)) >= 1", name=op.f("ck_position_events_actor_given")
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_position_events_reason_given")
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_position_events_quantity_non_negative")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_position_events_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["fill_id"],
            ["order_fills.id"],
            name=op.f("fk_position_events_fill_id_order_fills"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_position_events_journal_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_position_events_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["paper_positions.id"],
            name=op.f("fk_position_events_position_id_paper_positions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_events")),
        sa.UniqueConstraint(
            "position_id", "sequence", name=op.f("uq_position_events_position_id_sequence")
        ),
    )
    op.create_index(
        op.f("ix_position_events_event_type_occurred_at"),
        "position_events",
        ["event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_position_events_occurred_at"), "position_events", ["occurred_at"], unique=False
    )
    op.add_column(
        "orders",
        sa.Column(
            "exit_reason",
            sa.Enum(
                "STOP_LOSS",
                "TRAILING_STOP",
                "INVALIDATION",
                "EXPIRATION",
                "CLOSE_REQUEST",
                "TAKE_PROFIT",
                "MANUAL_EXIT",
                name="order_exit_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "paper_broker_events", sa.Column("execution_model_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_paper_broker_events_execution_model_id_execution_models"),
        "paper_broker_events",
        "execution_models",
        ["execution_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "paper_broker_orders", sa.Column("execution_model_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("arrival_price", sa.Numeric(precision=30, scale=10), nullable=True),
    )
    op.add_column(
        "paper_broker_orders", sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("trigger_price", sa.Numeric(precision=30, scale=10), nullable=True),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("trail_reference_price", sa.Numeric(precision=30, scale=10), nullable=True),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("current_stop_price", sa.Numeric(precision=30, scale=10), nullable=True),
    )
    op.add_column(
        "paper_broker_orders", sa.Column("quote_cursor_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("trade_cursor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "paper_broker_orders", sa.Column("trade_cursor_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "paper_broker_orders", sa.Column("bar_cursor_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("market_cursor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column(
            "commission_charged",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("evaluations", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "paper_broker_orders",
        sa.Column(
            "last_attempt_outcome",
            sa.Enum(
                "FILLED",
                "PARTIALLY_FILLED",
                "TRIGGERED",
                "NO_FILL",
                "DEFERRED",
                name="paper_broker_attempt_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "paper_broker_orders", sa.Column("last_attempt_code", sa.String(length=48), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_paper_broker_orders_execution_model_id_execution_models"),
        "paper_broker_orders",
        "execution_models",
        ["execution_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _constraints()
    _guards()


def downgrade() -> None:
    _drop_guards()
    _restore_constraints()
    op.drop_constraint(
        op.f("fk_paper_broker_orders_execution_model_id_execution_models"),
        "paper_broker_orders",
        type_="foreignkey",
    )
    op.drop_column("paper_broker_orders", "last_attempt_code")
    op.drop_column("paper_broker_orders", "last_attempt_outcome")
    op.drop_column("paper_broker_orders", "last_evaluated_at")
    op.drop_column("paper_broker_orders", "evaluations")
    op.drop_column("paper_broker_orders", "commission_charged")
    op.drop_column("paper_broker_orders", "market_cursor_at")
    op.drop_column("paper_broker_orders", "bar_cursor_at")
    op.drop_column("paper_broker_orders", "trade_cursor_id")
    op.drop_column("paper_broker_orders", "trade_cursor_at")
    op.drop_column("paper_broker_orders", "quote_cursor_id")
    op.drop_column("paper_broker_orders", "current_stop_price")
    op.drop_column("paper_broker_orders", "trail_reference_price")
    op.drop_column("paper_broker_orders", "trigger_price")
    op.drop_column("paper_broker_orders", "triggered_at")
    op.drop_column("paper_broker_orders", "arrival_price")
    op.drop_column("paper_broker_orders", "execution_model_id")
    op.drop_constraint(
        op.f("fk_paper_broker_events_execution_model_id_execution_models"),
        "paper_broker_events",
        type_="foreignkey",
    )
    op.drop_column("paper_broker_events", "execution_model_id")
    op.drop_column("orders", "exit_reason")
    op.drop_index(op.f("ix_position_events_occurred_at"), table_name="position_events")
    op.drop_index(op.f("ix_position_events_event_type_occurred_at"), table_name="position_events")
    op.drop_table("position_events")
    op.drop_index(
        op.f("ix_execution_fill_details_portfolio_account_id_filled_at"),
        table_name="execution_fill_details",
    )
    op.drop_index(op.f("ix_execution_fill_details_order_id"), table_name="execution_fill_details")
    op.drop_table("execution_fill_details")
    op.drop_index(
        op.f("ix_execution_attempts_outcome_evaluated_at"), table_name="execution_attempts"
    )
    op.drop_index(
        op.f("ix_execution_attempts_order_id_evaluated_at"), table_name="execution_attempts"
    )
    op.drop_table("execution_attempts")
    op.drop_index(
        op.f("ix_position_close_requests_position_id_status"), table_name="position_close_requests"
    )
    op.drop_table("position_close_requests")
    op.drop_index(op.f("ix_paper_positions_status_expires_at"), table_name="paper_positions")
    op.drop_index(
        op.f("ix_paper_positions_portfolio_account_id_status"), table_name="paper_positions"
    )
    op.drop_index(op.f("ix_paper_positions_asset_id_status"), table_name="paper_positions")
    op.drop_table("paper_positions")
    op.drop_table("execution_models")
