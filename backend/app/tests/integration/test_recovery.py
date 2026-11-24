"""Completeness detection and recovery (2D) against real PostgreSQL.

MOCK PROVIDER: Alpaca's history is the dataset-backed fake in
``tests/fixtures/alpaca_history.py`` (constructed data, not market data, not a
connectivity test). The database, migrations, constraints, locks and transactions are
real. Real-provider recovery is verified separately (``tests/live``, opt-in).
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    CoverageEvidence,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    DataType,
    GapCause,
    GapRecoveryStatus,
    GapStatus,
    PriceAdjustment,
    RecoveryJobKind,
    RecoveryJobStatus,
    StreamGapKind,
)
from app.data_engine.live import trade_row
from app.data_engine.providers.alpaca.normalize import normalize_trade
from app.data_engine.recovery import RecoveryService
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.timeframes import Timeframe
from app.database.models import (
    AuditLog,
    CoverageRecord,
    DataQualityEventRecord,
    MarketBarRecord,
    MarketDataGapRecord,
    MarketTradeRecord,
    RecoveryJobRecord,
    StreamGapRecord,
)
from app.database.repositories import (
    CompletenessRepository,
    LiveObservationRepository,
    MarketBarRepository,
    RecoveryJobRepository,
    Series,
    SeriesKey,
    StreamRepository,
)
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca_history import (
    FakeHistory,
    history_bar,
    history_provider,
    history_trade,
    minutes,
)

pytestmark = pytest.mark.integration

# Tuesday 2026-03-10, 09:00 New York (EDT since Mar 8): IEX hours are 12:00-21:00 UTC.
NOW = datetime(2026, 3, 10, 13, 0, tzinfo=UTC)
MON = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)
TUE = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
M1 = Timeframe.parse("1Min")


@pytest.fixture
def factory(fresh_engine):
    return sessionmaker(bind=fresh_engine, expire_on_commit=False)


@pytest.fixture
def assets(factory):
    with session_scope(factory) as session:
        registered = register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    return {r.symbol: r.asset_id for r in registered}


def _settings(**values: Any) -> MarketDataSettings:
    base: dict[str, Any] = {
        "market_data_symbols": "AAPL,SPY",
        "market_data_recovery_timeframes": "1Min",
        "market_data_recovery_data_types": "bars",
        "market_data_completeness_lookback_days": 1,
        "historical_data_batch_size": 50,
    }
    return MarketDataSettings(_env_file=None, **(base | values))


def _service(factory, fake: FakeHistory, clock: FixedClock, **values) -> RecoveryService:
    return RecoveryService(
        factory, history_provider(fake, clock), _settings(**values), clock, worker_id="test"
    )


def _dataset(fake: FakeHistory, symbol: str = "AAPL", count: int = 20) -> list[datetime]:
    """Sparse minute bars on Monday and Tuesday morning (thin trading: most minutes empty)."""
    times = minutes(MON + timedelta(minutes=3), count, timedelta(minutes=17)) + minutes(
        TUE + timedelta(minutes=5), 5, timedelta(minutes=9)
    )
    fake.bars[(symbol, "1Min")] = [history_bar(at) for at in times]
    return times


def _gaps(factory, **where) -> list[MarketDataGapRecord]:
    with session_scope(factory) as session:
        stmt = select(MarketDataGapRecord).order_by(MarketDataGapRecord.id)
        for key, value in where.items():
            stmt = stmt.where(getattr(MarketDataGapRecord, key) == value)
        return list(session.scalars(stmt))


def _jobs(factory) -> list[RecoveryJobRecord]:
    with session_scope(factory) as session:
        return list(
            session.scalars(select(RecoveryJobRecord).order_by(RecoveryJobRecord.created_at))
        )


def _count(factory, model, *where) -> int:
    with session_scope(factory) as session:
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _actions(factory) -> list[str]:
    with session_scope(factory) as session:
        return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def _cycle(service: RecoveryService):
    return asyncio.run(service.run_cycle())


# ------------------------------------------------------------------ detection
def test_scan_finds_expected_missing_intervals_and_ignores_closures(factory, assets):
    clock = FixedClock(NOW)
    service = _service(factory, FakeHistory(), clock, market_data_completeness_lookback_days=3)
    service.scan(NOW)  # Sat 7th and Sun 8th are closed; Mon full day; Tue until settled
    aapl = _gaps(factory, provider_symbol="AAPL")
    assert [(g.gap_start, g.gap_end) for g in aapl] == [
        (MON, MON + timedelta(hours=9)),
        (TUE, NOW - timedelta(minutes=2)),
    ]
    assert {g.cause for g in aapl} == {GapCause.UNCOVERED}
    assert (
        _count(
            factory,
            DataQualityEventRecord,
            DataQualityEventRecord.event_type == DataQualityEventType.MISSING_INTERVAL,
        )
        == 4
    )  # two per symbol
    # Scanning again finds nothing new: unresolved gaps are not recorded twice.
    service.scan(NOW)
    assert len(_gaps(factory)) == 4


def test_holiday_is_not_missing_data(factory, assets):
    # Monday 2026-04-06 after Good Friday (Apr 3) and a weekend.
    monday = datetime(2026, 4, 6, 13, 0, tzinfo=UTC)
    service = _service(
        factory, FakeHistory(), FixedClock(monday), market_data_completeness_lookback_days=3
    )
    service.scan(monday)
    starts = {g.gap_start.date() for g in _gaps(factory)}
    assert starts == {monday.date()}  # nothing for Good Friday or the weekend


# ------------------------------------------------------------------ backfill
def test_backfill_recovers_and_verifies_a_missing_interval(factory, assets):
    fake = FakeHistory()
    times = _dataset(fake, "AAPL")
    _dataset(fake, "SPY", 3)
    clock = FixedClock(NOW)
    report = _cycle(_service(factory, fake, clock))
    assert report.counts["gaps_detected"] == 4
    assert report.counts["jobs_completed"] == 4
    gaps = _gaps(factory, provider_symbol="AAPL")
    assert {g.status for g in gaps} == {GapStatus.RECOVERED}
    assert sum(g.records_recovered for g in gaps) == len(times)
    jobs = [j for j in _jobs(factory) if j.provider_symbol == "AAPL"]
    assert {j.status for j in jobs} == {RecoveryJobStatus.COMPLETED}
    assert all(j.ingestion_job_ids and j.attempts == 1 for j in jobs)
    # Recovered bars are labelled as backfill: original availability unknown.
    with session_scope(factory) as session:
        bars = list(session.scalars(select(MarketBarRecord)))
    assert {b.source for b in bars} == {DataSource.HISTORICAL_REST}
    assert all(b.original_available_at is None for b in bars)
    # Minutes without bars inside the verified interval are not missing.
    service = _service(factory, fake, clock)
    tracked = service._tracked_symbol("AAPL", DataType.BAR, M1)
    state = service.evaluate(tracked, (MON, NOW), NOW)
    assert state["missing"] == []
    assert state["expected"]
    actions = _actions(factory)
    assert actions.count("market_data.gap_identified") == 4
    assert actions.count("market_data.backfill_initiated") == 4
    assert actions.count("market_data.backfill_succeeded") == 4
    recovered_events = _count(
        factory,
        DataQualityEventRecord,
        DataQualityEventRecord.event_type == DataQualityEventType.RECOVERY,
        DataQualityEventRecord.resulting_status == DataQualityStatus.RECOVERED,
    )
    assert recovered_events == 4


def test_repeated_backfill_creates_no_duplicates(factory, assets):
    fake = FakeHistory()
    times = _dataset(fake)
    clock = FixedClock(NOW)
    _cycle(_service(factory, fake, clock))
    bars = _count(factory, MarketBarRecord)
    assert bars == len(times)
    # An operator asks again for a verified range: nothing to do, no request sent.
    requests = len(fake.requests)
    service = _service(factory, fake, clock)
    assert service.request_recovery("AAPL", DataType.BAR, M1, MON, NOW, "test") == []
    assert len(fake.requests) == requests
    # Running the same retrieval again stores nothing new (identical bars are duplicates).
    from app.data_engine.historical import HistoricalFetchRequest

    historical = service._historical
    job = historical.create_job(
        HistoricalFetchRequest(symbols=("AAPL",), timeframe="1Min", start=MON, end=NOW),
        requested_by="test",
    )
    result = asyncio.run(historical.run_job(job.job_id))
    assert result.counters.records_stored == 0
    assert result.counters.records_duplicate == len(times)
    assert _count(factory, MarketBarRecord) == bars


def test_partial_recovery_keeps_the_remainder_missing_then_completes(factory, assets):
    fake = FakeHistory(page_size=10)
    times = _dataset(fake, count=25)  # Monday: 25 bars -> 3 pages
    fake.fail_after_pages["/v2/stocks/bars"] = (1, 500)  # the second page fails once
    clock = FixedClock(NOW)
    service = _service(factory, fake, clock, market_data_symbols="AAPL")
    _cycle(service)
    gaps = _gaps(factory)
    parent = next(g for g in gaps if g.gap_start == MON and g.parent_gap_id is None)
    child = next(g for g in gaps if g.parent_gap_id == parent.id)
    assert parent.status is GapStatus.PARTIALLY_RECOVERED
    assert parent.records_recovered == 10
    # The remainder starts just after the last bar actually received.
    assert child.gap_start == times[9] + timedelta(minutes=1)
    assert child.status is GapStatus.OPEN
    statuses = {j.status for j in _jobs(factory) if j.missing_start == MON}
    assert RecoveryJobStatus.PARTIALLY_COMPLETED in statuses
    # The next cycle recovers the remainder.
    _cycle(service)
    child = _gaps(factory, id=child.id)[0]
    assert child.status is GapStatus.RECOVERED
    assert _count(factory, MarketBarRecord) == len(times)


def test_permanent_refusal_is_recorded_as_unavailable(factory, assets):
    fake = FakeHistory()
    _dataset(fake)
    fake.faults["/v2/stocks/bars"] = [403, 403]  # both of AAPL's gaps are refused
    clock = FixedClock(NOW)
    _cycle(_service(factory, fake, clock, market_data_symbols="AAPL"))
    gaps = _gaps(factory)
    assert {g.status for g in gaps} == {GapStatus.UNAVAILABLE}
    unavailable = gaps
    assert all("403" in (g.resolution or "") for g in unavailable)
    assert {j.status for j in _jobs(factory) if j.gap_id in {g.id for g in unavailable}} == {
        RecoveryJobStatus.FAILED
    }
    assert _count(
        factory,
        DataQualityEventRecord,
        DataQualityEventRecord.event_type == DataQualityEventType.UNRECOVERABLE_INTERVAL,
        DataQualityEventRecord.resulting_status == DataQualityStatus.UNAVAILABLE,
    ) == len(unavailable)
    with session_scope(factory) as session:
        audit = session.scalars(
            select(AuditLog).where(AuditLog.action == "market_data.interval_unrecoverable")
        ).all()
    assert audit
    assert all(a.details["priority"] == "HIGH" for a in audit)


def test_transient_failures_retry_then_stay_missing(factory, assets):
    fake = FakeHistory()
    _dataset(fake)
    fake.faults["/v2/stocks/bars"] = [503] * 10
    clock = FixedClock(NOW)
    service = _service(
        factory, fake, clock, market_data_symbols="AAPL", market_data_recovery_max_attempts=2
    )
    _cycle(service)
    jobs = _jobs(factory)
    first = {j.id for j in jobs}
    assert {j.status for j in jobs} == {RecoveryJobStatus.PENDING}
    assert all(j.next_attempt_at > NOW for j in jobs)
    assert {g.status for g in _gaps(factory)} == {GapStatus.OPEN}
    clock.advance(timedelta(minutes=2))  # (the scan also finds the 2 newly settled minutes)
    _cycle(service)
    jobs = [j for j in _jobs(factory) if j.id in first]
    assert {j.status for j in jobs} == {RecoveryJobStatus.FAILED}
    assert all(j.attempts == 2 for j in jobs)
    gaps = [g for g in _gaps(factory) if g.id in {j.gap_id for j in jobs}]
    assert {g.status for g in gaps} == {GapStatus.OPEN}  # never reported as recovered
    assert all("failed after 2 attempt(s)" in (g.resolution or "") for g in gaps)
    assert "market_data.backfill_failed" in _actions(factory)


def test_rate_limited_requests_are_retried_by_the_provider_client(factory, assets):
    fake = FakeHistory()
    times = _dataset(fake)
    fake.faults["/v2/stocks/bars"] = [429]
    clock = FixedClock(NOW)
    service = RecoveryService(
        factory,
        history_provider(fake, clock, max_retries=2),
        _settings(market_data_symbols="AAPL"),
        clock,
    )
    _cycle(service)
    assert {g.status for g in _gaps(factory)} == {GapStatus.RECOVERED}
    assert _count(factory, MarketBarRecord) == len(times)


def test_invalid_history_is_rejected_and_never_reported_complete(factory, assets):
    fake = FakeHistory()
    _dataset(fake, count=4)
    broken = history_bar(MON + timedelta(minutes=30))
    del broken["c"]  # required field missing
    fake.bars[("AAPL", "1Min")].append(broken)
    clock = FixedClock(NOW)
    _cycle(_service(factory, fake, clock, market_data_symbols="AAPL"))
    monday = next(g for g in _gaps(factory) if g.gap_start == MON)
    assert monday.status is GapStatus.PARTIALLY_RECOVERED
    assert monday.records_rejected == 1
    assert "invalid" in (monday.resolution or "")
    job = next(j for j in _jobs(factory) if j.gap_id == monday.id)
    assert job.status is RecoveryJobStatus.PARTIALLY_COMPLETED


# ------------------------------------------------------------------ live interruptions
def _stream_gap(factory, kind, start, end, channels=("bars", "trades", "quotes")) -> int:
    with session_scope(factory) as session:
        record = StreamRepository(session).open_gap(
            provider="alpaca",
            feed="iex",
            kind=kind,
            gap_start=start,
            gap_end=end,
            reason="test interruption",
            symbols=["AAPL", "SPY"],
            channels=list(channels),
            detected_at=end,
            previous_session_id=None,
            last_message_before=start,
        )
        return record.id


def test_outage_while_the_market_is_closed_loses_nothing(factory, assets):
    saturday = datetime(2026, 3, 7, 15, tzinfo=UTC)
    stream_gap = _stream_gap(
        factory, StreamGapKind.WORKER_NOT_RUNNING, saturday, saturday + timedelta(hours=20)
    )
    service = _service(factory, FakeHistory(), FixedClock(NOW))
    assert service.translate_stream_gaps(NOW) == 1
    assert _gaps(factory) == []
    with session_scope(factory) as session:
        record = session.get(StreamGapRecord, stream_gap)
        assert record is not None
        assert record.recovery_status is GapRecoveryStatus.RECOVERED
    assert "market_data.outage_no_expected_data" in _actions(factory)


def test_stream_outage_is_recovered_per_data_type_and_quotes_stay_missing(factory, assets):
    fake = FakeHistory()
    outage = (TUE + timedelta(minutes=10), TUE + timedelta(minutes=20))
    fake.bars[("AAPL", "1Min")] = [history_bar(TUE + timedelta(minutes=m)) for m in (9, 12, 15)]
    fake.trades["AAPL"] = [
        history_trade(TUE + timedelta(minutes=11, seconds=i), i) for i in range(1, 6)
    ]
    stream_gap = _stream_gap(factory, StreamGapKind.CONNECTION_LOST, *outage)
    clock = FixedClock(NOW)
    service = _service(
        factory,
        fake,
        clock,
        market_data_symbols="AAPL",
        market_data_recovery_data_types="bars,trades",
        market_data_completeness_lookback_days=0,
    )
    _cycle(service)
    outage_gaps = _gaps(factory, stream_gap_id=stream_gap)
    by_type = {(g.provider_symbol, g.data_type): g for g in outage_gaps}
    bar_gap = by_type[("AAPL", DataType.BAR)]
    assert (bar_gap.gap_start, bar_gap.gap_end) == (outage[0] - timedelta(minutes=1), outage[1])
    assert bar_gap.cause is GapCause.CONNECTION_LOST
    assert bar_gap.status is GapStatus.RECOVERED
    assert bar_gap.records_recovered == 3
    assert by_type[("AAPL", DataType.TRADE)].records_recovered == 5
    quote_gap = by_type[("AAPL", DataType.QUOTE)]
    assert quote_gap.status is GapStatus.OPEN
    assert quote_gap.details["recovery_disabled"]
    # SPY was in the interruption too; its gaps exist but nothing recovered without data.
    with session_scope(factory) as session:
        record = session.get(StreamGapRecord, stream_gap)
        assert record is not None
        assert record.recovery_status is GapRecoveryStatus.PARTIALLY_RECOVERED  # quotes missing
    assert "market_data.outage_detected" in _actions(factory)


def test_reconciliation_finds_trades_the_stream_never_delivered(factory, assets):
    fake = FakeHistory()
    window = (TUE, TUE + timedelta(minutes=30))
    history = [history_trade(TUE + timedelta(minutes=1, seconds=i), i + 1) for i in range(8)]
    fake.trades["AAPL"] = history
    received_at = TUE + timedelta(minutes=2)
    rows = [
        trade_row(
            normalize_trade(
                {**item, "S": "AAPL", "p": Decimal(item["p"])}, feed="iex", received_at=received_at
            ),
            assets["AAPL"],
            provider="alpaca",
            feed="iex",
            status=DataQualityStatus.VALID,
            now=received_at,
            raw_id=None,  # type: ignore[arg-type]
        )
        for item in history[:5]  # the stream delivered 5 of the 8 trades
    ]
    with session_scope(factory) as session:
        LiveObservationRepository(session).insert_trades(rows)
        series = Series(assets["AAPL"], "alpaca", "iex", DataType.TRADE)
        session.execute(
            text(
                "INSERT INTO market_data_stream_sessions (id, provider, feed, worker_id, state, "
                "url, started_at, subscribed_at, ended_at) VALUES (:id, 'alpaca', 'iex', 'w', "
                "'STOPPED', 'wss://test', :s, :s, :e)"
            ),
            {"id": (session_id := uuid.uuid4()), "s": window[0], "e": window[1]},
        )
        CompletenessRepository(session).add_coverage(
            series, window, CoverageEvidence.LIVE_STREAM, at=window[1], stream_session_id=session_id
        )
    clock = FixedClock(datetime(2026, 3, 10, 12, 33, tzinfo=UTC))
    service = _service(
        factory,
        fake,
        clock,
        market_data_symbols="AAPL",
        market_data_recovery_data_types="trades",
        market_data_recovery_timeframes="1Day",
        market_data_completeness_lookback_days=0,
    )
    _cycle(service)
    reconciliations = [j for j in _jobs(factory) if j.kind is RecoveryJobKind.RECONCILIATION]
    assert len(reconciliations) == 1
    job = reconciliations[0]
    assert (job.missing_start, job.missing_end) == window
    assert job.status is RecoveryJobStatus.COMPLETED
    assert (job.records_recovered, job.records_duplicate) == (3, 5)
    missed = _gaps(factory, cause=GapCause.STREAM_MISSED_DATA)
    assert len(missed) == 1
    assert missed[0].status is GapStatus.RECOVERED
    assert missed[0].records_recovered == 3
    assert _count(factory, MarketTradeRecord) == 8
    assert (
        _count(factory, MarketTradeRecord, MarketTradeRecord.source == DataSource.HISTORICAL_REST)
        == 3
    )
    assert "market_data.stream_missed_data" in _actions(factory)
    # The reconciled window is now verified by the provider's history: not scheduled again.
    _cycle(service)
    assert len([j for j in _jobs(factory) if j.kind is RecoveryJobKind.RECONCILIATION]) == 1


# ------------------------------------------------------------------ revisions
def test_backfill_overlapping_live_bars_never_rewrites_decision_time_data(factory, assets):
    minute = TUE + timedelta(minutes=5)
    key = SeriesKey(assets["AAPL"], "1Min", "alpaca", "iex", PriceAdjustment.RAW)
    live_at = minute + timedelta(minutes=1, seconds=1)
    with session_scope(factory) as session:
        repo = MarketBarRepository(session)
        repo.insert(
            [
                {
                    "asset_id": assets["AAPL"],
                    "provider": "alpaca",
                    "feed": "iex",
                    "provider_symbol": "AAPL",
                    "timeframe": "1Min",
                    "adjustment": PriceAdjustment.RAW,
                    "event_time": minute,
                    "revision": 0,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 50,
                    "vwap": 100.2,
                    "trade_count": 2,
                    "is_complete": True,
                    "source": DataSource.LIVE_STREAM,
                    "observation_kind": "LIVE_INITIAL",
                    "quality_status": DataQualityStatus.VALID,
                    "received_at": live_at,
                    "available_at": live_at,
                    "original_available_at": live_at,
                    "processed_at": live_at,
                }
            ]
        )
        decision_time = session.scalar(select(func.now()))
    fake = FakeHistory()
    fake.bars[("AAPL", "1Min")] = [history_bar(minute, close="100.40", volume=80)]  # corrected
    clock = FixedClock(NOW)
    service = _service(
        factory, fake, clock, market_data_symbols="AAPL", market_data_completeness_lookback_days=0
    )
    service.request_recovery("AAPL", DataType.BAR, M1, TUE, TUE + timedelta(minutes=30), "test")
    _cycle(service)
    with session_scope(factory) as session:
        repo = MarketBarRepository(session)
        revisions = repo.revisions(key, minute)
        then = repo.current_bars(
            key,
            start=minute,
            end=minute,
            statuses=[DataQualityStatus.VALID],
            known_at=decision_time,
        )
        current = repo.current_bars(
            key, start=minute, end=minute, statuses=[DataQualityStatus.VALID]
        )
    assert [(r.revision, r.source) for r in revisions] == [
        (0, DataSource.LIVE_STREAM),
        (1, DataSource.HISTORICAL_REST),
    ]
    assert str(revisions[0].close) == "100.5"  # the original observation is untouched
    assert [str(b.close) for b in then] == ["100.5"]  # what Atlas knew at decision time
    assert [str(b.close) for b in current] == ["100.40"]  # latest accepted revision
    assert revisions[1].available_at >= NOW  # available only from the recovery onwards
    assert _gaps(factory)[0].records_revised == 1


# ------------------------------------------------------------------ concurrency
def test_concurrent_workers_never_run_the_same_job_twice(factory, assets):
    fake = FakeHistory()
    _dataset(fake, "AAPL")
    _dataset(fake, "SPY")
    clock = FixedClock(NOW)
    first = _service(factory, fake, clock)
    first.scan(NOW)
    assert first.schedule(NOW) == 4
    second = _service(factory, fake, clock)
    second.worker_id = "test-2"
    results: list[list[str]] = []

    def run(service: RecoveryService) -> None:
        results.append(asyncio.run(service.run_due(max_jobs=10)))

    threads = [threading.Thread(target=run, args=(s,)) for s in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(o for r in results for o in r) == ["COMPLETED"] * 4
    jobs = _jobs(factory)
    assert all(j.attempts == 1 and len(j.ingestion_job_ids) == 1 for j in jobs)
    assert _count(factory, MarketBarRecord) == 50  # 25 per symbol, none twice


def test_concurrent_overlapping_detections_never_overlap(factory, assets):
    series = Series(assets["AAPL"], "alpaca", "iex", DataType.BAR, "1Min", PriceAdjustment.RAW)
    barrier = threading.Barrier(4)

    def detect(offset: int) -> None:
        barrier.wait()
        with session_scope(factory) as session:
            repo = CompletenessRepository(session)
            repo.lock_series(series)
            start = TUE + timedelta(minutes=offset)
            repo.record_gaps(
                series, "AAPL", [(start, start + timedelta(minutes=20))], GapCause.REQUESTED, at=NOW
            )

    threads = [threading.Thread(target=detect, args=(o,)) for o in (0, 5, 10, 15)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    gaps = sorted((g.gap_start, g.gap_end) for g in _gaps(factory))
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(gaps))
    assert gaps[0][0] == TUE
    assert gaps[-1][1] == TUE + timedelta(minutes=35)


def test_database_constraints_enforce_disjoint_gaps_and_one_active_job(factory, assets):
    series = Series(assets["AAPL"], "alpaca", "iex", DataType.BAR, "1Min", PriceAdjustment.RAW)
    with session_scope(factory) as session:
        gap = CompletenessRepository(session).record_gaps(
            series, "AAPL", [(TUE, TUE + timedelta(minutes=10))], GapCause.REQUESTED, at=NOW
        )[0]
        gap_id = gap.id
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        # Bypassing the repository: the exclusion constraint still refuses the overlap.
        session.add(
            MarketDataGapRecord(
                asset_id=assets["AAPL"],
                provider="alpaca",
                feed="iex",
                provider_symbol="AAPL",
                data_type=DataType.BAR,
                timeframe="1Min",
                gap_start=TUE + timedelta(minutes=5),
                gap_end=TUE + timedelta(minutes=15),
                cause=GapCause.REQUESTED,
                detected_at=NOW,
            )
        )
    service = _service(factory, FakeHistory(), FixedClock(NOW))
    assert service.schedule(NOW) == 1
    with session_scope(factory) as session:
        tracked = service._tracked_symbol("AAPL", DataType.BAR, M1)
        duplicate = service._job(
            tracked, RecoveryJobKind.RECOVERY, gap_id, (TUE, TUE + timedelta(minutes=10)), NOW
        )
        assert RecoveryJobRepository(session).create(duplicate) is None
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.add(
            service._job(
                tracked, RecoveryJobKind.RECOVERY, gap_id, (TUE, TUE + timedelta(minutes=10)), NOW
            )
        )


def test_job_creation_keeps_working_after_many_inserts_on_one_connection(factory, assets):
    """D-227: with the arbiter predicate sent as bound parameters, the eleventh insert on a
    connection failed ("no unique or exclusion constraint matching the ON CONFLICT
    specification") once the driver had prepared the statement and PostgreSQL tried a generic
    plan. A backlog of more than ten gaps then failed every recovery cycle."""
    series = Series(assets["AAPL"], "alpaca", "iex", DataType.BAR, "1Min", PriceAdjustment.RAW)
    with session_scope(factory) as session:
        gap = CompletenessRepository(session).record_gaps(
            series, "AAPL", [(TUE, TUE + timedelta(minutes=10))], GapCause.REQUESTED, at=NOW
        )[0]
        gap_id = gap.id
    service = _service(factory, FakeHistory(), FixedClock(NOW))
    with session_scope(factory) as session:  # one connection for every insert
        tracked = service._tracked_symbol("AAPL", DataType.BAR, M1)
        repo = RecoveryJobRepository(session)
        created = [
            repo.create(
                service._job(
                    tracked,
                    RecoveryJobKind.RECOVERY,
                    gap_id,
                    (TUE, TUE + timedelta(minutes=10)),
                    NOW,
                )
            )
            for _ in range(25)
        ]
    # The first is created; the other 24 conflict with the active job and are refused quietly.
    assert created[0] is not None
    assert all(job is None for job in created[1:])
    assert len(_jobs(factory)) == 1


# ------------------------------------------------------------------ restarts
def test_interrupted_job_runs_again_after_a_worker_restart(factory, assets):
    fake = FakeHistory()
    times = _dataset(fake)
    clock = FixedClock(NOW)
    crashed = _service(factory, fake, clock, market_data_symbols="AAPL")
    crashed.scan(NOW)
    crashed.schedule(NOW)
    with session_scope(factory) as session:
        claimed = RecoveryJobRepository(session).claim_due(
            provider="alpaca",
            feed="iex",
            now=NOW,
            owner="crashed",
            lease_until=NOW + timedelta(minutes=15),
            limit=10,
        )
    assert claimed  # RUNNING, and the worker "dies" here
    restarted = _service(factory, fake, clock, market_data_symbols="AAPL")
    assert restarted.recover_interrupted() == len(claimed)
    _cycle(restarted)
    assert {j.status for j in _jobs(factory)} == {RecoveryJobStatus.COMPLETED}
    assert _count(factory, MarketBarRecord) == len(times)


def test_recovery_worker_disabled_by_default_and_once_mode(factory, assets):
    from app.database.repositories import RecoveryStatusRepository
    from app.workers.market_data_recovery import EXIT_OK, RecoveryWorker

    fake = FakeHistory()
    _dataset(fake)
    clock = FixedClock(NOW)
    disabled = RecoveryWorker(
        session_factory=factory,
        provider=history_provider(fake, clock),
        settings=_settings(market_data_symbols="AAPL"),
        clock=clock,
    )
    assert asyncio.run(disabled.run(asyncio.Event())) == EXIT_OK
    with session_scope(factory) as session:
        status = RecoveryStatusRepository(session).get("alpaca", "iex")
        assert status is not None
        assert status.state == "DISABLED"
    assert fake.requests == []  # disabled means no provider calls
    once = RecoveryWorker(
        session_factory=factory,
        provider=history_provider(fake, clock),
        settings=_settings(market_data_symbols="AAPL"),
        clock=clock,
    )
    assert asyncio.run(once.run(asyncio.Event(), once=True)) == EXIT_OK
    with session_scope(factory) as session:
        status = RecoveryStatusRepository(session).get("alpaca", "iex")
        assert status is not None
        assert status.state == "STOPPED"
        assert status.counters["jobs_completed"] == 2
        assert status.last_successful_backfill_at is not None
    assert (
        _count(factory, CoverageRecord, CoverageRecord.evidence == CoverageEvidence.REST_RETRIEVAL)
        == 2
    )


# ------------------------------------------------------------------ API
def _client(
    factory,
    fake: FakeHistory,
    clock: FixedClock,
    make_settings,
    health_checks=None,
    **settings_overrides,
):
    from fastapi.testclient import TestClient

    from app.data_engine.historical import HistoricalIngestionService
    from app.data_engine.runtime import MarketDataContext
    from app.main import create_app

    settings = _settings(market_data_symbols="AAPL")
    provider = history_provider(fake, clock)
    context = MarketDataContext(
        settings=settings,
        provider=provider,
        service=HistoricalIngestionService(factory, provider, settings, clock),
        session_factory=factory,
    )
    app = create_app(
        make_settings(**settings_overrides),
        clock=clock,
        health_checks=health_checks or {},
        market_data=context,
    )
    return TestClient(app)


def test_completeness_gaps_and_jobs_api(factory, assets, make_settings):
    fake = FakeHistory()
    _dataset(fake)
    clock = FixedClock(NOW)
    with _client(factory, fake, clock, make_settings) as client:
        params = {
            "symbol": "AAPL",
            "timeframe": "1Min",
            "start": MON.isoformat(),
            "end": NOW.isoformat(),
        }
        before = client.get("/api/v1/market-data/completeness", params=params).json()
        assert before["status"] == "MISSING"
        assert before["missing"]
        weekend = client.get(
            "/api/v1/market-data/completeness",
            params=params | {"start": "2026-03-07T00:00:00Z", "end": "2026-03-09T04:00:00Z"},
        ).json()
        assert weekend["status"] == "NOT_EXPECTED"
        accepted = client.post(
            "/api/v1/market-data/recovery-requests",
            json={
                "symbol": "AAPL",
                "timeframe": "1Min",
                "start": MON.isoformat(),
                "end": NOW.isoformat(),
            },
        )
        assert accepted.status_code == 202
        assert len(accepted.json()["gaps"]) == 2
        assert {g["cause"] for g in accepted.json()["gaps"]} == {"REQUESTED"}
        _cycle(
            _service(
                factory,
                fake,
                clock,
                market_data_symbols="AAPL",
                market_data_completeness_lookback_days=0,
            )
        )
        after = client.get("/api/v1/market-data/completeness", params=params).json()
        assert after["status"] == "COMPLETE"
        assert after["missing"] == []
        assert after["known_gaps"] == []
        gaps = client.get("/api/v1/market-data/gaps", params={"symbol": "AAPL"}).json()
        assert {g["status"] for g in gaps} == {"RECOVERED"}
        jobs = client.get("/api/v1/market-data/recovery-jobs").json()
        assert jobs
        assert {j["status"] for j in jobs} == {"COMPLETED"}
        metrics = client.get("/api/v1/market-data/metrics").json()
        assert metrics["completeness"]["recovery_jobs"]["COMPLETED"] == len(jobs)
        assert metrics["completeness"]["records_recovered"]["BAR"] == 25
        bad = client.get("/api/v1/market-data/completeness", params=params | {"timeframe": "1Week"})
        assert bad.status_code == 422


def _write_stream_status(factory, now: datetime, **metrics) -> None:
    with session_scope(factory) as session:
        StreamRepository(session).upsert_status(
            "alpaca",
            "iex",
            {
                "worker_id": "w",
                "enabled": True,
                "connection_state": "SUBSCRIBED",
                "auth_state": "AUTHENTICATED",
                "subscription_state": "ACTIVE",
                "requested_subscription": {"bars": ["AAPL"]},
                "confirmed_subscription": {"bars": ["AAPL"]},
                "heartbeat_at": now,
                "last_subscribed_at": now - timedelta(minutes=5),
                "last_message_at": now - timedelta(seconds=1),
                "last_persisted_at": now - timedelta(seconds=1),
                "metrics": {"persistence_failing": False, **metrics},
            },
        )


def test_pipeline_health_separates_connectivity_from_persistence(factory, assets, make_settings):
    clock = FixedClock(datetime(2026, 3, 10, 15, 0, tzinfo=UTC))  # regular session
    with _client(factory, FakeHistory(), clock, make_settings) as client:
        _write_stream_status(factory, clock.now())
        healthy = client.get("/api/v1/market-data/pipeline-health").json()
        assert healthy["stream_health"] == "HEALTHY"
        dims = {d["name"]: d["state"] for d in healthy["dimensions"]}
        assert dims["database_persistence"] == "OK"
        assert healthy["pipeline_health"] in ("HEALTHY", "DEGRADED")  # backfill is informative
        # Connected and authenticated, but writes are failing: not a healthy pipeline.
        _write_stream_status(
            factory,
            clock.now(),
            persistence_failing=True,
            persistence_failing_since=clock.now().isoformat(),
        )
        failing = client.get("/api/v1/market-data/pipeline-health").json()
        dims = {d["name"]: d["state"] for d in failing["dimensions"]}
        assert dims["provider_connection"] == "OK"
        assert dims["authentication"] == "OK"
        assert dims["database_persistence"] == "FAILING"
        assert failing["pipeline_health"] == "FAILING"
        assert failing["stream_health"] == "DEGRADED"


def test_redis_interruption_does_not_affect_the_market_data_pipeline(
    factory, assets, make_settings
):
    from app.monitoring.health import ComponentHealth, HealthStatus

    def redis_down() -> ComponentHealth:
        return ComponentHealth(name="redis", status=HealthStatus.ERROR, latency_ms=0, detail="down")

    clock = FixedClock(datetime(2026, 3, 10, 15, 0, tzinfo=UTC))
    with _client(factory, FakeHistory(), clock, make_settings, {"redis": redis_down}) as client:
        _write_stream_status(factory, clock.now())
        assert client.get("/ready").json()["status"] == "not_ready"
        pipeline = client.get("/api/v1/market-data/pipeline-health").json()
        dims = {d["name"]: d["state"] for d in pipeline["dimensions"]}
        # Redis holds nothing authoritative: persistence goes to PostgreSQL only.
        assert dims["database_persistence"] == "OK"
        assert pipeline["pipeline_health"] != "FAILING"


def test_recovery_requests_require_the_operator_token(factory, assets, make_settings):
    token = "operator-token-for-tests-0001"  # test value
    body = {"symbol": "AAPL", "timeframe": "1Min", "start": MON.isoformat(), "end": NOW.isoformat()}
    clock = FixedClock(NOW)
    with _client(factory, FakeHistory(), clock, make_settings, operator_token=token) as client:
        missing = client.post("/api/v1/market-data/recovery-requests", json=body)
        wrong = client.post(
            "/api/v1/market-data/recovery-requests",
            json=body,
            headers={"X-Atlas-Operator-Token": "wrong"},
        )
        ok = client.post(
            "/api/v1/market-data/recovery-requests",
            json=body,
            headers={"X-Atlas-Operator-Token": token},
        )
    assert (missing.status_code, wrong.status_code, ok.status_code) == (401, 401, 202)
    with _client(
        factory,
        FakeHistory(),
        clock,
        make_settings,
        environment="paper",
        postgres_password="Unique-Pw-123",
    ) as client:
        assert client.post("/api/v1/market-data/recovery-requests", json=body).status_code == 403
