"""Validation of the research data foundation (D-239). Read only; computes no strategy result.

Checks, each PASS / WARN / FAIL with its numbers:

1. survivorship: the security master's coverage of well-known 2016-2019 delistings (an
   author-compiled reference list, each entry checked for SIP bars: used only to measure
   coverage), and the delisting rate inside the universe;
2. look-ahead: membership effective strictly after formation; formation windows end at the
   formation; classifications valid from their filing date; delistings not before the last bar;
3. missing symbols: members without bars, or with long gaps inside their membership span;
4. corporate-action consistency: RAW/SPLIT ratio changes versus provider split actions;
5. gaps and duplicates: duplicate bars, RAW/SPLIT session mismatches;
6. point-in-time classification coverage per formation;
7. reproducibility: rule hashes recomputed, append-only triggers present, dataset hash;
8. phantom-split coverage (D-240): every phantom split is excluded or unreachable.

Writes the report JSON and returns it.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_between
from app.database.models import (
    Asset,
    MarketBarRecord,
    ResearchDataReservationRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
    RiskAssetClassificationRecord,
)
from app.research import sources, store
from app.research.bars import BARS_END, BARS_START, END_TS, START_TS, members
from app.research.universe import CURRENT_UNIVERSES, rule_sha

# Well-known US listings that ended 2016-2019 by acquisition, merger or bankruptcy (ticker
# not continued by a rename). Author-compiled; used only to measure coverage.
KNOWN_DELISTINGS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
TABLES = (
    "research_securities",
    "research_universes",
    "research_universe_candidates",
    "research_data_reservations",
    "research_preregistrations",
    "research_exclusions",
)


def _status(ok: bool, warn: bool = False) -> str:
    return "PASS" if ok else ("WARN" if warn else "FAIL")


def survivorship(session: Session) -> dict[str, Any]:
    s = ResearchSecurityRecord
    master = set(session.scalars(select(s.symbol)))
    with_bars = {}
    with sources.alpaca_client() as client:
        for i in range(0, len(KNOWN_DELISTINGS), 50):
            batch = list(KNOWN_DELISTINGS[i : i + 50])
            for sym, _bar in sources.daily_bars(client, batch, date(2016, 1, 4), date(2016, 1, 8)):
                with_bars[sym] = True
    present = sorted(x for x in KNOWN_DELISTINGS if x in master)
    has_bars = sorted(with_bars)
    missing = sorted(x for x in has_bars if x not in master)
    coverage = len([x for x in has_bars if x in master]) / max(len(has_bars), 1)
    c = ResearchUniverseCandidateRecord
    member_syms = set(
        session.scalars(
            select(c.symbol).where(c.universe_code == CURRENT_UNIVERSES[0], c.member.is_(True))
        )
    )
    delisted = {
        r.symbol
        for r in session.scalars(select(s).where(s.delisted_on.is_not(None)))
        if r.symbol in member_syms
    }
    return {
        "status": _status(coverage >= 0.95, warn=True),
        "reference_list": len(KNOWN_DELISTINGS),
        "reference_with_sip_bars_2016": len(has_bars),
        "in_security_master": len(present),
        "coverage_of_reference_with_bars": round(coverage, 3),
        "missing_from_master": missing,
        "equity_members": len(member_syms),
        "equity_members_delisted_in_period": len(delisted),
        "note": "securities delisted before the provider's inactive list begins are absent; "
        "the universe is survivorship-incomplete by the missing share",
    }


def delisting_review(session: Session) -> dict[str, Any]:
    """Inferred delistings whose ticker the provider still lists as active: a rename or a
    reused ticker cannot be told from a delisting without symbol-change history."""
    s = ResearchSecurityRecord
    newest: dict[str, ResearchSecurityRecord] = {}
    for r in session.scalars(select(s).order_by(s.symbol, s.revision)):
        newest[r.symbol] = r
    delisted = [r for r in newest.values() if r.delisted_on is not None]
    review = sorted(
        (r.symbol, r.delisted_on.isoformat())
        for r in delisted
        if r.provider_status == "active"
        if r.delisted_on is not None
    )
    return {
        "status": _status(not review, warn=True),
        "inferred_delistings": len(delisted),
        "provider_still_active_review": review,
        "note": "exits at the last trade either way; a rename ends the member's span early "
        "(its successor ticker is not a member), which costs no price but cuts the trade",
    }


def look_ahead(session: Session) -> dict[str, Any]:
    c = ResearchUniverseCandidateRecord
    bad_span = session.scalar(
        select(func.count()).where(c.member.is_(True), c.effective_from <= c.formation_session)
    )
    bad_window = session.scalar(select(func.count()).where(c.sessions_observed > 60))
    s = ResearchSecurityRecord
    bad_delist = session.scalar(
        select(func.count()).where(s.delisted_on.is_not(None), s.delisted_on < s.last_bar)
    )
    k = RiskAssetClassificationRecord
    research_cls = session.scalars(
        select(k).where(k.created_by == "operator:research-foundation")
    ).all()
    bad_cls = [r.id for r in research_cls if r.valid_from.date().isoformat() not in r.provenance]
    ok = not (bad_span or bad_window or bad_delist or bad_cls)
    return {
        "status": _status(ok),
        "members_effective_on_or_before_formation": bad_span,
        "candidates_with_window_over_60_sessions": bad_window,
        "delisting_before_last_bar": bad_delist,
        "classifications_not_valid_from_their_filing": len(bad_cls),
    }


def bars_quality(session: Session) -> dict[str, Any]:
    b = MarketBarRecord
    targets = members(session, current=True)
    by_symbol = {a: s for a, s in targets}
    c = ResearchUniverseCandidateRecord
    spans: dict[Any, list[tuple[date, date]]] = defaultdict(list)
    for a, f, t in session.execute(
        select(c.asset_id, c.effective_from, c.effective_to).where(
            c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES)
        )
    ):
        if f is not None and t is not None:
            spans[a].append((f, t))
    no_bars, gaps, mismatch = [], [], []
    dup = session.scalar(
        text(
            "select count(*) from (select asset_id, adjustment, event_time from market_bars "
            "where feed='sip' and timeframe='1Day' group by 1,2,3 having count(*) > 1) x"
        )
    )
    for asset_id, symbol in targets:
        days: dict[str, set[date]] = {}
        for adj, at in session.execute(
            select(b.adjustment, b.event_time).where(
                b.asset_id == asset_id,
                b.feed == "sip",
                b.timeframe == "1Day",
                b.quality_status == "VALID",
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
        ):
            days.setdefault(adj, set()).add(at.astimezone(sources.EASTERN).date())
        raw, split = days.get("RAW", set()), days.get("SPLIT", set())
        if not raw:
            no_bars.append(symbol)
            continue
        if raw != split:
            mismatch.append(symbol)
        last = max(raw)
        for f, t in spans[asset_id]:
            expected = [d.day for d in sessions_between(f, min(t, last))]
            missing = [d for d in expected if d not in raw]
            if len(missing) > 5:
                gaps.append(
                    {
                        "symbol": symbol,
                        "span": [f.isoformat(), t.isoformat()],
                        "missing_sessions": len(missing),
                    }
                )
    return {
        "status": _status(not no_bars and not dup, warn=bool(gaps or mismatch)),
        "members": len(by_symbol),
        "members_without_bars": no_bars,
        "duplicate_bar_keys": dup,
        "raw_split_session_mismatch": mismatch[:50],
        "raw_split_session_mismatch_count": len(mismatch),
        "members_with_over_5_missing_sessions_in_span": gaps[:50],
        "gap_count": len(gaps),
    }


def corporate_actions(session: Session) -> dict[str, Any]:
    """RAW/SPLIT close ratio changes versus provider forward/reverse split actions."""
    b = MarketBarRecord
    targets = members(session, current=True)
    changes: list[dict[str, Any]] = []
    for asset_id, symbol in targets:
        rows = session.execute(
            select(b.adjustment, b.event_time, b.close).where(
                b.asset_id == asset_id,
                b.feed == "sip",
                b.timeframe == "1Day",
                b.quality_status == "VALID",
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
        ).all()
        by_day: dict[date, dict[str, float]] = defaultdict(dict)
        for adj, at, close in rows:
            by_day[at.astimezone(sources.EASTERN).date()][adj] = float(close)
        prev = None
        for d in sorted(by_day):
            v = by_day[d]
            if "RAW" in v and "SPLIT" in v and v["SPLIT"] > 0:
                ratio = v["RAW"] / v["SPLIT"]
                if prev is not None and abs(ratio / prev - 1) > 0.01:
                    changes.append({"symbol": symbol, "session": d, "factor": prev / ratio})
                prev = ratio
    actions: dict[str, list[tuple[date, float]]] = defaultdict(list)
    symbols = sorted({c["symbol"] for c in changes})
    with sources.alpaca_client() as client:
        for i in range(0, len(symbols), 50):
            for typ in ("forward_split", "reverse_split"):
                r = client.get(
                    f"{sources.DATA_API_BASE_URL}/v1/corporate-actions",
                    params={
                        "symbols": ",".join(symbols[i : i + 50]),
                        "types": typ,
                        "start": BARS_START.isoformat(),
                        "end": BARS_END.isoformat(),
                        "limit": "1000",
                    },
                )
                if r.status_code != 200:
                    continue
                for rows in (r.json().get("corporate_actions") or {}).values():
                    for a in rows:
                        actions[a["symbol"]].append(
                            (
                                date.fromisoformat(a["ex_date"]),
                                float(a["new_rate"]) / float(a["old_rate"]),
                            )
                        )
    matched, unmatched = 0, []
    for ch in changes:
        ok = any(
            abs((ex - ch["session"]).days) <= 3 and abs(rate / ch["factor"] - 1) < 0.02
            for ex, rate in actions.get(ch["symbol"], [])
        )
        if ok:
            matched += 1
        else:
            unmatched.append({**ch, "session": ch["session"].isoformat()})
    return {
        "status": _status(not unmatched, warn=True),
        "ratio_changes": len(changes),
        "matched_provider_split_actions": matched,
        "unmatched": unmatched[:50],
        "note": "an unmatched change is a split the provider's action history lacks, or a "
        "data issue; both are listed, none is corrected",
    }


def classification_coverage(session: Session) -> dict[str, Any]:
    c = ResearchUniverseCandidateRecord
    k = RiskAssetClassificationRecord
    out = {}
    for formation in sorted(
        set(
            session.scalars(
                select(c.formation_session).where(
                    c.universe_code == CURRENT_UNIVERSES[0], c.member.is_(True)
                )
            )
        )
    ):
        ids = list(
            session.scalars(
                select(c.asset_id).where(
                    c.universe_code == CURRENT_UNIVERSES[0],
                    c.member.is_(True),
                    c.formation_session == formation,
                )
            )
        )
        classified = set(
            session.scalars(
                select(k.asset_id).where(
                    k.asset_id.in_(ids),
                    k.valid_from
                    <= datetime(
                        formation.year, formation.month, formation.day, 23, 59, 59, tzinfo=UTC
                    ),
                )
            )
        )
        out[formation.isoformat()] = {
            "members": len(ids),
            "classified": len(classified),
            "share": round(len(classified) / max(len(ids), 1), 3),
        }
    worst = min((v["share"] for v in out.values()), default=0)
    return {"status": _status(worst >= 0.9, warn=True), "by_formation": out}


def dataset_sha256(session: Session, as_of: datetime | None = None) -> str:
    """Fingerprint of the research dataset: rules, candidates, securities, member bars.

    ``as_of`` recomputes it over the rows that existed then (research rows by ``created_at``,
    bars by ``received_at``): the tables are append-only, so a reservation's recorded hash
    stays verifiable after later additions."""
    u, c, s, b = (
        ResearchUniverseRecord,
        ResearchUniverseCandidateRecord,
        ResearchSecurityRecord,
        MarketBarRecord,
    )
    digest = hashlib.sha256()
    q_u = select(u).order_by(u.code)
    q_c = select(
        c.universe_code,
        c.formation_session,
        c.symbol,
        c.asset_id,
        c.eligible,
        c.rank,
        c.member,
        c.effective_from,
        c.effective_to,
    ).order_by(c.id)
    q_s = select(s.id, s.symbol, s.revision, s.delisted_on).order_by(s.id)
    q_m = select(c.asset_id).where(c.member.is_(True), c.asset_id.is_not(None)).distinct()
    if as_of is not None:
        q_u = q_u.where(u.created_at <= as_of)
        q_c = q_c.where(c.created_at <= as_of)
        q_s = q_s.where(s.created_at <= as_of)
        q_m = q_m.where(c.created_at <= as_of)
    for rule in session.scalars(q_u):
        digest.update(f"u|{rule.code}|{rule.rule_sha256}\n".encode())
    for r in session.execute(q_c):
        digest.update(("c|" + "|".join(str(x) for x in r) + "\n").encode())
    for srow in session.execute(q_s):
        digest.update(("s|" + "|".join(str(x) for x in srow) + "\n").encode())
    ids = list(session.scalars(q_m))
    q_b = (
        select(b.id, b.asset_id, b.adjustment, b.event_time, b.close)
        .where(
            b.asset_id.in_(ids),
            b.feed == "sip",
            b.timeframe == "1Day",
            b.event_time <= END_TS,
        )
        .order_by(b.id)
    )
    if as_of is not None:
        q_b = q_b.where(b.received_at <= as_of)
    for brow in session.execute(q_b):
        digest.update(("b|" + "|".join(str(x) for x in brow) + "\n").encode())
    return digest.hexdigest()


def reservation_hashes(session: Session) -> dict[str, Any]:
    """Each reservation's recorded dataset hash, recomputed as of its creation."""
    r = ResearchDataReservationRecord
    out = {}
    for res in session.scalars(select(r).where(r.dataset_sha256.is_not(None)).order_by(r.code)):
        again = dataset_sha256(session, as_of=res.created_at)
        out[res.code] = {"recorded": res.dataset_sha256, "reproduced": again == res.dataset_sha256}
    ok = all(v["reproduced"] for v in out.values())
    return {"status": _status(ok), "reservations": out}


def placeholders(session: Session) -> dict[str, Any]:
    """Zero-volume placeholder bars (the provider repeats the last close when a ticker does
    not trade): inside a current member's span before its recorded delisting they would be
    held positions on a non-trading security; single sessions are trading halts."""
    b = MarketBarRecord
    c = ResearchUniverseCandidateRecord
    # The replay's own lookup: what a run would see.
    member_assets = [
        a
        for a in session.scalars(
            select(c.asset_id)
            .where(c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES))
            .distinct()
        )
        if a is not None
    ]
    delisted: dict[Any, date] = dict(
        store.delistings(session, member_assets, known_at=datetime.now(UTC))
    )
    runs: list[dict[str, Any]] = []
    halts = 0
    for asset_id, symbol, f, t in session.execute(
        select(c.asset_id, c.symbol, c.effective_from, c.effective_to).where(
            c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES)
        )
    ):
        if f is None or t is None:  # a member always has a span (table check)
            continue
        end = min(t, delisted.get(asset_id, t))
        days = sorted(
            at.astimezone(sources.EASTERN).date()
            for (at,) in session.execute(
                select(b.event_time).where(
                    b.asset_id == asset_id,
                    b.feed == "sip",
                    b.timeframe == "1Day",
                    b.adjustment == "RAW",
                    b.volume == 0,
                    b.event_time >= datetime(f.year, f.month, f.day, tzinfo=UTC),
                    b.event_time <= datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC),
                )
            )
        )
        if len(days) > 3:
            runs.append({"symbol": symbol, "span": [f, t], "zero_volume_sessions": len(days)})
        else:
            halts += len(days)
    return {
        "status": _status(not runs),
        "members_with_placeholder_runs_in_span": runs,
        "single_session_halts_in_spans": halts,
    }


def identity(session: Session) -> dict[str, Any]:
    """One security per member: no two members of a formation share a formation fingerprint
    (last close, median dollar volume) or an asset row, and each member's loaded RAW close
    on its formation session equals the close its formation scan saw (the scan and the load
    identified the same security)."""
    c = ResearchUniverseCandidateRecord
    b = MarketBarRecord
    out: dict[str, Any] = {}
    ok = True
    for code in CURRENT_UNIVERSES:
        rows = session.execute(
            select(
                c.formation_session, c.symbol, c.asset_id, c.last_close, c.median_dollar_volume
            ).where(c.universe_code == code, c.member.is_(True))
        ).all()
        groups: dict[Any, list[str]] = defaultdict(list)
        assets: dict[Any, list[str]] = defaultdict(list)
        mismatch, missing = [], []
        for f, sym, asset_id, close, dvol in rows:
            groups[(f, close, dvol)].append(sym)
            assets[(f, asset_id)].append(sym)
            loaded = session.scalar(
                select(b.close)
                .where(
                    b.asset_id == asset_id,
                    b.feed == "sip",
                    b.timeframe == "1Day",
                    b.adjustment == "RAW",
                    b.event_time >= datetime(f.year, f.month, f.day, tzinfo=UTC),
                    b.event_time <= datetime(f.year, f.month, f.day, 23, 59, 59, tzinfo=UTC),
                )
                .order_by(b.id)
                .limit(1)
            )
            if loaded is None:
                missing.append(f"{sym}@{f}")
            elif close is not None and abs(float(loaded) / float(close) - 1) > 0.001:
                mismatch.append(f"{sym}@{f}")
        dup_fp = [v for v in groups.values() if len(v) > 1]
        dup_asset = [v for v in assets.values() if len(v) > 1]
        ok = ok and not (dup_fp or dup_asset or mismatch)
        out[code] = {
            "member_formations": len(rows),
            "duplicate_fingerprints": dup_fp,
            "duplicate_assets": dup_asset,
            "scan_load_close_mismatch": mismatch,
            "no_bar_on_formation_session": missing,
        }
    return {"status": _status(ok), **out}


JUMP = 0.35


def discontinuities(session: Session) -> dict[str, Any]:
    """One-session moves above 35% in split-adjusted closes (traded sessions) inside current
    members' spans, up to a recorded delisting. A merger's conversion ratio carried into an
    entity history, an unadjusted spin-off, or a genuine move: listed for review, never
    corrected."""
    b = MarketBarRecord
    c = ResearchUniverseCandidateRecord
    spans: dict[Any, list[tuple[date, date, str]]] = defaultdict(list)
    for asset_id, sym, f, t in session.execute(
        select(c.asset_id, c.symbol, c.effective_from, c.effective_to).where(
            c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES)
        )
    ):
        if f is not None and t is not None:
            spans[asset_id].append((f, t, sym))
    delisted = store.delistings(session, list(spans), known_at=datetime.now(UTC))
    jumps: list[dict[str, Any]] = []
    for asset_id, ranges in spans.items():
        closes: dict[date, float] = {}
        for at, close in session.execute(
            select(b.event_time, b.close)
            .where(
                b.asset_id == asset_id,
                b.feed == "sip",
                b.timeframe == "1Day",
                b.adjustment == "SPLIT",
                b.quality_status == "VALID",
                b.volume > 0,
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
            .order_by(b.id)
        ):
            closes.setdefault(at.astimezone(sources.EASTERN).date(), float(close))
        days = sorted(closes)
        last = delisted.get(asset_id)
        for prev, day in itertools.pairwise(days):
            if last is not None and day > last:
                break
            if not any(f <= day <= t for f, t, _ in ranges):
                continue
            move = closes[day] / closes[prev] - 1
            if abs(move) > JUMP:
                sym = next(s for f, t, s in ranges if f <= day <= t)
                jumps.append({"symbol": sym, "session": day.isoformat(), "move": round(move, 3)})
    dividend_explained(session, jumps)
    return {
        "status": _status(not jumps, warn=True),
        "threshold": JUMP,
        "count": len(jumps),
        "moves": sorted(jumps, key=lambda j: (j["symbol"], j["session"])),
    }


def _series(session: Session, asset_id: Any, adjustment: str) -> dict[date, float]:
    b = MarketBarRecord
    out: dict[date, float] = {}
    for at, close in session.execute(
        select(b.event_time, b.close)
        .where(
            b.asset_id == asset_id,
            b.feed == "sip",
            b.timeframe == "1Day",
            b.adjustment == adjustment,
            b.quality_status == "VALID",
            b.volume > 0,
            b.event_time >= START_TS,
            b.event_time <= END_TS,
        )
        .order_by(b.id)
    ):
        out.setdefault(at.astimezone(sources.EASTERN).date(), float(close))
    return out


def inferred_splits(session: Session, asset_id: Any) -> list[dict[str, Any]]:
    """Every split the replay would infer for one asset over 2016-2019, each tested against
    the RAW move (``phantom`` when it is off by more than 1.5x; see split_consistency)."""
    raw, split = _series(session, asset_id, "RAW"), _series(session, asset_id, "SPLIT")
    days = sorted(d for d in raw if d in split and split[d] > 0)
    out: list[dict[str, Any]] = []
    for prev, day in itertools.pairwise(days):
        before, after = raw[prev] / split[prev], raw[day] / split[day]
        if abs(after / before - 1) <= 0.01:
            continue
        ratio = before / after  # new shares per old
        implied = raw[prev] / ratio
        out.append(
            {
                "session": day.isoformat(),
                "previous_session": prev.isoformat(),
                "ratio": ratio,
                "raw_previous": raw[prev],
                "raw_close": raw[day],
                "split_previous": split[prev],
                "split_close": split[day],
                "raw_move": raw[day] / raw[prev] - 1,
                "split_series_move": split[day] / split[prev] - 1,
                "phantom": not 2 / 3 <= raw[day] / implied <= 3 / 2,
            }
        )
    return out


def split_consistency(session: Session) -> dict[str, Any]:
    """Every split the replay would infer (a change of RAW/SPLIT between consecutive traded
    sessions, as ``backtesting.data`` does) tested against the RAW price itself: a split of
    r new shares per old moves the RAW close by about 1/r. An inferred split whose RAW move
    REDACTED_FOR_GITHUB
    REDACTED_FOR_GITHUB
    The replay would rescale held quantities by it. Listed for every current member over
    2016-2019 (lookback features read outside membership spans too), never corrected."""
    c = ResearchUniverseCandidateRecord
    rows = session.execute(
        select(c.asset_id, c.symbol, c.effective_from, c.effective_to).where(
            c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES)
        )
    ).all()
    spans: dict[Any, list[tuple[date, date, str]]] = defaultdict(list)
    for asset_id, sym, f, t in rows:
        if f is not None and t is not None:
            spans[asset_id].append((f, t, sym))
    events, phantoms = 0, []
    for asset_id, ranges in spans.items():
        inferred = inferred_splits(session, asset_id)
        events += len(inferred)
        for e in inferred:
            if e["phantom"]:
                day = date.fromisoformat(e["session"])
                phantoms.append(
                    {
                        "symbol": ranges[0][2],
                        "asset_id": str(asset_id),
                        "session": e["session"],
                        "inferred_ratio": round(e["ratio"], 4),
                        "raw_move": round(e["raw_move"], 3),
                        "in_member_span": any(f <= day <= t for f, t, _ in ranges),
                    }
                )
    return {
        "status": _status(not phantoms),
        "inferred_split_events": events,
        "phantom_splits": len(phantoms),
        "phantom_splits_in_member_spans": sum(1 for p in phantoms if p["in_member_span"]),
        "phantoms": sorted(phantoms, key=lambda p: (p["symbol"], p["session"])),
        "note": "a phantom split would rescale a held position's quantity in the replay "
        "(backtesting.engine._split) and a split-adjusted lookback; the affected "
        "member-sessions need handling in a pre-registration before any reserved run",
    }


def phantom_disposition(
    event: date, spans: list[tuple[date, date]], excluded: list[str], delisted_on: date | None
) -> str:
    """How the replay is kept off a phantom split of one asset (D-240), or ``UNCOVERED``.

    * excluded: a registered exclusion removes the asset from every research universe and
      every reserved run, so no decision reads its history and no position holds it;
    * unreachable: the security master's delisting precedes the event and every member span
      ends before it. The replay closes positions at the delisting and a decision reads only
      bars up to its own session, so nothing after the delisting is ever used.

    Anything else is uncovered, whatever its distance from a span: a span starting after the
    event reads it through any lookback long enough, and a position from a span ending
    before it can still be held across it (leaving the universe does not close a position).
    """
    if excluded:
        return "excluded"
    if delisted_on is not None and delisted_on < event and all(t < event for _, t in spans):
        return "unreachable"
    return "UNCOVERED"


def phantom_split_coverage(session: Session, phantoms: list[dict[str, Any]]) -> dict[str, Any]:
    """Every phantom split of a current member is excluded or unreachable (D-240). The
    split_consistency check above still reports the data defect itself, unchanged."""
    import uuid

    now = datetime.now(UTC)
    excluded = store.exclusions(session, known_at=now)
    c = ResearchUniverseCandidateRecord
    rows: list[dict[str, Any]] = []
    for p in phantoms:
        asset_id = uuid.UUID(p["asset_id"])
        spans = [
            (f, t)
            for f, t in session.execute(
                select(c.effective_from, c.effective_to).where(
                    c.asset_id == asset_id,
                    c.member.is_(True),
                    c.universe_code.in_(CURRENT_UNIVERSES),
                )
            )
            if f is not None and t is not None
        ]
        delisted_on = store.delistings(session, [asset_id], known_at=now).get(asset_id)
        event = date.fromisoformat(p["session"])
        rows.append(
            {
                "symbol": p["symbol"],
                "asset_id": p["asset_id"],
                "session": p["session"],
                "in_member_span": p["in_member_span"],
                "disposition": phantom_disposition(
                    event, spans, excluded.get(asset_id, []), delisted_on
                ),
                "exclusions": excluded.get(asset_id, []),
                "delisted_on": delisted_on.isoformat() if delisted_on else None,
            }
        )
    uncovered = [r for r in rows if r["disposition"] == "UNCOVERED"]
    members_excluded = {
        code: sum(
            1
            for a in store.universe_members(session, code, known_at=now, apply_exclusions=False)
            if a in excluded
        )
        for code in CURRENT_UNIVERSES
    }
    return {
        "status": _status(not uncovered),
        "phantom_splits": len(rows),
        "excluded": sum(1 for r in rows if r["disposition"] == "excluded"),
        "unreachable": sum(1 for r in rows if r["disposition"] == "unreachable"),
        "uncovered": len(uncovered),
        "excluded_assets_per_universe": members_excluded,
        "phantoms": rows,
    }


def dividend_explained(session: Session, moves: list[dict[str, Any]]) -> None:
    """Mark each discontinuity a stored cash dividend of that symbol and ex-date explains
    (the replay credits it: the RAW drop is not a loss)."""
    from app.database.models import CashDividendRecord

    d = CashDividendRecord
    for m in moves:
        rate = session.scalar(
            select(func.max(d.rate)).where(
                d.symbol == m["symbol"], d.ex_date == date.fromisoformat(m["session"])
            )
        )
        m["cash_dividend_on_ex_date"] = None if rate is None else float(rate)


def reused_classifications(session: Session) -> dict[str, Any]:
    """Research classifications on a reused ticker's formation-era row (its CIK cannot come
    from today's ticker list): expected none."""
    k = RiskAssetClassificationRecord
    a = Asset
    rows = session.execute(
        select(a.symbol, k.sector, k.valid_from)
        .join(a, a.id == k.asset_id)
        .where(k.created_by == "operator:research-foundation", a.name.like("%ticker since reused)"))
    ).all()
    return {
        "status": _status(not rows),
        "classified_reused_rows": [(sym, sec, v.date().isoformat()) for sym, sec, v in rows],
    }


def integrity(session: Session) -> dict[str, Any]:
    rules = {
        u.code: u.rule_sha256 == rule_sha(u.rule)
        for u in session.scalars(select(ResearchUniverseRecord))
    }
    triggers = {
        t: bool(
            session.scalar(
                text("select count(*) from pg_trigger where tgname = :n"), {"n": f"{t}_append_only"}
            )
        )
        for t in TABLES
    }
    first = dataset_sha256(session)
    second = dataset_sha256(session)
    ok = all(rules.values()) and all(triggers.values()) and first == second
    return {
        "status": _status(ok),
        "rule_hashes_match": rules,
        "append_only_triggers": triggers,
        "dataset_sha256": first,
        "dataset_hash_stable": first == second,
    }


def run(factory: sessionmaker[Session], path: Path) -> dict[str, Any]:
    with factory() as session:
        splits = split_consistency(session)
        report = {
            "survivorship": survivorship(session),
            "delisting_review": delisting_review(session),
            "look_ahead": look_ahead(session),
            "bars": bars_quality(session),
            "corporate_actions": corporate_actions(session),
            "classification": classification_coverage(session),
            "identity": identity(session),
            "discontinuities": discontinuities(session),
            "split_consistency": splits,
            "phantom_split_coverage": phantom_split_coverage(session, splits["phantoms"]),
            "reused_ticker_classifications": reused_classifications(session),
            "placeholder_bars": placeholders(session),
            "integrity": integrity(session),
            "reservation_hashes": reservation_hashes(session),
            "universes": list(CURRENT_UNIVERSES),
        }
    path.write_text(json.dumps(report, indent=1, default=str))
    return report


__all__ = [
    "KNOWN_DELISTINGS",
    "dataset_sha256",
    "phantom_disposition",
    "reservation_hashes",
    "run",
]
