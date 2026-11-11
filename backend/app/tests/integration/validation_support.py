"""Integration fixtures for the Phase 8B validation laboratory (PostgreSQL).

Every price series here is a **DETERMINISTIC TEST FIXTURE** (``FIXTURE`` provider symbol): a
synthetic oscillating daily series written for these tests, not market data. The strategy
is the 4B TEST FIXTURE strategy (close versus a short SMA), not a trading strategy.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from app.asset_discovery.sessions import SessionDay
from app.backtesting.config import ResolvedConfig
from app.learning.governance import Policy
from app.learning.runtime import ValidationContext, build_validation_context
from app.tests.integration.backtest_support import (
    Bench,
    Candle,
    D,
    WallClock,
    make_bench,
    make_traded_asset,
    seed_series,
)
from app.tests.integration.strategy_support import ProposingStrategy

HISTORY_FIRST = date(2026, 2, 2)
START = date(2026, 6, 1)
END = date(2026, 10, 28)
WIDE = {
    "hard_limits": {
        "max_sector_pct": "1.00",
        "max_open_positions": 30,
        "max_single_asset_pct": "1.00",
        "max_correlated_exposure_pct": "1.00",
    }
}


def wave(index: int, _day: SessionDay, *, base: float = 100.0, period: float = 13.0) -> Candle:
    """MOCK candle: a slow drift plus a 13-session oscillation of +/-6% (crossings of a short
    SMA in both directions; stops, targets and horizon exits all occur)."""
    close = base + 0.03 * index + 6.0 * math.sin(index / period * 2 * math.pi)
    c = D(str(round(close, 4)))
    return Candle(c - D("0.15"), c + D("0.9"), c - D("0.9"), c)


def experiment(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "fixture walk-forward",
        "strategies": [
            {
                "strategy_id": "fixture_proposer",
                "space": {
                    "dimensions": [
                        {"name": "sma_length", "values": [3, 5, 10]},
                        {"name": "holding_horizon_periods", "values": [2, 4]},
                    ]
                },
            }
        ],
        "start_date": START.isoformat(),
        "end_date": END.isoformat(),
        "universe": {"mode": "EXPLICIT", "symbols": ["FIXA"]},
        "timeframes": ["1Day"],
        "directions": ["LONG"],
        "benchmark_symbol": "FIXB",
        "risk": WIDE,
        "windows": {"train_sessions": 25, "test_sessions": 10},
        "search": "GRID",
        "max_trials": 7,
        "robustness": {
            "bootstrap_resamples": 200,
            "monte_carlo_resamples": 200,
            "cost_multipliers": ["0", "2"],
            "slippage_multipliers": ["2"],
            "pbo_groups": 4,
            "cpcv_groups": 4,
            "purged_kfold": 4,
        },
        "seed": 7,
    }
    values.update(overrides)
    return values


@dataclass
class LoadSpy:
    """Records the date range of every dataset the replays load."""

    loads: list[tuple[date, date]] = field(default_factory=list)


@dataclass
class ValidationLab:
    bench: Bench
    ctx: ValidationContext
    spy: LoadSpy
    asset: uuid.UUID
    benchmark: uuid.UUID


def make_validation_lab(engine: Engine, *, policy: Policy | None = None) -> ValidationLab:
    bench = make_bench(engine, (ProposingStrategy(),))
    asset = make_traded_asset(bench.factory, "FIXA")
    benchmark = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, asset, wave, first=HISTORY_FIRST, last=END)
    seed_series(
        bench.factory,
        benchmark,
        lambda i, d: wave(i, d, base=400.0, period=40.0),
        first=HISTORY_FIRST,
        last=END,
    )
    spy = LoadSpy()
    original = bench.service.load

    def load(session: Any, config: ResolvedConfig) -> Any:
        spy.loads.append((config.start_date, config.end_date))
        return original(session, config)

    bench.service.load = load  # type: ignore[method-assign]
    ctx = build_validation_context(
        bench.factory,
        WallClock(),
        registry=bench.registry,
        backtests=bench.service,
        policy=policy,
    )
    return ValidationLab(bench, ctx, spy, asset, benchmark)


def module_engine(server_url: URL, template_database: str) -> Iterator[Engine]:
    """An isolated, migrated database shared by one test module (copied from the template)."""
    name = f"atlas_v_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_database}"'))
    engine = create_engine(
        server_url.set(database=name), connect_args={"options": "-c timezone=UTC"}
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


__all__ = [
    "END",
    "HISTORY_FIRST",
    "START",
    "WIDE",
    "LoadSpy",
    "ValidationLab",
    "experiment",
    "make_validation_lab",
    "module_engine",
    "wave",
]
