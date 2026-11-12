"""Phase 10B reliability: pure checks (slots, heartbeat thresholds, configuration, redaction)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config.settings import InvalidSettingsError
from app.core.security.operators import TOKEN_PREFIX, digest, new_token
from app.core.security.redaction import REDACTED, redact_text
from app.monitoring.heartbeats import MISSED_PASSES, STALE_FLOOR, stale_after
from app.orchestration.alerts import ACTIONS, action_for
from app.orchestration.config import OrchestratorSettings, check_environment
from app.orchestration.enums import DAILY_JOBS, FINAL_JOB_STATUSES, JobKind, JobStatus
from app.orchestration.jobs import (
    after_close,
    around_open,
    every,
    previous_days,
    slot_day,
    slot_start,
)

# Tuesday 2026-11-03 (a trading day; the regular session is 14:30-21:00 UTC).
TUE_OPEN = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
TUE_CLOSE = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
SAT = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def settings(**kw: object) -> OrchestratorSettings:
    return OrchestratorSettings(_env_file=None, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- slots
def test_interval_slots_round_trip_and_daily_slots_name_the_trading_day():
    slot = every(300)(TUE_OPEN + timedelta(seconds=299))
    assert slot == "2026-11-03T14:30:00Z"
    assert slot_start(slot) == TUE_OPEN
    assert slot_start("close:2026-11-03") is None
    assert after_close(900, "eod")(TUE_CLOSE + timedelta(minutes=14)) is None
    assert after_close(900, "eod")(TUE_CLOSE + timedelta(minutes=15)) == "eod:2026-11-03"
    assert slot_day("eod:2026-11-03") == TUE_CLOSE.date()
    assert slot_day("2026-11-03T14:30:00Z") is None


def test_the_open_check_slot_covers_the_lead_and_window_of_a_trading_day_only():
    check = around_open(1800, 3600)
    assert check(TUE_OPEN - timedelta(minutes=31)) is None
    assert check(TUE_OPEN - timedelta(minutes=30)) == "open:2026-11-03"
    assert check(TUE_OPEN + timedelta(minutes=59)) == "open:2026-11-03"
    assert check(TUE_OPEN + timedelta(minutes=60)) is None
    assert check(SAT) is None


def test_previous_days_are_trading_days_oldest_first():
    past = previous_days("close")
    # Monday 2026-11-09 midday: the three trading days before are Wed, Thu, Fri.
    monday = datetime(2026, 11, 9, 16, 0, tzinfo=UTC)
    assert past(monday, 3) == ["close:2026-11-04", "close:2026-11-05", "close:2026-11-06"]
    assert past(SAT, 2) == ["close:2026-11-05", "close:2026-11-06"]
    assert past(monday, 0) == []


def test_daily_kinds_and_final_statuses():
    assert JobKind.SESSION_CLOSE_REVIEW in DAILY_JOBS
    assert JobKind.RISK_MONITOR not in DAILY_JOBS
    assert JobStatus.SUPERSEDED in FINAL_JOB_STATUSES
    assert JobStatus.FAILED not in FINAL_JOB_STATUSES


# ---------------------------------------------------------------------- heartbeats
def test_a_heartbeat_is_stale_after_several_missed_passes_never_less_than_a_minute():
    assert stale_after(None) == STALE_FLOOR
    assert stale_after(5) == STALE_FLOOR  # 6 x 5 s < 60 s
    assert stale_after(Decimal("30")) == timedelta(seconds=MISSED_PASSES * 30)


# ---------------------------------------------------------------------- configuration
def test_defaults_keep_the_protection_gate_and_backstops_on():
    s = settings()
    assert s.require_protection_workers is True
    assert s.backstop_position_monitor is True
    assert s.backstop_paper_execution is True
    assert s.worker_enabled is False  # 10A: still off by default


def test_the_paper_environment_needs_explicit_configuration():
    check_environment(settings(), "paper")  # worker off: nothing to check
    check_environment(settings(worker_enabled=True), "development")
    with pytest.raises(InvalidSettingsError) as caught:
        check_environment(settings(worker_enabled=True, require_protection_workers=False), "paper")
    message = str(caught.value)
    assert "ORCHESTRATOR_ACCOUNT_NAME" in message
    assert "ORCHESTRATOR_REQUIRE_PROTECTION_WORKERS" in message
    check_environment(settings(worker_enabled=True, account_name="Atlas paper account"), "paper")


# ---------------------------------------------------------------------- operator tokens
def test_operator_tokens_are_random_prefixed_and_redacted_everywhere():
    a, b = new_token(), new_token()
    assert a != b
    assert a.startswith(TOKEN_PREFIX)
    assert len(a) > 40
    assert digest(a) != digest(b)
    assert len(digest(a)) == 64
    leaked = f"request failed for header {a} at /api"
    assert a not in redact_text(leaked)
    assert REDACTED in redact_text(leaked)


def test_every_alert_type_has_an_action():
    for event_type in (
        "POSITION_MONITOR_DOWN",
        "PAPER_EXECUTION_DOWN",
        "PAPER_EXECUTION_STOPPED",
        "ENTRY_GATE_CLOSED",
        "FEED_STALE",
        "SESSION_NOT_READY",
        "ORCHESTRATION_JOB_DEAD",
        "ORCHESTRATION_SCHEDULE_GAP",
    ):
        assert action_for(event_type), event_type
    assert action_for("NOT_A_REAL_EVENT") is None
    assert all(len(text) > 20 for text in ACTIONS.values())
