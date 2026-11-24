"""Shared setup for the Phase 5A ML integration tests.

Everything here is TEST FIXTURE material: invented assets, SYNTHETIC daily bars from
``tests/fixtures/ml_series`` (seeded random walks, not market data) and datasets whose
specification sets ``fixture=True`` so they are stored with origin TEST_FIXTURE.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import FixedClock
from app.database.models import AssetClass
from app.database.sessions import session_scope
from app.ml_engine.config import MLSettings
from app.ml_engine.datasets.builder import build_dataset
from app.ml_engine.datasets.spec import DatasetSpec
from app.ml_engine.datasets.store import save_dataset
from app.ml_engine.inference.engine import InferenceEngine
from app.ml_engine.training.trainer import Trainer
from app.tests.fixtures.ml_series import noise, planted_signal
from app.tests.integration.strategy_support import CAL, NOW, make_asset, seed_candles

SESSIONS = 420
FIRST_DECISION = datetime(2025, 6, 2, tzinfo=UTC)
LAST_DECISION = datetime(2026, 11, 3, 22, 0, tzinfo=UTC)

FEATURES: list[dict[str, Any]] = [
    {"name": "return", "params": {"periods": 3}},
    {"name": "return", "params": {"periods": 10}},
    {"name": "rsi", "params": {"length": 14}},
    {"name": "atr_pct", "params": {"length": 14}},
    {"name": "zscore", "params": {"length": 20}},
    {"name": "volume_ratio", "params": {"short": 5, "long": 20}},
    {"name": "relative_return", "params": {"periods": 20}, "optional": True},
]


@dataclass
class MLLab:
    factory: sessionmaker[Session]
    settings: MLSettings
    clock: FixedClock
    trainer: Trainer
    inference: InferenceEngine


def make_ml_lab(engine: Engine, artifact_dir: Path, now: datetime = NOW) -> MLLab:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = MLSettings(artifact_dir=artifact_dir)
    clock = FixedClock(now)
    return MLLab(
        factory,
        settings,
        clock,
        Trainer(factory, settings, clock),
        InferenceEngine(factory, settings, clock),
    )


def seed_universe(
    lab: MLLab, symbols: tuple[str, ...] = ("FXA", "FXB", "FXC"), *, planted: bool = True
) -> dict[str, uuid.UUID]:
    """FIXTURE assets with SYNTHETIC bars, plus a SYNTHETIC SPY benchmark."""
    ids = {}
    make = planted_signal if planted else noise
    for i, symbol in enumerate(symbols):
        ids[symbol] = make_asset(lab.factory, symbol)
        seed_candles(lab.factory, ids[symbol], make(SESSIONS, seed=100 + i))
    ids["SPY"] = make_asset(lab.factory, "SPY", AssetClass.ETF)
    seed_candles(lab.factory, ids["SPY"], noise(SESSIONS, seed=7))
    return ids


def spec(**overrides: Any) -> DatasetSpec:
    base: dict[str, Any] = {
        "name": "fixture-direction",
        "description": "TEST FIXTURE dataset",
        "symbols": ["FXA", "FXB", "FXC"],
        "timeframes": ["1Day"],
        "directions": ["LONG", "SHORT"],
        "decision_start": FIRST_DECISION.isoformat(),
        "decision_end": LAST_DECISION.isoformat(),
        "data_known_at": NOW.isoformat(),
        "features": FEATURES,
        "target": {"kind": "DIRECTION", "horizon_periods": 3, "round_trip_cost_bps": 10},
        "fixture": True,
    }
    base.update(overrides)
    return DatasetSpec.model_validate(base)


def build_and_save(lab: MLLab, dataset_spec: DatasetSpec) -> uuid.UUID:
    with session_scope(lab.factory) as session:
        built = build_dataset(
            session, dataset_spec, provider="alpaca", feed="iex", calendar=CAL, now=lab.clock.now()
        )
        version = save_dataset(session, built, actor="test", provider="alpaca", feed="iex")
        return version.id


def fetch[T](session: Session, cls: type[T], ident: Any) -> T:
    """``session.get`` for rows a test knows exist."""
    found = session.get(cls, ident)
    assert found is not None, f"{cls.__name__} {ident} does not exist"
    return found


def later(lab: MLLab, delta: timedelta) -> None:
    lab.clock.advance(delta)


__all__ = [
    "CAL",
    "FEATURES",
    "FIRST_DECISION",
    "LAST_DECISION",
    "NOW",
    "MLLab",
    "build_and_save",
    "fetch",
    "make_ml_lab",
    "seed_universe",
    "spec",
]
