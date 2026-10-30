"""Market-data API over real PostgreSQL; provider responses come from the FAKE server."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.clocks import FixedClock
from app.core.config import MarketDataSettings
from app.data_engine.historical import HistoricalIngestionService
from app.data_engine.reference_assets import load_reference_assets, register_assets
from app.data_engine.runtime import MarketDataContext
from app.database.sessions import session_scope
from app.main import create_app
from app.tests.fixtures.alpaca import FAKE_KEY, FAKE_SECRET, FakeAlpaca, fake_provider, fixture_bar

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 10, 18, 0, tzinfo=UTC)
DAYS = [datetime(2026, 3, d, 5, 0, tzinfo=UTC) for d in (2, 3, 4)]
OPERATOR_TOKEN = "test-operator-token-fixture"


@pytest.fixture
def factory(fresh_engine):
    factory = sessionmaker(bind=fresh_engine, expire_on_commit=False)
    with session_scope(factory) as session:
        register_assets(session, ["AAPL", "SPY"], "alpaca", load_reference_assets())
    return factory


@pytest.fixture
def make_client(factory, make_settings):
    def build(fake: FakeAlpaca, **settings_overrides) -> TestClient:
        md_settings = MarketDataSettings(_env_file=None)
        provider = fake_provider(fake, now=NOW)
        context = MarketDataContext(
            settings=md_settings,
            provider=provider,
            service=HistoricalIngestionService(factory, provider, md_settings, FixedClock(NOW)),
            session_factory=factory,
        )
        app = create_app(
            make_settings(**settings_overrides),
            clock=FixedClock(NOW),
            health_checks={},
            market_data=context,
        )
        return TestClient(app)

    return build


def _fetch_body(**overrides):
    return {
        "symbols": ["AAPL"],
        "timeframe": "1Day",
        "start": DAYS[0].isoformat(),
        "end": DAYS[-1].isoformat(),
    } | overrides


def _page(*days, **bar):
    return {"AAPL": [fixture_bar(d, **bar) for d in days]}


# ------------------------------------------------------------------ fetch and jobs
def test_fetch_runs_in_background_and_reports_job_state(make_client):
    fake = FakeAlpaca().add_page(_page(*DAYS))
    with make_client(fake) as client:
        accepted = client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        assert accepted.status_code == 202
        job = accepted.json()
        assert job["status"] == "PENDING"
        assert job["feed"] == "iex"
        assert job["adjustment"] == "RAW"

        done = client.get(f"/api/v1/market-data/ingestion-jobs/{job['job_id']}").json()
        assert done["status"] == "COMPLETED"
        assert done["counters"]["records_stored"] == 3
        assert done["symbols"][0]["status"] == "COMPLETED"

        listed = client.get("/api/v1/market-data/ingestion-jobs", params={"status": "COMPLETED"})
        assert [j["job_id"] for j in listed.json()] == [job["job_id"]]
        assert (
            client.get("/api/v1/market-data/ingestion-jobs", params={"status": "FAILED"}).json()
            == []
        )


def test_failed_retrievals_are_inspectable(make_client):
    fake = FakeAlpaca().add_error(401, "unauthorized")
    with make_client(fake) as client:
        job = client.post("/api/v1/market-data/historical-fetch", json=_fetch_body()).json()
        failed = client.get(
            "/api/v1/market-data/ingestion-jobs", params={"status": "FAILED"}
        ).json()
        events = client.get("/api/v1/market-data/quality-events", params={"job_id": job["job_id"]})

    assert [j["job_id"] for j in failed] == [job["job_id"]]
    assert "provider_authentication_failed" in failed[0]["symbols"][0]["error"]
    assert events.json()[0]["event_type"] == "PROVIDER_ERROR"


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (_fetch_body(feed="sip"), 422, "unsupported_capability"),
        (_fetch_body(symbols=["QQQ"]), 404, "unknown_asset"),
        (_fetch_body(timeframe="2Day"), 422, "unsupported_capability"),
    ],
)
def test_invalid_fetch_requests_are_rejected_with_clear_errors(make_client, body, status, code):
    with make_client(FakeAlpaca()) as client:
        response = client.post("/api/v1/market-data/historical-fetch", json=body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_fetch_body_validation(make_client):
    with make_client(FakeAlpaca()) as client:
        naive = client.post(
            "/api/v1/market-data/historical-fetch", json=_fetch_body(start="2026-03-02T05:00:00")
        )
        bad_timeframe = client.post(
            "/api/v1/market-data/historical-fetch", json=_fetch_body(timeframe="daily")
        )
    assert naive.status_code == 422
    assert bad_timeframe.status_code == 422


# ------------------------------------------------------------------ operator protection
def test_operator_token_is_required_when_configured(make_client):
    fake = FakeAlpaca().add_page(_page(DAYS[0]))
    with make_client(fake, operator_token=OPERATOR_TOKEN) as client:
        missing = client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        wrong = client.post(
            "/api/v1/market-data/historical-fetch",
            json=_fetch_body(),
            headers={"X-Atlas-Operator-Token": "wrong"},
        )
        ok = client.post(
            "/api/v1/market-data/historical-fetch",
            json=_fetch_body(),
            headers={"X-Atlas-Operator-Token": OPERATOR_TOKEN},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert ok.status_code == 202


def test_paper_environment_without_token_disables_operations(make_client):
    with make_client(
        FakeAlpaca(), environment="paper", postgres_password="Unique-Pw-123"
    ) as client:
        response = client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        reads = client.get("/api/v1/market-data/assets")
    assert response.status_code == 403
    assert reads.status_code == 200


# ------------------------------------------------------------------ candles and history
def test_candles_are_served_with_provenance_and_exact_decimals(make_client):
    fake = FakeAlpaca().add_page(_page(*DAYS, o="187.10", h="188.2525", low="186.5", c="187.9999"))
    with make_client(fake) as client:
        client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        response = client.get(
            "/api/v1/market-data/candles",
            params={
                "symbol": "AAPL",
                "timeframe": "1Day",
                "start": DAYS[0].isoformat(),
                "end": DAYS[-1].isoformat(),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["feed"] == "iex"
    assert body["feed_coverage"] == "SINGLE_VENUE"
    assert body["adjustment"] == "RAW"
    assert body["asset"]["symbol"] == "AAPL"
    assert any("single venue" in note for note in body["notes"])
    assert any("original availability unknown" in note for note in body["notes"])
    candle = body["candles"][0]
    assert candle["close"] == "187.9999"  # Decimal serialized exactly, as a string
    assert candle["high"] == "188.2525"
    assert candle["original_available_at"] is None
    assert candle["source"] == "HISTORICAL_REST"
    assert candle["quality_status"] == "VALID"
    assert [c["event_time"] for c in body["candles"]] == [
        d.isoformat().replace("+00:00", "Z") for d in DAYS
    ]


def test_candles_paginate_and_never_mix_series(make_client):
    with make_client(FakeAlpaca().add_page(_page(*DAYS))) as client:
        client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        params = {
            "symbol": "AAPL",
            "timeframe": "1Day",
            "start": DAYS[0].isoformat(),
            "end": DAYS[-1].isoformat(),
        }
        first = client.get("/api/v1/market-data/candles", params=params | {"limit": 2}).json()
        rest = client.get(
            "/api/v1/market-data/candles", params=params | {"after": first["next_after"]}
        ).json()
        other_feed = client.get("/api/v1/market-data/candles", params=params | {"feed": "sip"})
        adjusted = client.get(
            "/api/v1/market-data/candles", params=params | {"adjustment": "SPLIT"}
        )

    assert len(first["candles"]) == 2
    assert len(rest["candles"]) == 1
    assert rest["next_after"] is None
    assert other_feed.json()["candles"] == []  # IEX bars never presented as SIP
    assert adjusted.json()["candles"] == []  # raw bars never presented as adjusted


def test_quarantined_candles_are_hidden_unless_requested(make_client):
    fake = FakeAlpaca().add_page({"AAPL": [fixture_bar(DAYS[0]), fixture_bar(DAYS[1], h="1")]})
    with make_client(fake) as client:
        client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        params = {
            "symbol": "AAPL",
            "timeframe": "1Day",
            "start": DAYS[0].isoformat(),
            "end": DAYS[-1].isoformat(),
        }
        default = client.get("/api/v1/market-data/candles", params=params).json()
        everything = client.get(
            "/api/v1/market-data/candles",
            params=params | {"quality": ["VALID", "QUARANTINED"]},
        ).json()
    assert len(default["candles"]) == 1
    assert [c["quality_status"] for c in everything["candles"]] == ["VALID", "QUARANTINED"]


def test_history_reports_available_ranges(make_client):
    with make_client(FakeAlpaca().add_page(_page(*DAYS))) as client:
        client.post("/api/v1/market-data/historical-fetch", json=_fetch_body())
        assets = client.get("/api/v1/market-data/assets").json()
        aapl = next(a for a in assets if a["symbol"] == "AAPL")
        history = client.get(f"/api/v1/market-data/assets/{aapl['asset_id']}/history").json()

    assert {a["symbol"] for a in assets} == {"AAPL", "SPY"}
    (coverage,) = history["coverage"]
    assert coverage["timeframe"] == "1Day"
    assert coverage["feed"] == "iex"
    assert coverage["bars"] == 3
    assert coverage["first_event_at"] == DAYS[0].isoformat().replace("+00:00", "Z")


def test_candle_query_validation(make_client):
    with make_client(FakeAlpaca()) as client:
        unknown = client.get(
            "/api/v1/market-data/candles",
            params={
                "symbol": "ZZZ",
                "timeframe": "1Day",
                "start": DAYS[0].isoformat(),
                "end": DAYS[1].isoformat(),
            },
        )
        bad_tf = client.get(
            "/api/v1/market-data/candles",
            params={
                "symbol": "AAPL",
                "timeframe": "1Year",
                "start": DAYS[0].isoformat(),
                "end": DAYS[1].isoformat(),
            },
        )
    assert unknown.status_code == 404
    assert bad_tf.status_code == 422
    assert bad_tf.json()["error"]["code"] == "invalid_request"


# ------------------------------------------------------------------ provider info
def test_provider_endpoint_never_exposes_credentials(make_client):
    with make_client(FakeAlpaca()) as client:
        response = client.get("/api/v1/market-data/providers")
    assert response.status_code == 200
    text = response.text
    assert FAKE_KEY not in text
    assert FAKE_SECRET not in text
    info = response.json()[0]
    assert info["provider"] == "alpaca"
    assert info["credentials"] == "CONFIGURED"
    assert info["feed_coverage"] == "SINGLE_VENUE"
    assert info["adjustment"] == "RAW"
