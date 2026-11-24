"""D-237: engine 8A.2 (explicit execution conventions, cash dividends) and replay of 8A.1.

Bars, dividends and strategies are TEST FIXTURES (see ``backtest_support``), not market data.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine

from app.backtesting.broker import backtest_model
from app.backtesting.config import (
    DIVIDEND_POLICY,
    ENGINE_VERSION,
    ExecutionConventions,
    ResolvedConfig,
)
from app.backtesting.data import sessions_between
from app.backtesting.dividends import PROVIDER
from app.database.models import (
    BacktestJournalEntryRecord,
    BacktestRunEventRecord,
    BacktestTradeRecord,
    CashDividendRecord,
    CashDividendRetrievalRecord,
)
from app.database.sessions import session_scope
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    D,
    make_bench,
    make_traded_asset,
    rows,
    seed_series,
    trend,
)
from app.tests.integration.test_backtest_live_pipeline import WIDE, completed

pytestmark = pytest.mark.integration

RETRIEVED_AT = datetime(2026, 11, 1, tzinfo=UTC)


def seed_dividends(
    factory: Any,
    asset_id: uuid.UUID,
    symbol: str,
    rate: Decimal,
    *,
    retrieved_at: datetime = RETRIEVED_AT,
    revision: int = 0,
    coverage: bool = True,
    basis: str | None = "ex_date",
) -> None:
    """FIXTURE dividends: one going ex on every session of the run, plus a coverage row
    (``basis`` ``ex_date``: an X3.1 retrieval; None: a D-237 processing-date range)."""
    with session_scope(factory) as session:
        for day in sessions_between(RUN_START, RUN_END):
            session.add(
                CashDividendRecord(
                    provider=PROVIDER,
                    provider_action_id=f"fixture-{symbol}-{day.day.isoformat()}",
                    revision=revision,
                    asset_id=asset_id,
                    symbol=symbol,
                    ex_date=day.day,
                    rate=rate,
                    special=False,
                    foreign=False,
                    retrieved_at=retrieved_at,
                    raw={"fixture": True},
                )
            )
        if coverage:
            session.add(
                CashDividendRetrievalRecord(
                    provider=PROVIDER,
                    symbol=symbol,
                    range_start=date(2026, 1, 1),
                    range_end=date(2026, 12, 31),
                    dividends=len(sessions_between(RUN_START, RUN_END)),
                    status="OK",
                    retrieved_at=retrieved_at,
                    details={} if basis is None else {"basis": basis},
                )
            )


def summary(bench: Any, run_id: uuid.UUID) -> dict[str, Any]:
    events = [
        e for e in rows(bench, BacktestRunEventRecord, run_id) if e.kind == "PIPELINE_SUMMARY"
    ]
    assert len(events) == 1
    return dict(events[0].details)


def test_new_runs_use_engine_8a2_with_daily_v2_conventions_and_dividends(
    fresh_engine: Engine,
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    run_id = bench.run(bench.request(risk=WIDE))
    run = completed(bench, run_id)
    assert run.engine_version == ENGINE_VERSION == "8A.2"
    assert run.config["execution_conventions"] == "daily-v2"
    assert run.config["dividend_policy"] == DIVIDEND_POLICY
    assert run.config["execution_model"]["version"] == 2
    assert (
        run.execution_model_sha256
        == backtest_model(None, ExecutionConventions.DAILY_V2).definition_sha256()
    )
    details = summary(bench, run_id)
    assert details["execution_conventions"] == "daily-v2"
    assert details["execution_fidelity"]["limit_marketable_at_open"] is True
    # No retrieval covers FIXA: dividends are unknown, nothing is credited, and it is said.
    assert details["dividends"]["assets_not_retrieved"] == ["FIXA"]
    assert details["dividends"]["postings"] == {}


def test_a_stored_8a1_configuration_replays_identically(fresh_engine: Engine) -> None:
    """An 8A.1 configuration (no conventions or dividend keys) runs the daily-v1
    conventions and reproduces its result hash exactly under the 8A.2 engine."""
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    request = bench.request(risk=WIDE, execution_conventions="daily-v1", dividends=False)
    with session_scope(bench.factory) as session:
        resolved = bench.service.resolve(session, request)
    legacy = ResolvedConfig.model_validate(
        {
            k: v
            for k, v in resolved.as_json().items()
            if k not in ("execution_conventions", "dividend_policy")
        }
        | {"engine_version": "8A.1"}
    )
    assert legacy.conventions is ExecutionConventions.DAILY_V1
    assert legacy.dividend_policy is None
    assert "execution_conventions" not in legacy.as_json()
    assert legacy.execution_model_sha256 == backtest_model().definition_sha256()
    run_id = bench.service.create(request, actor="operator:test", resolved=legacy)
    bench.service.execute(run_id)
    original = completed(bench, run_id)
    assert original.engine_version == "8A.1"
    assert legacy.sha256() == original.config_sha256
    # No 8A.2 summary is written for an 8A.1 configuration without a pipeline.
    assert not [
        e for e in rows(bench, BacktestRunEventRecord, run_id) if e.kind == "PIPELINE_SUMMARY"
    ]
    copy = bench.service.reproduce(run_id, actor="operator:test")
    reproduced = completed(bench, copy)
    assert reproduced.reproduction_matches is True
    assert reproduced.result_sha256 == original.result_sha256
    assert reproduced.config["engine_version"] == "8A.1"
    assert "execution_conventions" not in reproduced.config


def test_dividends_are_credited_on_held_quantity_from_the_ex_date(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    seed_dividends(bench.factory, asset, "FIXA", D("0.05"))
    # A later revision retrieved after the run's knowledge time must not be used.
    later = datetime.now(UTC) + timedelta(days=30)  # after the run's knowledge time
    seed_dividends(
        bench.factory,
        asset,
        "FIXA",
        D("9.99"),
        retrieved_at=later,
        revision=1,
        coverage=False,
    )
    request = bench.request(risk=WIDE)
    run_id = bench.run(request)
    completed(bench, run_id)  # the books reconcile (cash conservation included)
    trades = rows(bench, BacktestTradeRecord, run_id)
    journal = rows(bench, BacktestJournalEntryRecord, run_id)
    dividend_entries = [j for j in journal if j.template == "cash_dividend"]
    assert dividend_entries, (
        summary(bench, run_id),
        [(t.seq, t.holding_sessions, t.status) for t in trades],
    )
    assert {j.entry_type for j in dividend_entries} == {"DIVIDEND"}
    per_trade = Counter(j.source_ref.split(":")[1] for j in dividend_entries)
    for t in trades:
        credited = Decimal(t.details["dividends"])
        postings = per_trade.get(str(t.seq), 0)
        # One dividend per session held over the previous close: none on the entry's own
        # session (bought ex-dividend), one on the exit session (held over its open).
        assert postings == (t.holding_sessions or 0)
        quantity = Decimal(t.entry_quantity)
        assert credited == (D("0.05") * quantity).quantize(D("0.01")) * postings
        assert (
            Decimal(t.net_pnl)
            == Decimal(t.realized_gross) + Decimal(t.unrealized) - Decimal(t.costs) + credited
        )
    for j in dividend_entries:
        assert j.params["rate"] == "0.05"  # revision 1 (retrieved later) never used
    details = summary(bench, run_id)["dividends"]
    assert details["assets_not_retrieved"] == []
    assert details["postings"]["credited"] == len(dividend_entries)
