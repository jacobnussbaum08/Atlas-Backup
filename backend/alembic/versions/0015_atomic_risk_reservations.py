"""atomic risk reservations (Phase 6C)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-05 23:41:17.766331+00:00

Atomic risk reservations (decisions D-148 to D-155, docs/architecture/reservations.md).

* ``risk_reservations``: current-state projection. ``atlas_risk_reservation_insert_guard``
  inserts it PENDING and empty, decided by an assessment of the same transaction;
  ``atlas_risk_reservation_update_guard`` lets only the event trigger change it (and never
  its identity); the deferred ``atlas_risk_reservation_decided`` refuses to commit a
  reservation that is still PENDING, lacks its idempotency record, or was approved without
  an approval record.
* ``risk_reservation_events``: append-only; ``atlas_risk_reservation_apply`` is the state
  machine: gapless numbering per reservation and per account (a stale computation fails
  with SQLSTATE 40001), valid transitions, approvals equal to the assessment's permitted
  quantity/loss/notional/capital/margin, no approval while a breaker is tripped or the
  latest reservation reconciliation FAILED, consumption only against a ledger fill of the
  same asset, direction and quantity, no partial consumption freeing more than its
  proportion, expiry only when nothing depends on the reservation. It updates the
  projection and ``risk_reservation_states`` (active totals per account).
* ``risk_reservation_approvals``, ``risk_reservation_requests``,
  ``risk_reservation_reconciliations``, ``system_health_events``: append-only.
* 6B: ``risk_exposure_snapshots.pending_exposure`` may now be positive; ``risk_events``
  accepts the reservation event types.

Reservations post nothing to the ledger. Downgrading is possible only before any 6C row
exists (risk events and snapshots that record reservations are append-only).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = (
    "risk_reservation_events",
    "risk_reservation_requests",
    "risk_reservation_approvals",
    "risk_reservation_reconciliations",
    "system_health_events",
)
# Columns of a reservation that never change after it is inserted.
IMMUTABLE_COLUMNS = (
    "id",
    "portfolio_account_id",
    "idempotency_key",
    "request_sha256",
    "source",
    "strategy_proposal_id",
    "ai_decision_id",
    "assessment_id",
    "asset_id",
    "symbol",
    "asset_class",
    "direction",
    "quote_currency",
    "functional_currency",
    "entry",
    "stop",
    "target",
    "proposed_quantity",
    "expires_at",
    "requested_by",
    "requested_at",
    "created_txid",
    "created_at",
)
AMOUNTS = ("risk", "exposure", "capital", "margin")
_changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in IMMUTABLE_COLUMNS)
_zero_amounts = " OR ".join(f"NEW.approved_{a} <> 0 OR NEW.remaining_{a} <> 0" for a in AMOUNTS)

GUARD_FUNCTIONS = {
    # A reservation is inserted PENDING and empty; only its events change it afterwards.
    # Built from constant column names only (no input reaches this SQL).
    "atlas_risk_reservation_insert_guard": f"""
    DECLARE
        a record;
    BEGIN
        NEW.created_txid := txid_current();
        IF NEW.status <> 'PENDING' OR NEW.last_event_sequence <> 0
           OR NEW.approved_quantity <> 0 OR NEW.consumed_quantity <> 0
           OR NEW.released_quantity <> 0 OR NEW.remaining_quantity <> 0
           OR {_zero_amounts}
           OR NEW.execution_link <> 'NONE' OR NEW.order_ref IS NOT NULL
           OR NEW.expiry_deferred_at IS NOT NULL THEN
            RAISE EXCEPTION 'a risk reservation is inserted PENDING and empty; its events '
                'change it' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.assessment_id IS NOT NULL THEN
            SELECT portfolio_account_id, asset_id, direction, created_txid INTO a
                FROM risk_assessments WHERE id = NEW.assessment_id;
            IF a.created_txid IS DISTINCT FROM txid_current() THEN
                RAISE EXCEPTION 'a reservation must be decided by an assessment recorded in '
                    'its own transaction' USING ERRCODE = 'check_violation';
            END IF;
            IF a.portfolio_account_id <> NEW.portfolio_account_id OR a.asset_id <> NEW.asset_id
               OR a.direction <> NEW.direction THEN
                RAISE EXCEPTION 'assessment % is for another account, asset or direction',
                    NEW.assessment_id USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    """,  # noqa: S608
    "atlas_risk_reservation_update_guard": f"""
    BEGIN
        IF pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION 'risk_reservations is a projection: it changes only through '
                'risk_reservation_events' USING ERRCODE = 'restrict_violation';
        END IF;
        IF {_changed} THEN
            RAISE EXCEPTION 'immutable reservation fields cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
    "atlas_risk_reservation_state_guard": """
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.last_sequence <> 0 OR NEW.active_count <> 0 OR NEW.reserved_risk <> 0
               OR NEW.reserved_exposure <> 0 OR NEW.reserved_long_exposure <> 0
               OR NEW.reserved_short_exposure <> 0 OR NEW.reserved_capital <> 0
               OR NEW.reserved_margin <> 0 THEN
                RAISE EXCEPTION 'a reservation state starts empty'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION 'risk_reservation_states changes only through '
                'risk_reservation_events' USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.portfolio_account_id <> OLD.portfolio_account_id THEN
            RAISE EXCEPTION 'the account of a reservation state cannot change'
                USING ERRCODE = 'restrict_violation';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    """,
    # The state machine. Checks the event against the current projection, applies it to the
    # reservation and to the account totals, and advances the account's event counter. An
    # event computed from a stale view of the account (its account_sequence is not the next
    # one) is refused with a serialization failure.
    "atlas_risk_reservation_apply": """
    DECLARE
        r risk_reservations%ROWTYPE;
        a record;
        expected_from text;
        new_status text;
        link text;
        ref text;
        deferred timestamptz;
        q_app numeric; q_con numeric; q_rel numeric; q_rem numeric;
        ap_risk numeric; ap_exp numeric; ap_cap numeric; ap_mar numeric;
        rm_risk numeric; rm_exp numeric; rm_cap numeric; rm_mar numeric;
        was_active boolean;
        now_active boolean;
        d_exp numeric;
        tripped text;
        recon text;
        movement numeric;
        takes_all boolean := false;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT * INTO r FROM risk_reservations WHERE id = NEW.reservation_id FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'reservation % does not exist', NEW.reservation_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
        IF r.portfolio_account_id <> NEW.portfolio_account_id THEN
            RAISE EXCEPTION 'reservation % belongs to another account', r.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.reservation_sequence <> r.last_event_sequence + 1 THEN
            RAISE EXCEPTION 'reservation % event % must follow event %', r.id,
                NEW.reservation_sequence, r.last_event_sequence USING ERRCODE = '40001';
        END IF;
        expected_from := CASE WHEN r.last_event_sequence = 0 THEN NULL ELSE r.status END;
        IF NEW.from_status IS DISTINCT FROM expected_from THEN
            RAISE EXCEPTION 'reservation % is %, not %', r.id, expected_from, NEW.from_status
                USING ERRCODE = 'check_violation';
        END IF;

        q_app := r.approved_quantity; q_con := r.consumed_quantity;
        q_rel := r.released_quantity; q_rem := r.remaining_quantity;
        ap_risk := r.approved_risk; ap_exp := r.approved_exposure;
        ap_cap := r.approved_capital; ap_mar := r.approved_margin;
        rm_risk := r.remaining_risk; rm_exp := r.remaining_exposure;
        rm_cap := r.remaining_capital; rm_mar := r.remaining_margin;
        link := r.execution_link; ref := r.order_ref; deferred := r.expiry_deferred_at;
        new_status := r.status;
        was_active := r.status IN ('APPROVED', 'PARTIALLY_CONSUMED');

        IF NEW.event_type NOT IN ('APPROVED', 'CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED')
           AND (NEW.quantity <> 0 OR NEW.risk_amount <> 0 OR NEW.exposure_amount <> 0
                OR NEW.capital_amount <> 0 OR NEW.margin_amount <> 0) THEN
            RAISE EXCEPTION '% events move no quantity or amount', NEW.event_type
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.event_type = 'CREATED' THEN
            IF r.last_event_sequence <> 0 THEN
                RAISE EXCEPTION 'reservation % was already created', r.id
                    USING ERRCODE = 'check_violation';
            END IF;
            new_status := 'PENDING';

        ELSIF NEW.event_type = 'APPROVED' THEN
            IF r.status <> 'PENDING' OR r.assessment_id IS NULL THEN
                RAISE EXCEPTION 'only a PENDING, assessed reservation can be approved'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT result, permitted_quantity, potential_loss, notional, valid_until,
                   created_txid, calculations -> 'reservation' AS reserved
                INTO a FROM risk_assessments WHERE id = r.assessment_id;
            IF a.result <> 'PASS_FOR_RESERVATION' OR a.created_txid <> txid_current() THEN
                RAISE EXCEPTION 'reservation % needs a passing assessment recorded in this '
                    'transaction', r.id USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.quantity <> a.permitted_quantity OR NEW.risk_amount <> a.potential_loss
               OR NEW.exposure_amount <> a.notional
               OR NEW.capital_amount IS DISTINCT FROM (a.reserved ->> 'capital')::numeric
               OR NEW.margin_amount IS DISTINCT FROM (a.reserved ->> 'margin')::numeric THEN
                RAISE EXCEPTION 'the approval must reserve exactly what the assessment '
                    'permitted (quantity %, loss %, notional %)', a.permitted_quantity,
                    a.potential_loss, a.notional USING ERRCODE = 'check_violation';
            END IF;
            IF r.expires_at > a.valid_until THEN
                RAISE EXCEPTION 'a reservation cannot outlive its assessment'
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT string_agg(breaker_type, ', ' ORDER BY breaker_type) INTO tripped
                FROM risk_circuit_breaker_states
                WHERE portfolio_account_id = r.portfolio_account_id AND status = 'TRIPPED';
            IF tripped IS NOT NULL THEN
                RAISE EXCEPTION 'no reservation can be approved while breakers are tripped: %',
                    tripped USING ERRCODE = 'check_violation';
            END IF;
            SELECT status INTO recon FROM risk_reservation_reconciliations
                WHERE portfolio_account_id = r.portfolio_account_id
                ORDER BY finished_at DESC, created_at DESC LIMIT 1;
            IF recon = 'FAILED' THEN
                RAISE EXCEPTION 'reservation approvals are blocked: the latest reservation '
                    'reconciliation FAILED' USING ERRCODE = 'check_violation';
            END IF;
            q_app := NEW.quantity; q_rem := NEW.quantity;
            ap_risk := NEW.risk_amount; ap_exp := NEW.exposure_amount;
            ap_cap := NEW.capital_amount; ap_mar := NEW.margin_amount;
            rm_risk := ap_risk; rm_exp := ap_exp; rm_cap := ap_cap; rm_mar := ap_mar;
            new_status := 'APPROVED';

        ELSIF NEW.event_type = 'REJECTED' THEN
            IF r.status <> 'PENDING' THEN
                RAISE EXCEPTION 'only a PENDING reservation can be rejected'
                    USING ERRCODE = 'check_violation';
            END IF;
            new_status := 'REJECTED';

        ELSIF NEW.event_type = 'ORDER_ATTACHED' THEN
            IF r.status <> 'APPROVED' OR r.execution_link <> 'NONE' OR NEW.order_ref IS NULL THEN
                RAISE EXCEPTION 'an order reference attaches once to an APPROVED reservation'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.occurred_at >= r.expires_at THEN
                RAISE EXCEPTION 'reservation % expired at %: nothing may depend on it',
                    r.id, r.expires_at USING ERRCODE = 'check_violation';
            END IF;
            link := 'ORDER_ATTACHED'; ref := NEW.order_ref;

        ELSIF NEW.event_type = 'CONSUMED' THEN
            IF NOT was_active OR r.execution_link = 'NONE' THEN
                RAISE EXCEPTION 'only an active reservation with an attached order is consumed'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.quantity <= 0 OR NEW.quantity > r.remaining_quantity THEN
                RAISE EXCEPTION 'consumption % exceeds the remaining %', NEW.quantity,
                    r.remaining_quantity USING ERRCODE = 'check_violation';
            END IF;
            IF r.order_ref IS NOT NULL AND NEW.order_ref IS DISTINCT FROM r.order_ref THEN
                RAISE EXCEPTION 'the fill is for order %, not %', NEW.order_ref, r.order_ref
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT quantity_delta INTO movement FROM asset_quantity_ledger
                WHERE entry_id = NEW.journal_entry_id AND asset_id = r.asset_id
                  AND portfolio_account_id = r.portfolio_account_id;
            IF movement IS NULL
               OR movement <> (CASE WHEN r.direction = 'LONG' THEN NEW.quantity
                                    ELSE -NEW.quantity END) THEN
                RAISE EXCEPTION 'journal entry % does not move % units of the asset in the '
                    'reservation''s direction', NEW.journal_entry_id, NEW.quantity
                    USING ERRCODE = 'check_violation';
            END IF;
            takes_all := NEW.quantity = r.remaining_quantity;
            q_con := q_con + NEW.quantity;
            new_status := CASE WHEN takes_all THEN 'CONSUMED' ELSE 'PARTIALLY_CONSUMED' END;

        ELSIF NEW.event_type IN ('RELEASED', 'EXPIRED', 'FAILED') THEN
            IF NOT was_active THEN
                RAISE EXCEPTION 'reservation % is % and holds nothing to give back', r.id,
                    r.status USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.quantity <> r.remaining_quantity THEN
                RAISE EXCEPTION '% must give back the whole remainder %', NEW.event_type,
                    r.remaining_quantity USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.event_type = 'EXPIRED' AND (r.execution_link <> 'NONE'
                    OR r.consumed_quantity <> 0 OR NEW.occurred_at < r.expires_at) THEN
                RAISE EXCEPTION 'reservation % cannot expire: it is not due, or an order '
                    'depends on it', r.id USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.event_type = 'RELEASED' AND NEW.resolution = 'NOT_SUBMITTED'
               AND r.execution_link <> 'NONE' THEN
                RAISE EXCEPTION 'reservation % has an attached order: NOT_SUBMITTED cannot '
                    'release it', r.id USING ERRCODE = 'check_violation';
            END IF;
            takes_all := true;
            q_rel := q_rel + NEW.quantity;
            new_status := NEW.event_type;

        ELSIF NEW.event_type = 'EXECUTION_UNCERTAIN' THEN
            IF NOT was_active THEN
                RAISE EXCEPTION 'only an active reservation can be held for review'
                    USING ERRCODE = 'check_violation';
            END IF;
            link := 'UNCERTAIN'; ref := COALESCE(r.order_ref, NEW.order_ref);

        ELSIF NEW.event_type = 'EXPIRY_DEFERRED' THEN
            IF NOT was_active OR r.execution_link = 'NONE' OR r.expiry_deferred_at IS NOT NULL
               OR NEW.occurred_at < r.expires_at THEN
                RAISE EXCEPTION 'expiry of reservation % cannot be deferred', r.id
                    USING ERRCODE = 'check_violation';
            END IF;
            deferred := NEW.occurred_at;
        END IF;

        IF NEW.event_type IN ('CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED') THEN
            IF takes_all THEN
                IF NEW.risk_amount <> rm_risk OR NEW.exposure_amount <> rm_exp
                   OR NEW.capital_amount <> rm_cap OR NEW.margin_amount <> rm_mar THEN
                    RAISE EXCEPTION 'taking the whole remainder must take every remaining amount'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.risk_amount > rm_risk * NEW.quantity / r.remaining_quantity
               OR NEW.exposure_amount > rm_exp * NEW.quantity / r.remaining_quantity
               OR NEW.capital_amount > rm_cap * NEW.quantity / r.remaining_quantity
               OR NEW.margin_amount > rm_mar * NEW.quantity / r.remaining_quantity THEN
                RAISE EXCEPTION 'a partial consumption cannot free more than its proportion'
                    USING ERRCODE = 'check_violation';
            END IF;
            q_rem := q_rem - NEW.quantity;
            rm_risk := rm_risk - NEW.risk_amount; rm_exp := rm_exp - NEW.exposure_amount;
            rm_cap := rm_cap - NEW.capital_amount; rm_mar := rm_mar - NEW.margin_amount;
        END IF;

        IF NEW.to_status IS DISTINCT FROM new_status THEN
            RAISE EXCEPTION '% leaves reservation % %, not %', NEW.event_type, r.id,
                new_status, NEW.to_status USING ERRCODE = 'check_violation';
        END IF;
        now_active := new_status IN ('APPROVED', 'PARTIALLY_CONSUMED');

        UPDATE risk_reservations SET
            status = new_status, last_event_sequence = NEW.reservation_sequence,
            approved_quantity = q_app, consumed_quantity = q_con, released_quantity = q_rel,
            remaining_quantity = q_rem,
            approved_risk = ap_risk, approved_exposure = ap_exp,
            approved_capital = ap_cap, approved_margin = ap_mar,
            remaining_risk = rm_risk, remaining_exposure = rm_exp,
            remaining_capital = rm_cap, remaining_margin = rm_mar,
            execution_link = link, order_ref = ref, expiry_deferred_at = deferred
        WHERE id = r.id;

        d_exp := rm_exp - r.remaining_exposure;
        UPDATE risk_reservation_states SET
            last_sequence = NEW.account_sequence,
            active_count = active_count + (now_active::int - was_active::int),
            reserved_risk = reserved_risk + (rm_risk - r.remaining_risk),
            reserved_exposure = reserved_exposure + d_exp,
            reserved_long_exposure = reserved_long_exposure
                + CASE WHEN r.direction = 'LONG' THEN d_exp ELSE 0 END,
            reserved_short_exposure = reserved_short_exposure
                + CASE WHEN r.direction = 'SHORT' THEN d_exp ELSE 0 END,
            reserved_capital = reserved_capital + (rm_cap - r.remaining_capital),
            reserved_margin = reserved_margin + (rm_mar - r.remaining_margin)
        WHERE portfolio_account_id = NEW.portfolio_account_id
          AND last_sequence = NEW.account_sequence - 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'reservation event % of account % is out of sequence: the account '
                'changed since it was computed', NEW.account_sequence, NEW.portfolio_account_id
                USING ERRCODE = '40001';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_risk_reservation_approval_guard": """
    DECLARE
        r risk_reservations%ROWTYPE;
    BEGIN
        NEW.created_txid := txid_current();
        SELECT * INTO r FROM risk_reservations WHERE id = NEW.reservation_id;
        IF r.status IS DISTINCT FROM 'APPROVED' OR r.created_txid <> txid_current() THEN
            RAISE EXCEPTION 'an approval record is written with its reservation''s approval'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.approved_quantity <> r.approved_quantity OR NEW.assessment_id <> r.assessment_id
           OR NEW.portfolio_account_id <> r.portfolio_account_id OR NEW.asset_id <> r.asset_id
           OR NEW.direction <> r.direction OR NEW.valid_until <> r.expires_at
           OR NEW.potential_loss <> r.approved_risk OR NEW.notional <> r.approved_exposure
           OR NEW.proposed_quantity IS DISTINCT FROM r.proposed_quantity THEN
            RAISE EXCEPTION 'the approval record must match reservation %', r.id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
    "atlas_risk_reservation_request_guard": """
    BEGIN
        NEW.created_txid := txid_current();
        IF (SELECT portfolio_account_id FROM risk_reservations WHERE id = NEW.reservation_id)
                IS DISTINCT FROM NEW.portfolio_account_id THEN
            RAISE EXCEPTION 'request % is for another account''s reservation',
                NEW.idempotency_key USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END;
    """,
}

# At COMMIT: a reservation was decided in its own transaction (never left PENDING), has
# its idempotency record, and an approval record when it was approved.
DECIDED_CHECK = """
CREATE FUNCTION atlas_risk_reservation_decided() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    r risk_reservations%ROWTYPE;
    approval record;
BEGIN
    SELECT * INTO r FROM risk_reservations WHERE id = NEW.id;
    IF r.status = 'PENDING' OR r.last_event_sequence < 2 THEN
        RAISE EXCEPTION 'reservation % was not decided in its own transaction', r.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM risk_reservation_requests
                   WHERE reservation_id = r.id AND command = 'RESERVE'
                     AND idempotency_key = r.idempotency_key
                     AND request_sha256 = r.request_sha256) THEN
        RAISE EXCEPTION 'reservation % has no idempotency record', r.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF r.approved_quantity > 0 THEN
        SELECT approved_quantity INTO approval FROM risk_reservation_approvals
            WHERE reservation_id = r.id;
        IF approval.approved_quantity IS DISTINCT FROM r.approved_quantity THEN
            RAISE EXCEPTION 'approved reservation % has no matching approval record', r.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
"""

RISK_EVENT_TYPES_6B = (
    "CONFIGURATION_CREATED",
    "CONFIGURATION_CHANGED",
    "ASSESSMENT_RECORDED",
    "BREAKER_TRIPPED",
    "BREAKER_RESET",
    "BREAKER_RESET_REFUSED",
    "STATE_EVALUATED",
    "CLASSIFICATION_RECORDED",
)
RISK_EVENT_TYPES_6C = (
    "RESERVATION_APPROVED",
    "RESERVATION_REJECTED",
    "RESERVATION_CONSUMED",
    "RESERVATION_RELEASED",
    "RESERVATION_EXPIRED",
    "RESERVATION_FAILED",
    "RESERVATION_UNCERTAIN",
    "RESERVATION_RECONCILED",
    "RESERVATION_MISMATCH",
)


def _in(values: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _guards() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )
    for table in ("risk_reservations", "risk_reservation_states"):
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION atlas_reject_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION atlas_reject_modification()"
        )
    for name, body in GUARD_FUNCTIONS.items():
        op.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $${body}$$;")
    op.execute(
        "CREATE TRIGGER risk_reservations_insert_guard BEFORE INSERT ON risk_reservations "
        "FOR EACH ROW EXECUTE FUNCTION atlas_risk_reservation_insert_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_reservations_update_guard BEFORE UPDATE ON risk_reservations "
        "FOR EACH ROW EXECUTE FUNCTION atlas_risk_reservation_update_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_states_guard BEFORE INSERT OR UPDATE ON "
        "risk_reservation_states FOR EACH ROW "
        "EXECUTE FUNCTION atlas_risk_reservation_state_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_events_apply BEFORE INSERT ON risk_reservation_events "
        "FOR EACH ROW EXECUTE FUNCTION atlas_risk_reservation_apply()"
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_approvals_guard BEFORE INSERT ON "
        "risk_reservation_approvals FOR EACH ROW "
        "EXECUTE FUNCTION atlas_risk_reservation_approval_guard()"
    )
    op.execute(
        "CREATE TRIGGER risk_reservation_requests_guard BEFORE INSERT ON "
        "risk_reservation_requests FOR EACH ROW "
        "EXECUTE FUNCTION atlas_risk_reservation_request_guard()"
    )
    op.execute(DECIDED_CHECK)
    op.execute(
        "CREATE CONSTRAINT TRIGGER risk_reservations_decided AFTER INSERT ON risk_reservations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION atlas_risk_reservation_decided()"
    )


def _drop_guards() -> None:
    op.execute("DROP TRIGGER IF EXISTS risk_reservations_decided ON risk_reservations")
    op.execute("DROP FUNCTION IF EXISTS atlas_risk_reservation_decided()")
    op.execute(
        "DROP TRIGGER IF EXISTS risk_reservation_requests_guard ON risk_reservation_requests"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS risk_reservation_approvals_guard ON risk_reservation_approvals"
    )
    op.execute("DROP TRIGGER IF EXISTS risk_reservation_events_apply ON risk_reservation_events")
    op.execute("DROP TRIGGER IF EXISTS risk_reservation_states_guard ON risk_reservation_states")
    op.execute("DROP TRIGGER IF EXISTS risk_reservations_update_guard ON risk_reservations")
    op.execute("DROP TRIGGER IF EXISTS risk_reservations_insert_guard ON risk_reservations")
    for name in GUARD_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
    for table in ("risk_reservations", "risk_reservation_states"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete ON {table}")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")


def _alter_6b_constraints() -> None:
    """Reserved exposure is now real; risk events gain the reservation types."""
    op.drop_constraint(
        "ck_risk_exposure_snapshots_no_reservations_before_6c", "risk_exposure_snapshots"
    )
    op.create_check_constraint(
        op.f("ck_risk_exposure_snapshots_pending_non_negative"),
        "risk_exposure_snapshots",
        "pending_exposure >= 0",
    )
    op.drop_constraint("ck_risk_events_risk_event_type", "risk_events")
    op.create_check_constraint(
        op.f("ck_risk_events_risk_event_type"),
        "risk_events",
        _in(RISK_EVENT_TYPES_6B + RISK_EVENT_TYPES_6C),
    )


def _restore_6b_constraints() -> None:
    op.drop_constraint("ck_risk_events_risk_event_type", "risk_events")
    op.create_check_constraint(
        op.f("ck_risk_events_risk_event_type"), "risk_events", _in(RISK_EVENT_TYPES_6B)
    )
    op.drop_constraint("ck_risk_exposure_snapshots_pending_non_negative", "risk_exposure_snapshots")
    op.create_check_constraint(
        op.f("ck_risk_exposure_snapshots_no_reservations_before_6c"),
        "risk_exposure_snapshots",
        "pending_exposure = 0",
    )


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "risk_reservation_reconciliations",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PASSED",
                "FAILED",
                name="risk_reservation_reconciliation_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_sequence", sa.BigInteger(), nullable=False),
        sa.Column("reservations_checked", sa.Integer(), nullable=False),
        sa.Column("events_checked", sa.Integer(), nullable=False),
        sa.Column("unexplained", sa.Integer(), nullable=False),
        sa.Column("warnings", sa.Integer(), nullable=False),
        sa.Column(
            "findings", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column(
            "totals", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(status = 'PASSED') = (unexplained = 0)",
            name=op.f("ck_risk_reservation_reconciliations_passed_iff_nothing_unexplained"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at",
            name=op.f("ck_risk_reservation_reconciliations_finished_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f(
                "fk_risk_reservation_reconciliations_portfolio_account_id_portfolio_accounts"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservation_reconciliations")),
    )
    op.create_index(
        op.f("ix_risk_reservation_reconciliations_portfolio_account_id_finished_at"),
        "risk_reservation_reconciliations",
        ["portfolio_account_id", "finished_at"],
        unique=False,
    )
    op.create_table(
        "risk_reservation_states",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("active_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "reserved_risk", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "reserved_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_long_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_short_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_capital",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_margin", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
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
            "active_count >= 0", name=op.f("ck_risk_reservation_states_count_non_negative")
        ),
        sa.CheckConstraint(
            "last_sequence >= 0", name=op.f("ck_risk_reservation_states_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "reserved_exposure = reserved_long_exposure + reserved_short_exposure",
            name=op.f("ck_risk_reservation_states_gross_is_long_plus_short"),
        ),
        sa.CheckConstraint(
            "reserved_risk >= 0 AND reserved_exposure >= 0 AND reserved_capital >= 0 AND reserved_margin >= 0 AND reserved_long_exposure >= 0 AND reserved_short_exposure >= 0",
            name=op.f("ck_risk_reservation_states_totals_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_reservation_states_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("portfolio_account_id", name=op.f("pk_risk_reservation_states")),
    )
    op.create_table(
        "system_health_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "severity",
            sa.Enum(
                "INFO",
                "WARNING",
                "HIGH",
                "CRITICAL",
                name="system_health_severity",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=True),
        sa.Column("subject_kind", sa.String(length=48), nullable=True),
        sa.Column("subject_id", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(summary)) >= 3", name=op.f("ck_system_health_events_summary_given")
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_system_health_events_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_health_events")),
    )
    op.create_index(
        op.f("ix_system_health_events_component_occurred_at"),
        "system_health_events",
        ["component", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_system_health_events_severity_occurred_at"),
        "system_health_events",
        ["severity", "occurred_at"],
        unique=False,
    )
    op.create_table(
        "risk_reservations",
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "OPERATOR",
                "STRATEGY_PROPOSAL",
                "AI_DECISION",
                name="risk_reservation_source",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("strategy_proposal_id", sa.UUID(), nullable=True),
        sa.Column("ai_decision_id", sa.UUID(), nullable=True),
        sa.Column("assessment_id", sa.UUID(), nullable=True),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "LONG",
                "SHORT",
                name="risk_reservation_direction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("functional_currency", sa.String(length=3), nullable=False),
        sa.Column("entry", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("stop", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("target", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("proposed_quantity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column(
            "approved_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column("fx_rate", sa.Numeric(precision=30, scale=12), nullable=True),
        sa.Column(
            "approved_risk", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "approved_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "approved_capital",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "approved_margin", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "PARTIALLY_CONSUMED",
                "CONSUMED",
                "RELEASED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="risk_reservation_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "consumed_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "released_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_quantity",
            sa.Numeric(precision=38, scale=18),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_risk", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "remaining_exposure",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_capital",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_margin",
            sa.Numeric(precision=28, scale=8),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "execution_link",
            sa.Enum(
                "NONE",
                "ORDER_ATTACHED",
                "UNCERTAIN",
                name="risk_reservation_execution_link",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="NONE",
            nullable=False,
        ),
        sa.Column("order_ref", sa.String(length=160), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_deferred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reasons", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("last_event_sequence", sa.Integer(), server_default="0", nullable=False),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "(execution_link <> 'NONE' OR order_ref IS NULL) AND (execution_link <> 'ORDER_ATTACHED' OR order_ref IS NOT NULL)",
            name=op.f("ck_risk_reservations_order_ref_matches_link"),
        ),
        sa.CheckConstraint(
            "NOT (status IN ('APPROVED', 'PARTIALLY_CONSUMED')) OR remaining_quantity > 0",
            name=op.f("ck_risk_reservations_active_holds_quantity"),
        ),
        sa.CheckConstraint(
            "NOT (status IN ('CONSUMED', 'RELEASED', 'EXPIRED', 'REJECTED', 'FAILED')) OR (remaining_quantity = 0 AND remaining_risk = 0 AND remaining_exposure = 0 AND remaining_capital = 0 AND remaining_margin = 0)",
            name=op.f("ck_risk_reservations_final_holds_nothing"),
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_risk_reservations_request_sha256_format"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'REJECTED') OR (approved_quantity > 0 AND assessment_id IS NOT NULL AND approved_risk > 0 AND approved_exposure > 0 AND fx_rate IS NOT NULL AND fx_rate > 0)",
            name=op.f("ck_risk_reservations_approval_is_sized"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('PARTIALLY_CONSUMED', 'CONSUMED') OR consumed_quantity > 0",
            name=op.f("ck_risk_reservations_consumed_has_fills"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('PENDING', 'REJECTED') OR (approved_quantity = 0 AND approved_risk = 0 AND approved_exposure = 0 AND approved_capital = 0 AND approved_margin = 0)",
            name=op.f("ck_risk_reservations_unapproved_reserves_nothing"),
        ),
        sa.CheckConstraint(
            "approved_quantity >= 0 AND consumed_quantity >= 0 AND released_quantity >= 0 AND remaining_quantity >= 0",
            name=op.f("ck_risk_reservations_quantities_non_negative"),
        ),
        sa.CheckConstraint(
            "approved_risk >= 0 AND remaining_risk >= 0 AND remaining_risk <= approved_risk AND approved_exposure >= 0 AND remaining_exposure >= 0 AND remaining_exposure <= approved_exposure AND approved_capital >= 0 AND remaining_capital >= 0 AND remaining_capital <= approved_capital AND approved_margin >= 0 AND remaining_margin >= 0 AND remaining_margin <= approved_margin",
            name=op.f("ck_risk_reservations_amounts_within_approval"),
        ),
        sa.CheckConstraint(
            "consumed_quantity + released_quantity + remaining_quantity = approved_quantity",
            name=op.f("ck_risk_reservations_quantity_conservation"),
        ),
        sa.CheckConstraint("entry > 0", name=op.f("ck_risk_reservations_entry_positive")),
        sa.CheckConstraint(
            "expires_at >= requested_at",
            name=op.f("ck_risk_reservations_expires_not_before_request"),
        ),
        sa.CheckConstraint(
            "last_event_sequence >= 0", name=op.f("ck_risk_reservations_sequence_non_negative")
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) >= 8", name=op.f("ck_risk_reservations_key_present")
        ),
        sa.CheckConstraint(
            "proposed_quantity IS NULL OR approved_quantity <= proposed_quantity",
            name=op.f("ck_risk_reservations_never_above_proposal"),
        ),
        sa.ForeignKeyConstraint(
            ["ai_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_risk_reservations_ai_decision_id_ai_decisions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_risk_reservations_assessment_id_risk_assessments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_risk_reservations_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["functional_currency"],
            ["currencies.code"],
            name=op.f("fk_risk_reservations_functional_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_reservations_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["quote_currency"],
            ["currencies.code"],
            name=op.f("fk_risk_reservations_quote_currency_currencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_proposal_id"],
            ["strategy_proposals.id"],
            name=op.f("fk_risk_reservations_strategy_proposal_id_strategy_proposals"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservations")),
        sa.UniqueConstraint("assessment_id", name=op.f("uq_risk_reservations_assessment_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_risk_reservations_idempotency_key")),
    )
    op.create_index(
        op.f("ix_risk_reservations_asset_id_status"),
        "risk_reservations",
        ["asset_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_reservations_portfolio_account_id_requested_at"),
        "risk_reservations",
        ["portfolio_account_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_reservations_portfolio_account_id_status"),
        "risk_reservations",
        ["portfolio_account_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_risk_reservations_active_ai_decision",
        "risk_reservations",
        ["ai_decision_id"],
        unique=True,
        postgresql_where=sa.text(
            "ai_decision_id IS NOT NULL AND status IN ('APPROVED', 'PARTIALLY_CONSUMED')"
        ),
    )
    op.create_index(
        "uq_risk_reservations_active_strategy_proposal",
        "risk_reservations",
        ["strategy_proposal_id"],
        unique=True,
        postgresql_where=sa.text(
            "strategy_proposal_id IS NOT NULL AND status IN ('APPROVED', 'PARTIALLY_CONSUMED')"
        ),
    )
    op.create_table(
        "risk_reservation_approvals",
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("assessment_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "LONG",
                "SHORT",
                name="risk_approval_direction",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("proposed_quantity", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("approved_quantity", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("entry", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("worst_entry_price", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("stop", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("target", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("potential_loss", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("notional", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
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
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_risk_reservation_approvals_content_sha256_format"),
        ),
        sa.CheckConstraint(
            "approved_quantity > 0", name=op.f("ck_risk_reservation_approvals_quantity_positive")
        ),
        sa.CheckConstraint(
            "proposed_quantity IS NULL OR approved_quantity <= proposed_quantity",
            name=op.f("ck_risk_reservation_approvals_never_above_proposal"),
        ),
        sa.CheckConstraint(
            "valid_until > approved_at",
            name=op.f("ck_risk_reservation_approvals_valid_after_approval"),
        ),
        sa.CheckConstraint(
            "worst_entry_price > 0", name=op.f("ck_risk_reservation_approvals_worst_entry_positive")
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_risk_reservation_approvals_assessment_id_risk_assessments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_risk_reservation_approvals_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_reservation_approvals_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_risk_reservation_approvals_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservation_approvals")),
        sa.UniqueConstraint(
            "reservation_id", name=op.f("uq_risk_reservation_approvals_reservation_id")
        ),
    )
    op.create_table(
        "risk_reservation_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "command",
            sa.Enum(
                "RESERVE",
                "ATTACH_ORDER",
                "CONSUME",
                "RELEASE",
                "FAIL",
                "FLAG_UNCERTAIN",
                name="risk_reservation_command",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column(
            "result_status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "PARTIALLY_CONSUMED",
                "CONSUMED",
                "RELEASED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="risk_reservation_request_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=128), nullable=False),
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
            "request_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_risk_reservation_requests_request_sha256_format"),
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) >= 8",
            name=op.f("ck_risk_reservation_requests_key_present"),
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_reservation_requests_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_risk_reservation_requests_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservation_requests")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_risk_reservation_requests_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_risk_reservation_requests_reservation_id"),
        "risk_reservation_requests",
        ["reservation_id"],
        unique=False,
    )
    op.create_table(
        "risk_reservation_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("reservation_id", sa.UUID(), nullable=False),
        sa.Column("portfolio_account_id", sa.UUID(), nullable=False),
        sa.Column("account_sequence", sa.BigInteger(), nullable=False),
        sa.Column("reservation_sequence", sa.Integer(), nullable=False),
        sa.Column(
            "event_type",
            sa.Enum(
                "CREATED",
                "APPROVED",
                "REJECTED",
                "ORDER_ATTACHED",
                "CONSUMED",
                "RELEASED",
                "EXPIRED",
                "FAILED",
                "EXECUTION_UNCERTAIN",
                "EXPIRY_DEFERRED",
                name="risk_reservation_event_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "from_status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "PARTIALLY_CONSUMED",
                "CONSUMED",
                "RELEASED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="risk_reservation_from_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "PENDING",
                "APPROVED",
                "PARTIALLY_CONSUMED",
                "CONSUMED",
                "RELEASED",
                "EXPIRED",
                "REJECTED",
                "FAILED",
                name="risk_reservation_to_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "quantity", sa.Numeric(precision=38, scale=18), server_default="0", nullable=False
        ),
        sa.Column(
            "risk_amount", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "exposure_amount", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "capital_amount", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column(
            "margin_amount", sa.Numeric(precision=28, scale=8), server_default="0", nullable=False
        ),
        sa.Column("journal_entry_id", sa.UUID(), nullable=True),
        sa.Column("order_ref", sa.String(length=160), nullable=True),
        sa.Column(
            "resolution",
            sa.Enum(
                "NOT_SUBMITTED",
                "ORDER_CANCELLED",
                "ORDER_REJECTED",
                "ORDER_EXPIRED",
                "ORDER_COMPLETED",
                "OPERATOR",
                name="risk_reservation_resolution",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("request_id", sa.BigInteger(), nullable=True),
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
            "(event_type = 'CONSUMED') = (journal_entry_id IS NOT NULL)",
            name=op.f("ck_risk_reservation_events_consumption_references_ledger"),
        ),
        sa.CheckConstraint(
            "(event_type = 'RELEASED') = (resolution IS NOT NULL)",
            name=op.f("ck_risk_reservation_events_release_has_resolution"),
        ),
        sa.CheckConstraint(
            "resolution IS DISTINCT FROM 'OPERATOR' OR actor ~ '^operator:[A-Za-z0-9._@-]+$'",
            name=op.f("ck_risk_reservation_events_operator_release_by_operator"),
        ),
        sa.CheckConstraint(
            "account_sequence >= 1",
            name=op.f("ck_risk_reservation_events_account_sequence_positive"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) >= 3", name=op.f("ck_risk_reservation_events_reason_given")
        ),
        sa.CheckConstraint(
            "quantity >= 0 AND risk_amount >= 0 AND exposure_amount >= 0 AND capital_amount >= 0 AND margin_amount >= 0",
            name=op.f("ck_risk_reservation_events_magnitudes_non_negative"),
        ),
        sa.CheckConstraint(
            "reservation_sequence >= 1",
            name=op.f("ck_risk_reservation_events_reservation_sequence_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_risk_reservation_events_journal_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_account_id"],
            ["portfolio_accounts.id"],
            name=op.f("fk_risk_reservation_events_portfolio_account_id_portfolio_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["risk_reservation_requests.id"],
            name=op.f("fk_risk_reservation_events_request_id_risk_reservation_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["risk_reservations.id"],
            name=op.f("fk_risk_reservation_events_reservation_id_risk_reservations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_reservation_events")),
        sa.UniqueConstraint(
            "journal_entry_id", name=op.f("uq_risk_reservation_events_journal_entry_id")
        ),
        sa.UniqueConstraint(
            "portfolio_account_id",
            "account_sequence",
            name=op.f("uq_risk_reservation_events_portfolio_account_id_account_sequence"),
        ),
        sa.UniqueConstraint(
            "reservation_id",
            "reservation_sequence",
            name=op.f("uq_risk_reservation_events_reservation_id_reservation_sequence"),
        ),
    )
    op.create_index(
        op.f("ix_risk_reservation_events_event_type_occurred_at"),
        "risk_reservation_events",
        ["event_type", "occurred_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_risk_reservation_events_portfolio_account_id_occurred_at"),
        "risk_reservation_events",
        ["portfolio_account_id", "occurred_at"],
        unique=False,
    )
    # ### end Alembic commands ###
    _guards()
    _alter_6b_constraints()


def downgrade() -> None:
    _restore_6b_constraints()
    _drop_guards()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        op.f("ix_risk_reservation_events_portfolio_account_id_occurred_at"),
        table_name="risk_reservation_events",
    )
    op.drop_index(
        op.f("ix_risk_reservation_events_event_type_occurred_at"),
        table_name="risk_reservation_events",
    )
    op.drop_table("risk_reservation_events")
    op.drop_index(
        op.f("ix_risk_reservation_requests_reservation_id"), table_name="risk_reservation_requests"
    )
    op.drop_table("risk_reservation_requests")
    op.drop_table("risk_reservation_approvals")
    op.drop_index(
        "uq_risk_reservations_active_strategy_proposal",
        table_name="risk_reservations",
        postgresql_where=sa.text(
            "strategy_proposal_id IS NOT NULL AND status IN ('APPROVED', 'PARTIALLY_CONSUMED')"
        ),
    )
    op.drop_index(
        "uq_risk_reservations_active_ai_decision",
        table_name="risk_reservations",
        postgresql_where=sa.text(
            "ai_decision_id IS NOT NULL AND status IN ('APPROVED', 'PARTIALLY_CONSUMED')"
        ),
    )
    op.drop_index(
        op.f("ix_risk_reservations_portfolio_account_id_status"), table_name="risk_reservations"
    )
    op.drop_index(
        op.f("ix_risk_reservations_portfolio_account_id_requested_at"),
        table_name="risk_reservations",
    )
    op.drop_index(op.f("ix_risk_reservations_asset_id_status"), table_name="risk_reservations")
    op.drop_table("risk_reservations")
    op.drop_index(
        op.f("ix_system_health_events_severity_occurred_at"), table_name="system_health_events"
    )
    op.drop_index(
        op.f("ix_system_health_events_component_occurred_at"), table_name="system_health_events"
    )
    op.drop_table("system_health_events")
    op.drop_table("risk_reservation_states")
    op.drop_index(
        op.f("ix_risk_reservation_reconciliations_portfolio_account_id_finished_at"),
        table_name="risk_reservation_reconciliations",
    )
    op.drop_table("risk_reservation_reconciliations")
    # ### end Alembic commands ###
