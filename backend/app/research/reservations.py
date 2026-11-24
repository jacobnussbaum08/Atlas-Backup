"""Data reservations and pre-registration records (D-239). Simulation database only.

Reservations (append-only; enforced by ``store.check_reservations`` at backtest resolve):

* ``V1_2016_2019`` (PERIOD 2016-01-01..2019-12-31): any universe. A run whose dates overlap
  needs a pre-registration listing it. (Its lookback may read 2014-09..2015 bars.)
* ``V2_<universe code>`` (UNIVERSE), one per stored universe version (equity V1-V3, ETF
  candidates V2-V4; superseded versions stay reserved): any run that trades a member (any
  period), or names the universe, needs a pre-registration.
* ``EXPLORATION_21_2020_2026`` (EXPLORATION_ONLY): the 21 previously examined symbols over
  2020-06..2026-11 may be explored but not used to confirm a hypothesis. Never blocks.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import ResearchDataReservationRecord, ResearchPreregistrationRecord
from app.research.universe import EXPLORATION_SYMBOLS

CREATED_BY = "operator:research-foundation"


def reserve(factory: sessionmaker[Session], dataset_sha256: str) -> list[str]:
    rows = [
        ResearchDataReservationRecord(
            code="V1_2016_2019",
            scope="PERIOD",
            start_date=date(2016, 1, 1),
            end_date=date(2019, 12, 31),
            symbols=[],
            description="Initial untouched research and test period (any universe). No "
            "strategy result has been computed on it. Backtests overlapping it need a "
            "registered pre-registration that lists this reservation.",
            dataset_sha256=dataset_sha256,
            details={"lookback_note": "a reserved run's lookback may read bars from 2014-09"},
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_US_EQUITY_V1",
            scope="UNIVERSE",
            universe_code="RESEARCH_US_EQUITY_V1",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Broad reconstructed US equity universe excluding the 21 examined "
            "symbols (and SPY): trading any member, in any period, needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_ETF_CANDIDATES_V2",
            scope="UNIVERSE",
            universe_code="RESEARCH_ETF_CANDIDATES_V2",
            symbols=list(EXPLORATION_SYMBOLS),
            description="ETF candidate pool for H-B: trading any member needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_US_EQUITY_V2",
            scope="UNIVERSE",
            universe_code="RESEARCH_US_EQUITY_V2",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Corrected broad US equity universe (supersedes V1's typing): trading "
            "any member, in any period, needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_ETF_CANDIDATES_V3",
            scope="UNIVERSE",
            universe_code="RESEARCH_ETF_CANDIDATES_V3",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Corrected ETF candidate pool for H-B: trading any member needs a "
            "pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_US_EQUITY_V3",
            scope="UNIVERSE",
            universe_code="RESEARCH_US_EQUITY_V3",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Current broad US equity universe (traded bars only; supersedes V2): "
            "trading any member, in any period, needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_US_EQUITY_V4",
            scope="UNIVERSE",
            universe_code="RESEARCH_US_EQUITY_V4",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Current broad US equity universe (point-in-time identity; supersedes "
            "V3): trading any member, in any period, needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_ETF_CANDIDATES_V4",
            scope="UNIVERSE",
            universe_code="RESEARCH_ETF_CANDIDATES_V4",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Current ETF candidate pool for H-B (traded bars only; supersedes V3): "
            "trading any member needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="V2_RESEARCH_ETF_CANDIDATES_V5",
            scope="UNIVERSE",
            universe_code="RESEARCH_ETF_CANDIDATES_V5",
            symbols=list(EXPLORATION_SYMBOLS),
            description="Current ETF candidate pool for H-B (point-in-time identity; "
            "supersedes V4): trading any member needs a pre-registration.",
            dataset_sha256=dataset_sha256,
            created_by=CREATED_BY,
        ),
        ResearchDataReservationRecord(
            code="EXPLORATION_21_2020_2026",
            scope="EXPLORATION_ONLY",
            start_date=date(2020, 6, 1),
            end_date=date(2026, 11, 14),
            symbols=list(EXPLORATION_SYMBOLS),
            description="Previously examined data (the 21 evidence-universe symbols, "
            "2020-06..2026-11): exploration only; no hypothesis may be confirmed on it.",
            created_by=CREATED_BY,
        ),
    ]
    with factory() as session, session.begin():
        existing = set(session.scalars(select(ResearchDataReservationRecord.code)))
        added = [r.code for r in rows if r.code not in existing]
        session.add_all([r for r in rows if r.code not in existing])
    return added


def preregister(
    factory: sessionmaker[Session], code: str, document: Path, reservations: list[str]
) -> dict[str, Any]:
    """Register a pre-registration document by its hash (no experiment is started)."""
    sha = hashlib.sha256(document.read_bytes()).hexdigest()
    with factory() as session, session.begin():
        known = set(session.scalars(select(ResearchDataReservationRecord.code)))
        unknown = sorted(set(reservations) - known)
        if unknown:
            raise ValueError(f"unknown reservations: {', '.join(unknown)}")
        session.add(
            ResearchPreregistrationRecord(
                code=code,
                document_path=str(document),
                document_sha256=sha,
                reservation_codes=sorted(reservations),
                created_by=CREATED_BY,
            )
        )
    return {"code": code, "document_sha256": sha}


__all__ = ["preregister", "reserve"]
