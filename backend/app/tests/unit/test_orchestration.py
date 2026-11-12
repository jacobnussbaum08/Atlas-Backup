"""Phase 10A orchestration: pure parts (configuration, slots, back-off, transitions)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.data_engine.calendar import MarketSession
from app.execution.enums import OrderType
from app.execution.validation import authorize_submitter
from app.monitoring.workers import orchestrator_stale_after
from app.orchestration.config import OrchestratorSettings, get_orchestrator_settings
from app.orchestration.controls import ALLOWED, ControlState
from app.orchestration.enums import ControlAction, IntentStatus, TradingMode
from app.orchestration.intents import MAX_ORDER_LIFETIME, ORCHESTRATOR_ACTOR, order_expiry
from app.orchestration.jobs import after_close, backoff, every
from app.workers.trading_orchestrator import heartbeat_interval


def _settings(**values: object) -> OrchestratorSettings:
    return OrchestratorSettings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_never_enable_trading(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("ORCHESTRATOR_"):
            monkeypatch.delenv(key)
    s = _settings()
    assert s.worker_enabled is False
    assert s.entry_order_type is OrderType.LIMIT
    # No setting can select a broker, a live mode or the trading mode itself.
    names = set(OrchestratorSettings.model_fields)
    assert not {n for n in names if "live" in n or "broker" in n or n == "trading_mode"}


def test_entry_sessions_and_order_type_are_validated():
    s = _settings(entry_sessions="regular, after_hours")
    assert s.entry_sessions == (MarketSession.REGULAR, MarketSession.AFTER_HOURS)
    with pytest.raises(ValidationError):
        _settings(entry_sessions="LUNCH")
    with pytest.raises(ValidationError, match="LIMIT or MARKET"):
        _settings(entry_order_type="STOP")
    with pytest.raises(ValidationError):
        _settings(tick_seconds=0)


def test_unknown_orchestrator_keys_are_refused(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_LIVE_TRADING", "true")
    get_orchestrator_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="ORCHESTRATOR_ENABLE_LIVE_TRADING"):
            get_orchestrator_settings()
    finally:
        get_orchestrator_settings.cache_clear()


def test_env_example_orchestrator_section_loads(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    for key in list(os.environ):
        if key.startswith("ORCHESTRATOR_"):
            monkeypatch.delenv(key)
    example = Path(__file__).resolve().parents[4] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    get_orchestrator_settings.cache_clear()
    try:
        s = get_orchestrator_settings()
    finally:
        get_orchestrator_settings.cache_clear()
    assert s.worker_enabled is False
    assert s.account_name == ""
    assert s.entry_sessions == (
        MarketSession.REGULAR,
        MarketSession.AFTER_HOURS,
        MarketSession.CLOSED,
    )


def test_the_orchestrator_is_an_authorized_paper_submitter_but_not_an_operator():
    assert authorize_submitter(ORCHESTRATOR_ACTOR) == []
    assert not ORCHESTRATOR_ACTOR.startswith("operator:")


def test_transition_table():
    assert ALLOWED[ControlAction.ENABLE] == (frozenset({TradingMode.DISABLED}), TradingMode.ENABLED)
    assert ALLOWED[ControlAction.CLEAR_EMERGENCY][1] is TradingMode.DISABLED
    assert ALLOWED[ControlAction.EMERGENCY_STOP][0] == frozenset(TradingMode)
    # Nothing but ENABLE and RESUME leads to ENABLED, and neither starts from EMERGENCY_STOP.
    to_enabled = [a for a, (_, to) in ALLOWED.items() if to is TradingMode.ENABLED]
    assert sorted(to_enabled) == [ControlAction.ENABLE, ControlAction.RESUME]
    for action in to_enabled:
        assert TradingMode.EMERGENCY_STOP not in ALLOWED[action][0]


def test_only_enabled_allows_entries():
    account = uuid.uuid4()
    for mode in TradingMode:
        state = ControlState(account, mode, 1, None, None, None)
        assert state.entries_allowed is (mode is TradingMode.ENABLED)
    assert ControlState(account, TradingMode.DISABLED, 0, None, None, None).as_dict()["default"]


def test_interval_slots_align_to_the_epoch():
    slot = every(300)
    a = datetime(2026, 11, 3, 22, 31, 10, tzinfo=UTC)
    assert slot(a) == "2026-11-03T22:30:00Z"
    assert slot(a + timedelta(minutes=3)) == "2026-11-03T22:30:00Z"
    assert slot(a + timedelta(minutes=4)) == "2026-11-03T22:35:00Z"


def test_after_close_slots():
    slot = after_close(3900)
    # Tuesday 3 Nov 2026: NYSE closes 21:00 UTC (EST); +65 min = 22:05 UTC.
    assert slot(datetime(2026, 11, 3, 22, 0, tzinfo=UTC)) is None
    assert slot(datetime(2026, 11, 3, 22, 10, tzinfo=UTC)) == "close:2026-11-03"
    assert slot(datetime(2026, 11, 7, 23, 0, tzinfo=UTC)) is None  # Saturday


def test_backoff_doubles_and_caps():
    assert backoff(1) == timedelta(seconds=30)
    assert backoff(2) == timedelta(seconds=60)
    assert backoff(3) == timedelta(seconds=120)
    assert backoff(20) == timedelta(minutes=15)


def test_entry_orders_live_one_regular_session():
    during = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)  # Tuesday, regular session
    assert order_expiry(during) == datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    after = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)
    assert order_expiry(after) == datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    friday_night = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)
    assert order_expiry(friday_night) == datetime(2026, 11, 9, 21, 0, tzinfo=UTC)
    assert order_expiry(friday_night) - friday_night <= MAX_ORDER_LIFETIME


def test_heartbeat_and_stale_thresholds():
    assert heartbeat_interval(_settings(tick_seconds=5)) == 5
    assert heartbeat_interval(_settings(tick_seconds=300)) == 30
    assert orchestrator_stale_after(None) == timedelta(seconds=90)
    assert orchestrator_stale_after(300) == timedelta(seconds=90)


def test_intent_statuses():
    final = set(IntentStatus) - {IntentStatus.RECEIVED}
    assert IntentStatus.ORDER_CREATED in final
    assert IntentStatus.DEAD in final
