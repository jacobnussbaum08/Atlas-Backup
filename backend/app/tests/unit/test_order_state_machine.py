"""Phase 7A order state machine: every valid and every invalid transition (pure)."""

from __future__ import annotations

import importlib.util
import itertools
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.execution import state_machine as sm
from app.execution.enums import FINAL_ORDER_STATUSES, OrderEventType, OrderStatus

S, E = OrderStatus, OrderEventType
STATUSES: list[OrderStatus | None] = [None, *OrderStatus]
VERSIONS = Path(__file__).resolve().parents[3] / "alembic/versions"
MIGRATION = VERSIONS / "0016_paper_execution_engine.py"
MIGRATION_7B = VERSIONS / "0017_simulated_execution_positions.py"

# The specification of Phase 7A written out independently of the module under test.
EXPECTED = {
    (None, E.CREATED, S.PROPOSED),
    (S.PROPOSED, E.APPROVED, S.APPROVED),
    (S.PROPOSED, E.REJECTED, S.REJECTED),
    (S.APPROVED, E.ACCEPTED, S.OPEN),
    (S.APPROVED, E.REJECTED, S.REJECTED),
    (S.APPROVED, E.CANCEL_REQUESTED, S.CANCEL_PENDING),
    (S.APPROVED, E.EXPIRED, S.EXPIRED),
    (S.APPROVED, E.FAILED, S.FAILED),
    (S.OPEN, E.FILL, S.PARTIALLY_FILLED),
    (S.OPEN, E.FILL, S.FILLED),
    (S.OPEN, E.CANCEL_REQUESTED, S.CANCEL_PENDING),
    (S.OPEN, E.EXPIRED, S.EXPIRED),
    (S.OPEN, E.FAILED, S.FAILED),
    (S.PARTIALLY_FILLED, E.FILL, S.PARTIALLY_FILLED),
    (S.PARTIALLY_FILLED, E.FILL, S.FILLED),
    (S.PARTIALLY_FILLED, E.CANCEL_REQUESTED, S.CANCEL_PENDING),
    (S.PARTIALLY_FILLED, E.EXPIRED, S.EXPIRED),
    (S.PARTIALLY_FILLED, E.FAILED, S.FAILED),
    (S.CANCEL_PENDING, E.ACCEPTED, S.CANCEL_PENDING),
    (S.CANCEL_PENDING, E.FILL, S.CANCEL_PENDING),
    (S.CANCEL_PENDING, E.FILL, S.FILLED),
    (S.CANCEL_PENDING, E.CANCEL_CONFIRMED, S.CANCELLED),
    (S.CANCEL_PENDING, E.CANCEL_REJECTED, S.OPEN),
    (S.CANCEL_PENDING, E.CANCEL_REJECTED, S.PARTIALLY_FILLED),
    (S.CANCEL_PENDING, E.EXPIRED, S.EXPIRED),
    (S.CANCEL_PENDING, E.FAILED, S.FAILED),
}
# Phase 7B: stop triggers are recorded without changing the status.
EXPECTED_7A = set(EXPECTED)
EXPECTED |= {
    (S.OPEN, E.TRIGGERED, S.OPEN),
    (S.CANCEL_PENDING, E.TRIGGERED, S.CANCEL_PENDING),
}
ALL = set(itertools.product(STATUSES, list(E), list(S)))


def table() -> set[tuple[OrderStatus | None, OrderEventType, OrderStatus]]:
    return {(f, e, t) for (f, e), targets in sm.TRANSITIONS.items() for t in targets}


def test_the_table_is_exactly_the_specified_transitions():
    assert table() == EXPECTED


@pytest.mark.parametrize(("source", "event", "target"), sorted(EXPECTED, key=str), ids=str)
def test_every_valid_transition_is_allowed(source, event, target):
    assert sm.is_allowed(source, event, target)
    sm.check(source, event, target)  # does not raise


@pytest.mark.parametrize(("source", "event", "target"), sorted(ALL - EXPECTED, key=str), ids=str)
def test_every_invalid_transition_is_refused(source, event, target):
    assert not sm.is_allowed(source, event, target)
    with pytest.raises(sm.InvalidOrderTransitionError):
        sm.check(source, event, target)


@pytest.mark.parametrize("final", sorted(FINAL_ORDER_STATUSES))
def test_final_statuses_have_no_transition(final):
    for event in E:
        assert sm.allowed_targets(final, event) == frozenset()
        with pytest.raises(sm.InvalidOrderTransitionError):
            sm.next_status(final, event, quantity=D(10), filled=D(0), fill=D(1))
    assert sm.is_final(final)


def test_a_cancellation_is_final_only_when_confirmed():
    assert not sm.is_final(S.CANCEL_PENDING)
    assert sm.allowed_targets(S.CANCEL_PENDING, E.FILL) == {S.CANCEL_PENDING, S.FILLED}
    assert sm.allowed_targets(S.OPEN, E.CANCEL_CONFIRMED) == frozenset()  # never skipped
    assert sm.allowed_targets(S.APPROVED, E.CANCEL_CONFIRMED) == frozenset()


def test_fill_targets_depend_on_the_remaining_quantity():
    q = D(100)
    assert sm.next_status(S.OPEN, E.FILL, quantity=q, filled=D(0), fill=D(40)) is S.PARTIALLY_FILLED
    assert sm.next_status(S.OPEN, E.FILL, quantity=q, filled=D(0), fill=q) is S.FILLED
    assert (
        sm.next_status(S.PARTIALLY_FILLED, E.FILL, quantity=q, filled=D(40), fill=D(60)) is S.FILLED
    )
    assert (
        sm.next_status(S.CANCEL_PENDING, E.FILL, quantity=q, filled=D(40), fill=D(10))
        is S.CANCEL_PENDING
    )
    assert (
        sm.next_status(S.CANCEL_PENDING, E.FILL, quantity=q, filled=D(40), fill=D(60)) is S.FILLED
    )


@pytest.mark.parametrize("fill", [D(0), D(-1), D(61)])
def test_zero_negative_and_over_fills_are_refused(fill):
    with pytest.raises(sm.InvalidOrderTransitionError):
        sm.next_status(S.PARTIALLY_FILLED, E.FILL, quantity=D(100), filled=D(40), fill=fill)


def test_rejected_cancellation_returns_to_the_right_working_status():
    q = D(100)
    assert sm.next_status(S.CANCEL_PENDING, E.CANCEL_REJECTED, quantity=q, filled=D(0)) is S.OPEN
    assert (
        sm.next_status(S.CANCEL_PENDING, E.CANCEL_REJECTED, quantity=q, filled=D(1))
        is S.PARTIALLY_FILLED
    )


def test_cancellable_and_fillable_sets():
    assert {S.APPROVED, S.OPEN, S.PARTIALLY_FILLED} == sm.CANCELLABLE
    assert {S.OPEN, S.PARTIALLY_FILLED, S.CANCEL_PENDING} == sm.FILLABLE
    for status in sm.CANCELLABLE:
        assert sm.is_allowed(status, E.CANCEL_REQUESTED, S.CANCEL_PENDING)
    for status in sm.FILLABLE:
        assert sm.allowed_targets(status, E.FILL)


def _frozen(path: Path) -> set[tuple[OrderStatus | None, OrderEventType, OrderStatus]]:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {(None if f is None else S(f), E(e), S(t)) for f, e, t in module.TRANSITIONS}


def test_the_database_migration_freezes_the_same_table():
    # 0016 froze the Phase 7A table; 0017 (Phase 7B) replaces it with the current one.
    assert _frozen(MIGRATION) == EXPECTED_7A
    assert _frozen(MIGRATION_7B) == table()


def test_a_trigger_never_changes_the_status():
    for status in (S.OPEN, S.CANCEL_PENDING):
        assert sm.next_status(status, E.TRIGGERED, quantity=D(10), filled=D(0)) is status
    others: list[OrderStatus | None] = [None, S.APPROVED, S.PARTIALLY_FILLED, *FINAL_ORDER_STATUSES]
    for other in others:
        assert not sm.allowed_targets(other, E.TRIGGERED)
