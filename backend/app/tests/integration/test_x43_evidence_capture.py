"""X4.3 (D-247): the read-only provider-identity snapshot, from a FIXTURE database.

Identities and bars are TEST FIXTURES, not market data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, select

from app.asset_discovery.sessions import SessionDay
from app.backtesting.data import sessions_between
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityStatus,
    DataSource,
    PriceAdjustment,
)
from app.database.models import MarketBarRecord, ResearchSecurityRecord
from app.database.sessions import build_session_factory, session_scope
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43
from app.tests.integration.backtest_support import seed_series, trend
from app.tests.integration.strategy_support import make_asset, midnight

pytestmark = pytest.mark.integration
FIRST, LAST = date(2016, 12, 27), date(2016, 12, 30)


def sip_rows(asset: Any, day: SessionDay, close: str, revision: int) -> list[dict[str, Any]]:
    known = datetime.combine(day.day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=22)
    return [
        {
            "asset_id": asset,
            "provider": "alpaca",
            "feed": "sip",
            "provider_symbol": "FIXZ",
            "timeframe": "1Day",
            "adjustment": adj,
            "event_time": midnight(day),
            "revision": revision,
            "open": Decimal(close),
            "high": Decimal(close),
            "low": Decimal(close),
            "close": Decimal(close) / (2 if adj is PriceAdjustment.SPLIT else 1),
            "volume": Decimal(1000 + revision),
            "vwap": None,
            "trade_count": 10,
            "is_complete": True,
            "source": DataSource.HISTORICAL_REST,
            "observation_kind": BarObservationKind.HISTORICAL,
            "quality_status": DataQualityStatus.VALID,
            "received_at": known,
            "available_at": known,
            "original_available_at": None,
            "processed_at": known,
            "ingested_at": known,
        }
        for adj in (PriceAdjustment.RAW, PriceAdjustment.SPLIT)
    ]


def seed(factory: Any) -> Any:
    asset = make_asset(factory, "FIXZ")
    other = make_asset(factory, "FIXY")
    days = sessions_between(FIRST, LAST)
    # IEX bars of the same asset are not the research identity's history: ignored.
    seed_series(factory, asset, lambda i, _d: trend(i), first=FIRST, last=LAST)
    rows = [r for i, d in enumerate(days) for r in sip_rows(asset, d, f"{10 + i}.5", 0)]
    rows += sip_rows(asset, days[1], "99.5", 1)  # a corrected bar: the latest revision wins
    rows += sip_rows(other, days[0], "7.5", 0)  # an asset with no alpaca-asof identity
    now = datetime.now(UTC)
    with session_scope(factory) as s:
        s.execute(insert(MarketBarRecord), rows)
        for provider, asset_id in (("alpaca-asof", asset), ("alpaca", other)):
            s.add(
                ResearchSecurityRecord(
                    provider=provider,
                    symbol="FIXZ",
                    revision=0,
                    provider_status="active",
                    security_type="STOCK",
                    type_basis="fixture",
                    scan_start=FIRST,
                    scan_end=LAST,
                    first_bar=FIRST,
                    last_bar=LAST,
                    asset_id=asset_id,
                    retrieved_at=now,
                    details={"asof": "2016-12-30", "request_symbol": "FIXZ"},
                )
            )
    return asset, days


def test_the_snapshot_reads_identities_and_latest_sip_bars(fresh_engine: Engine) -> None:
    factory = build_session_factory(fresh_engine)
    asset, days = seed(factory)
    with factory() as s:
        before = s.scalar(select(func.count()).select_from(MarketBarRecord))
    snap = x43.provider_identities(factory)
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(MarketBarRecord)) == before
    (ident,) = snap["identities"]
    assert ident["asset_id"] == str(asset)
    assert ident["details"]["asof"] == "2016-12-30"
    assert list(snap["bars"]) == [str(asset)]  # the other asset has no alpaca-asof identity
    series = snap["bars"][str(asset)]
    assert [r[0] for r in series] == [d.day.isoformat() for d in days]
    assert Decimal(series[1][1]) == Decimal("99.5")  # revision 1
    assert Decimal(series[1][2]) == Decimal("49.75")  # split-adjusted close kept apart
    assert Decimal(series[1][3]) == Decimal(1001)
    assert Decimal(series[0][1]) == Decimal("10.5")


def test_the_capture_is_write_once_and_verifiable(fresh_engine: Engine, tmp_path) -> None:
    factory = build_session_factory(fresh_engine)
    seed(factory)
    store = x43.Store(tmp_path / "x43")
    got = x43.capture_identities(factory, store)
    assert got == {"identities": 1, "assets_with_bars": 1, "bars": 4}
    shas = {k: v["sha256"] for k, v in store.entries.items()}
    x43.capture_identities(factory, x43.Store(tmp_path / "x43"))  # nothing replaced
    again = x43.Store(tmp_path / "x43")
    assert {k: v["sha256"] for k, v in again.entries.items()} == shas
    assert x43.verify(again)["ok"]
    entry = again.entries["db/provider_identities.json.gz"]
    assert entry["source"]["kind"] == "simulation_database_snapshot"
    assert x4.read_json(again.path("db/provider_identities.json.gz"))[0]["symbol"] == "FIXZ"
