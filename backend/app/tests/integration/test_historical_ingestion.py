"""Historical ingestion against real PostgreSQL with a FAKE Alpaca server.

The provider responses are constructed fixtures (see tests/fixtures/alpaca.py); the
database, migrations, constraints, triggers and transactions are real.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.enums import (
    DataQualityEventType,
    DataQualityStatus,
    IngestionJobStatus,
    IngestionSymbolStatus,
    PriceAdjustment,
)
from app.data_engine.errors import UnsupportedCapabilityError
from app.data_engine.historical import (
    HistoricalFetchRequest,
    HistoricalIngestionService,
    IngestionJobStateError,
    UnknownAssetError,
)
from app.data_engine.reference_assets import (
    ReferenceDataError,
    load_reference_assets,
    register_assets,
)
from app.data_engine.schemas import DataIngestionJob
from app.database.models import (
    Asset,
    AuditLog,
    DataQualityEventRecord,
    MarketBarRecord,
    RawMarketDataEventRecord,
)
from app.database.repositories import MarketBarRepository, SeriesKey
from app.database.sessions import session_scope
from app.tests.fixtures.alpaca import FakeAlpaca, fake_provider, fixture_bar

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=UTC)
# Daily bars start at midnight New York (05:00 UTC in winter).
DAYS = [datetime(2026, 3, d, 5, 0, tzinfo=UTC) for d in (2, 3, 4, 5, 6)]
START, END = DAYS[0], DAYS[-1]


@pytest.fixture
def factory(fresh_engine):
    return sessionmaker(bind=fresh_engine, expire_on_commit=False)


@pytest.fixture
def assets(factory):
    with session_scope(factory) as session:
        registered = register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    return {r.symbol: r.asset_id for r in registered}


def _service(factory, fake: FakeAlpaca, **provider_settings) -> HistoricalIngestionService:
    settings = MarketDataSettings(_env_file=None, historical_data_batch_size=2)
    provider = fake_provider(fake, now=NOW, **provider_settings)
    return HistoricalIngestionService(factory, provider, settings, FixedClock(NOW))


def _request(**overrides) -> HistoricalFetchRequest:
    values = {"symbols": ("AAPL",), "timeframe": "1Day", "start": START, "end": END}
    return HistoricalFetchRequest(**(values | overrides))


def _run(service, request):
    job = service.create_job(request, requested_by="test")
    return asyncio.run(service.run_job(job.job_id))


def _bars(factory, **filters):
    with session_scope(factory) as session:
        stmt = select(MarketBarRecord).order_by(
            MarketBarRecord.event_time, MarketBarRecord.revision
        )
        for column, value in filters.items():
            stmt = stmt.where(getattr(MarketBarRecord, column) == value)
        return list(session.scalars(stmt))


def _events(factory, event_type=None):
    with session_scope(factory) as session:
        stmt = select(DataQualityEventRecord)
        if event_type is not None:
            stmt = stmt.where(DataQualityEventRecord.event_type == event_type)
        return list(session.scalars(stmt))


# ------------------------------------------------------------------ happy path
def test_job_lifecycle_persists_exact_normalized_bars(factory, assets):
    fake = (
        FakeAlpaca()
        .add_page(
            {
                "AAPL": [
                    fixture_bar(
                        DAYS[0], o="187.00", h="187.50", low="186.90", c="187.1234", vw="187.2"
                    ),
                    fixture_bar(DAYS[1]),
                ]
            },
            "p2",
        )
        .add_page({"AAPL": [fixture_bar(DAYS[2], v=0)]})
        .add_page({"SPY": [fixture_bar(DAYS[0], o="512.01", h="515.5", low="510.25", c="514.99")]})
    )
    service = _service(factory, fake)
    created = service.create_job(_request(symbols=("AAPL", "SPY")), requested_by="test")
    assert created.status is IngestionJobStatus.PENDING
    assert created.adjustment is PriceAdjustment.RAW
    assert created.feed == "iex"

    job = asyncio.run(service.run_job(created.job_id))

    assert job.status is IngestionJobStatus.COMPLETED
    assert job.counters.pages_retrieved == 3
    assert job.counters.records_retrieved == 4
    assert job.counters.records_validated == 4
    assert job.counters.records_stored == 4
    assert job.counters.records_rejected == 0
    assert [s.status for s in job.symbols] == [IngestionSymbolStatus.COMPLETED] * 2
    aapl = next(s for s in job.symbols if s.provider_symbol == "AAPL")
    assert aapl.counters.pages_retrieved == 2
    assert (aapl.first_event_at, aapl.last_event_at) == (DAYS[0], DAYS[2])
    assert job.completed_at is not None

    bars = _bars(factory, asset_id=assets["AAPL"])
    assert [b.event_time for b in bars] == DAYS[:3]
    first = bars[0]
    assert first.close == Decimal("187.1234")
    assert first.event_time.tzinfo is not None
    assert first.event_time.utcoffset() == timedelta(0)
    assert (first.provider, first.feed, first.adjustment) == ("alpaca", "iex", PriceAdjustment.RAW)
    assert first.source.value == "HISTORICAL_REST"
    assert first.original_available_at is None
    assert first.received_at == NOW
    assert first.available_at == NOW
    assert first.quality_status is DataQualityStatus.VALID
    assert first.revision == 0
    assert first.ingestion_job_id == job.job_id
    assert first.raw_event_id is not None
    assert bars[2].volume == Decimal(0)  # genuine zero preserved

    with session_scope(factory) as session:
        raws = list(session.scalars(select(RawMarketDataEventRecord)))
    assert len(raws) == 3
    assert all(len(r.payload_sha256) == 64 for r in raws)
    assert "187.1234" in raws[0].payload_text  # verbatim response kept


def test_missing_volume_is_stored_as_null_with_a_quality_note(factory, assets):
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0], v=None)]})
    _run(_service(factory, fake), _request())

    (bar,) = _bars(factory)
    assert bar.volume is None
    assert bar.quality_status is DataQualityStatus.VALID
    assert _events(factory, DataQualityEventType.MISSING_FIELD)


# ------------------------------------------------------------------ idempotency and revisions
def test_repeating_a_request_stores_nothing_new(factory, assets):
    page = {"AAPL": [fixture_bar(d) for d in DAYS[:3]]}
    _run(_service(factory, FakeAlpaca().add_page(page)), _request())
    second = _run(_service(factory, FakeAlpaca().add_page(page)), _request())

    assert second.status is IngestionJobStatus.COMPLETED
    assert second.counters.records_stored == 0
    assert second.counters.records_duplicate == 3
    assert len(_bars(factory)) == 3


def test_overlapping_ranges_only_add_new_bars(factory, assets):
    _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(d) for d in DAYS[:3]]})),
        _request(end=DAYS[2]),
    )
    overlap = _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(d) for d in DAYS[1:]]})),
        _request(start=DAYS[1]),
    )
    assert overlap.counters.records_duplicate == 2
    assert overlap.counters.records_stored == 2
    assert [b.event_time for b in _bars(factory)] == DAYS


def test_changed_bar_becomes_a_revision_and_history_is_kept(factory, assets):
    _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0], c="100.20")]})),
        _request(),
    )
    with session_scope(factory) as session:
        between = session.execute(text("SELECT clock_timestamp()")).scalar_one()
    corrected = _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0], c="100.30")]})),
        _request(),
    )

    assert corrected.counters.records_revised == 1
    rows = _bars(factory)
    assert [(r.revision, r.close) for r in rows] == [(0, Decimal("100.20")), (1, Decimal("100.30"))]
    (correction,) = _events(factory, DataQualityEventType.CORRECTION)
    assert correction.details["previous"]["close"] == "100.20"
    assert correction.details["current"]["close"] == "100.30"

    key = SeriesKey(assets["AAPL"], "1Day", "alpaca", "iex", PriceAdjustment.RAW)
    with session_scope(factory) as session:
        repo = MarketBarRepository(session)
        current = repo.current_bars(key, start=START, end=END, statuses=[DataQualityStatus.VALID])
        as_known = repo.current_bars(
            key, start=START, end=END, statuses=[DataQualityStatus.VALID], known_at=between
        )
    assert [b.close for b in current] == [Decimal("100.30")]
    assert [b.close for b in as_known] == [Decimal("100.20")]  # point-in-time view


def test_concurrent_overlapping_jobs_never_duplicate(factory, assets):
    page = {"AAPL": [fixture_bar(d) for d in DAYS]}
    services = [_service(factory, FakeAlpaca().add_page(page)) for _ in range(2)]
    jobs = [s.create_job(_request(), requested_by="test") for s in services]
    results: list[DataIngestionJob] = []

    def run(service, job_id):
        results.append(asyncio.run(service.run_job(job_id)))

    threads = [
        threading.Thread(target=run, args=(s, j.job_id))
        for s, j in zip(services, jobs, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(_bars(factory)) == len(DAYS)
    assert sum(r.counters.records_stored for r in results) == len(DAYS)
    assert sum(r.counters.records_duplicate for r in results) == len(DAYS)


# ------------------------------------------------------------------ validation
def test_invalid_bars_are_quarantined_or_rejected_never_silently_accepted(factory, assets):
    fake = FakeAlpaca().add_page(
        {
            "AAPL": [
                fixture_bar(DAYS[0]),
                fixture_bar(DAYS[1], h="90", low="99.9"),  # impossible OHLC -> quarantined
                fixture_bar(DAYS[2] + timedelta(hours=3)),  # not on the daily grid -> quarantined
                {
                    "t": DAYS[3].isoformat().replace("+00:00", "Z"),
                    "h": 1,
                    "l": 1,
                    "c": 1,
                },  # malformed
                fixture_bar(END + timedelta(days=3)),  # outside requested range -> rejected
            ]
        }
    )
    job = _run(_service(factory, fake), _request())

    assert job.status is IngestionJobStatus.COMPLETED
    assert job.counters.records_retrieved == 5
    assert job.counters.records_rejected == 2
    assert job.counters.records_validated == 3
    assert job.counters.records_stored == 3
    assert job.counters.records_quarantined == 2
    statuses = {b.event_time: b.quality_status for b in _bars(factory)}
    assert statuses[DAYS[0]] is DataQualityStatus.VALID
    assert statuses[DAYS[1]] is DataQualityStatus.QUARANTINED
    assert DataQualityEventType.INVALID_OHLC in {e.event_type for e in _events(factory)}
    assert _events(factory, DataQualityEventType.TIMEFRAME_MISALIGNED)
    assert _events(factory, DataQualityEventType.MISSING_FIELD)
    assert _events(factory, DataQualityEventType.OUT_OF_REQUESTED_RANGE)

    key = SeriesKey(assets["AAPL"], "1Day", "alpaca", "iex", PriceAdjustment.RAW)
    with session_scope(factory) as session:
        served = MarketBarRepository(session).current_bars(
            key, start=START, end=END + timedelta(days=5), statuses=[DataQualityStatus.VALID]
        )
    assert [b.event_time for b in served] == [DAYS[0]]


def test_wrong_symbol_and_currency_mismatch_are_rejected(factory, assets):
    fake = (
        FakeAlpaca()
        .add_page({"AAPL": [fixture_bar(DAYS[0])], "MSFT": [fixture_bar(DAYS[0])]}, "p2")
        .add_page({"AAPL": [fixture_bar(DAYS[1])]}, currency="EUR")
    )
    job = _run(_service(factory, fake), _request())

    assert job.counters.records_stored == 1
    assert job.counters.records_rejected == 2
    assert _events(factory, DataQualityEventType.UNEXPECTED_SYMBOL)
    assert _events(factory, DataQualityEventType.CURRENCY_MISMATCH)


def test_bars_before_the_mapping_period_are_rejected(factory, assets):
    early = datetime(2015, 6, 1, 4, 0, tzinfo=UTC)  # reference mapping starts 2016-01-01
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(early)]})
    job = _run(_service(factory, fake), _request(start=early, end=END))

    assert job.counters.records_stored == 0
    (event,) = _events(factory, DataQualityEventType.UNEXPECTED_SYMBOL)
    assert "not mapped" in event.message


def test_duplicate_and_out_of_order_bars_within_a_response(factory, assets):
    fake = FakeAlpaca().add_page(
        {
            "AAPL": [
                fixture_bar(DAYS[1]),
                fixture_bar(DAYS[1]),  # identical repeat
                fixture_bar(DAYS[0]),  # goes backwards
            ]
        }
    )
    job = _run(_service(factory, fake), _request())
    assert job.counters.records_stored == 1
    assert job.counters.records_duplicate == 1
    assert job.counters.records_rejected == 1
    assert _events(factory, DataQualityEventType.OUT_OF_ORDER_EVENT)


# ------------------------------------------------------------------ failures and job states
def test_one_failing_symbol_makes_the_job_partial(factory, assets):
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0])]})
    for _ in range(3):
        fake.add_error(503, "service unavailable")
    job = _run(_service(factory, fake, max_retries=2), _request(symbols=("AAPL", "SPY")))

    assert job.status is IngestionJobStatus.PARTIALLY_COMPLETED
    spy = next(s for s in job.symbols if s.provider_symbol == "SPY")
    assert spy.status is IngestionSymbolStatus.FAILED
    assert "provider_unavailable" in (spy.error or "")
    assert "SPY" in (job.error or "")
    assert _events(factory, DataQualityEventType.PROVIDER_ERROR)


def test_rejected_credentials_fail_every_symbol(factory, assets):
    fake = FakeAlpaca().add_error(401, "unauthorized").add_error(401, "unauthorized")
    job = _run(_service(factory, fake), _request(symbols=("AAPL", "SPY")))
    assert job.status is IngestionJobStatus.FAILED
    assert all(s.status is IngestionSymbolStatus.FAILED for s in job.symbols)
    assert len(fake.requests) == 2  # never retried


def test_empty_response_is_no_data_not_success_or_failure(factory, assets):
    job = _run(_service(factory, FakeAlpaca().add_page({})), _request())
    assert job.status is IngestionJobStatus.COMPLETED
    assert job.symbols[0].status is IngestionSymbolStatus.NO_DATA
    assert job.counters.records_retrieved == 0
    assert _events(factory, DataQualityEventType.NO_DATA)


def test_pagination_failure_keeps_committed_pages_and_fails_the_symbol(factory, assets):
    fake = (
        FakeAlpaca()
        .add_page({"AAPL": [fixture_bar(DAYS[0])]}, "loop")
        .add_page({"AAPL": [fixture_bar(DAYS[1])]}, "loop")
    )
    job = _run(_service(factory, fake), _request())

    assert job.status is IngestionJobStatus.FAILED
    assert "repeated page token" in (job.symbols[0].error or "")
    # Both pages were complete and committed before the bad token was seen.
    assert len(_bars(factory)) == 2


def test_database_error_rolls_back_the_whole_page(factory, assets, monkeypatch):
    def broken_insert(self, rows):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr(MarketBarRepository, "insert", broken_insert)
    job = _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0])]})), _request()
    )

    assert job.status is IngestionJobStatus.FAILED
    assert "database_error" in (job.symbols[0].error or "")
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(RawMarketDataEventRecord)) == 0
        assert session.scalar(select(func.count()).select_from(MarketBarRecord)) == 0


def test_feed_switch_is_refused_and_recorded(factory, assets):
    service = _service(factory, FakeAlpaca())
    with pytest.raises(UnsupportedCapabilityError, match="never switches feeds"):
        service.create_job(_request(feed="sip"), requested_by="test")
    (event,) = _events(factory, DataQualityEventType.CAPABILITY_LIMITATION)
    assert event.feed == "sip"


def test_unentitled_request_is_refused_before_any_io(factory, assets):
    fake = FakeAlpaca()
    service = _service(factory, fake, data_feed="sip")  # Basic plan: no recent SIP
    with pytest.raises(UnsupportedCapabilityError, match="newer than"):
        service.create_job(_request(end=NOW - timedelta(minutes=1)), requested_by="test")
    assert fake.requests == []
    assert _events(factory, DataQualityEventType.CAPABILITY_LIMITATION)


def test_unknown_symbol_is_an_error(factory, assets):
    with pytest.raises(UnknownAssetError, match="not a registered"):
        _service(factory, FakeAlpaca()).create_job(_request(symbols=("QQQ",)), requested_by="t")


def test_jobs_run_once_and_orphans_are_recovered(factory, assets):
    service = _service(factory, FakeAlpaca())
    job = service.create_job(_request(), requested_by="test")

    assert service.recover_interrupted_jobs() == 1
    recovered = service.get_job(job.job_id)
    assert recovered.status is IngestionJobStatus.FAILED
    assert "interrupted" in (recovered.error or "")
    with pytest.raises(IngestionJobStateError, match="only PENDING"):
        asyncio.run(service.run_job(job.job_id))


# ------------------------------------------------------------------ immutability
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE market_bars SET close = 1",
        "DELETE FROM market_bars",
        "UPDATE market_data_raw_events SET payload_text = 'x'",
        "DELETE FROM market_data_quality_events",
    ],
)
def test_market_data_records_are_append_only(factory, assets, statement):
    _run(
        _service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0], v=None)]})),
        _request(),
    )
    with pytest.raises(DBAPIError, match="append-only"), session_scope(factory) as session:
        session.execute(text(statement))


def test_backfill_rows_cannot_claim_original_availability(factory, assets):
    _run(_service(factory, FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0])]})), _request())
    with pytest.raises(DBAPIError), session_scope(factory) as session:
        session.execute(
            text(
                "INSERT INTO market_bars (asset_id, provider, feed, provider_symbol, timeframe, "
                "adjustment, event_time, revision, open, high, low, close, is_complete, source, "
                "quality_status, received_at, available_at, original_available_at, processed_at) "
                "SELECT asset_id, provider, feed, provider_symbol, timeframe, adjustment, "
                "event_time, 9, open, high, low, close, is_complete, source, quality_status, "
                "received_at, available_at, received_at, processed_at FROM market_bars LIMIT 1"
            )
        )


# ------------------------------------------------------------------ asset registration
def test_registration_is_idempotent_and_audited(factory, assets):
    with session_scope(factory) as session:
        again = register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
        assert not any(r.asset_created or r.mapping_created for r in again)
        assert {r.asset_id for r in again} == set(assets.values())
        actions = list(session.scalars(select(AuditLog.action)))
    assert actions.count("asset.registered") == 2
    assert actions.count("asset_mapping.registered") == 2


def test_registration_never_remaps_silently(factory, assets):
    reference = load_reference_assets()
    changed = reference["AAPL"].model_copy(
        update={
            "providers": (reference["AAPL"].providers[0].model_copy(update={"symbol": "AAPL.X"}),)
        }
    )
    with (
        pytest.raises(ReferenceDataError, match="does not re-map silently"),
        session_scope(factory) as session,
    ):
        register_assets(session, ["AAPL"], "alpaca", reference | {"AAPL": changed})


def test_registration_rejects_unknown_symbols(factory):
    with (
        pytest.raises(ReferenceDataError, match="not in the reference data"),
        session_scope(factory) as session,
    ):
        register_assets(session, ["ZZZZ"], "alpaca", load_reference_assets())
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 0
