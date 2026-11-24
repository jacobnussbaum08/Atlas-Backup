"""Point-in-time sector classifications from SEC filings (D-239). Simulation database only.

For every stock member of the current equity universe (``universe.CURRENT_UNIVERSES[0]``)
whose ticker was not reused, the SIC code *as filed* in the header
of the latest 10-K / 10-Q / 20-F / 40-F filed on or before each formation session becomes a
classification valid from that filing's date:

* ``sector`` = the Fama-French 12-industry group of the SIC code (definitions of the
  Kenneth R. French data library, ``Siccodes12``), e.g. ``FF12_BUSEQ``;
* ``cluster`` = ``SIC_<code>``.

The CIK comes from the SEC's *current* ticker list, so securities whose ticker is no longer
listed (most delistings) cannot be mapped and stay unclassified: the risk engine then pools
them under its unchanged ``unclassified_sector_policy``. Coverage is reported, not filled.
ETFs are not classified here (a fund's asset class belongs to the H-B pre-registration).
Rows are written with ``created_at`` = now, so no earlier run's replay can see them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import (
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    RiskAssetClassificationRecord,
)
from app.research import sources
from app.research.universe import CURRENT_UNIVERSES

# Fama-French 12 industries by SIC range (inclusive); anything else is "OTHER".
FF12: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("NODUR", ((100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989))),
    ("DURBL", ((2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714),
               (3716, 3716), (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999))),
    ("MANUF", ((2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569),
               (3580, 3629), (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749),
               (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899))),
    ("ENRGY", ((1200, 1399), (2900, 2999))),
    ("CHEMS", ((2800, 2829), (2840, 2899))),
    ("BUSEQ", ((3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379))),
    ("TELCM", ((4800, 4899),)),
    ("UTILS", ((4900, 4949),)),
    ("SHOPS", ((5000, 5999), (7200, 7299), (7600, 7699))),
    ("HLTH", ((2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099))),
    ("MONEY", ((6000, 6999),)),
)  # fmt: skip


def ff12(sic: int) -> str:
    for name, ranges in FF12:
        if any(lo <= sic <= hi for lo, hi in ranges):
            return f"FF12_{name}"
    return "FF12_OTHER"


def classify(factory: sessionmaker[Session], progress: Any = print) -> dict[str, Any]:
    sec = sources.Sec()
    tickers = sec.company_tickers()
    c = ResearchUniverseCandidateRecord
    with factory() as session:
        rows = session.execute(
            select(
                c.asset_id,
                c.symbol,
                c.formation_session,
                ResearchSecurityRecord.type_basis,
                ResearchSecurityRecord.details,
            )
            .join(ResearchSecurityRecord, ResearchSecurityRecord.id == c.security_id)
            .where(
                c.universe_code == CURRENT_UNIVERSES[0],
                c.member.is_(True),
                ResearchSecurityRecord.security_type != "ETF",
            )
        ).all()
    by_asset: dict[Any, tuple[str, list[date]]] = {}
    reused: set[Any] = set()
    for asset_id, symbol, formation, basis, details in rows:
        # A point-in-time identity is looked up by its request ticker; its CIK comes from
        # today's ticker list only if it is the ticker's current holder.
        ticker = (details or {}).get("request_symbol", symbol)
        by_asset.setdefault(asset_id, (ticker, []))[1].append(formation)
        if (
            basis.startswith("ticker reuse")
            or (details or {}).get("same_as_current_holder") is False
        ):
            reused.add(asset_id)
    stats = {
        "members": len(by_asset),
        "reused_ticker": 0,
        "no_cik": 0,
        "no_filing": 0,
        "classified": 0,
        "rows": 0,
    }
    created_at = datetime.now(UTC)
    k = RiskAssetClassificationRecord
    with factory() as session:
        existing = {(a, v) for a, v in session.execute(select(k.asset_id, k.valid_from))}
    out = []
    for n, (asset_id, (symbol, formations)) in enumerate(
        sorted(by_asset.items(), key=lambda x: x[1][0])
    ):
        if asset_id in reused:
            # The SEC's current ticker list names the ticker's current holder, not this
            # formation-era security: no CIK can be taken from it.
            stats["reused_ticker"] += 1
            continue
        cik = tickers.get(symbol.replace(".", "-")) or tickers.get(symbol)
        if cik is None:
            stats["no_cik"] += 1
            continue
        filings = sec.filings(cik)
        chosen: dict[date, tuple[int, str]] = {}
        for f in sorted(formations):
            before = [x for x in filings if x["filed"] <= f]
            if not before:
                continue
            last = before[-1]
            if last["filed"] in chosen:
                continue
            code = sec.sic_as_filed(cik, last["acc"])
            if code is not None:
                chosen[last["filed"]] = (code, last["acc"])
        if not chosen:
            stats["no_filing"] += 1
            continue
        stats["classified"] += 1
        for filed, (code, acc) in sorted(chosen.items()):
            if (asset_id, datetime(filed.year, filed.month, filed.day, tzinfo=UTC)) in existing:
                continue  # already classified from this filing (an earlier universe version)
            out.append(
                RiskAssetClassificationRecord(
                    asset_id=asset_id,
                    sector=ff12(code),
                    cluster=f"SIC_{code:04d}",
                    valid_from=datetime(filed.year, filed.month, filed.day, tzinfo=UTC),
                    source="OPERATOR",
                    provenance=(
                        f"D-239 research: SIC {code:04d} as filed in SEC accession {acc} "
                        f"(CIK {cik}, filed {filed.isoformat()}); Fama-French 12 grouping"
                    ),
                    created_by="operator:research-foundation",
                    created_at=created_at,
                )
            )
        if (n + 1) % 50 == 0:
            progress(f"classified {n + 1}/{len(by_asset)}")
    with factory() as session, session.begin():
        session.add_all(out)
    stats["rows"] = len(out)
    return stats


__all__ = ["FF12", "classify", "ff12"]
