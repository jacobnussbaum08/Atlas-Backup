"""Point-in-time security identity: ``RESEARCH_US_EQUITY_V4`` (D-239). Simulation database only.

The provider maps a requested ticker to a security through ``asof``. By default it maps to
the ticker's *current* holder (with that holder's history under earlier tickers) and falls
back to the raw ticker when the holder has no bars in the requested range. Universes formed
that way (equity V1-V3) contain, at every formation:

* duplicates: a renamed company under its old ticker (raw) and its new one (mapped), e.g.
  REDACTED_FOR_GITHUB
* shadowed securities: the security that actually held a ticker at the formation is
  REDACTED_FOR_GITHUB
  REDACTED_FOR_GITHUB
* request-dependent identity: a short formation window and a full-range load can map one
  REDACTED_FOR_GITHUB

V4 identifies each candidate explicitly:

1. every listed ticker *as of the formation session* (``asof`` = formation): the security
   trading under that ticker then, with its history under later tickers;
2. plus every ticker mapped *as of* ``MAPPED_ASOF`` (its current holder) whose formation
   fingerprint (last close, median dollar volume) no formation-date security already has: a
   company that has since been renamed and whose old ticker the provider no longer lists
   REDACTED_FOR_GITHUB
   ticker meant another security at the formation;
3. members' full 2016-2019 histories are fetched with the same mapping; identical histories
   are one entity: one asset row across formations and renames, one security record
   (provider ``alpaca-asof``) with the request (ticker, asof), the entity hash and its
   delisting from its last traded bar. An existing asset whose stored bars are exactly that
   history is reused; otherwise a new identity row is created for the loader.

Typing, eligibility thresholds, formations and sizes are those of V3.
"""

from __future__ import annotations

import hashlib
import statistics
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_back, sessions_between
from app.database.models import (
    Asset,
    MarketBarRecord,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
)
from app.research import sources
from app.research.universe import (
    CREATED_BY,
    EQUITY_RULE_V3,
    ETF_RULE_V4,
    EXPLORATION_SYMBOLS,
    HISTORY_START,
    eligible_symbol,
    rule_sha,
    scan,
)

PROVIDER = "alpaca-asof"
MAPPED_ASOF = date(2026, 11, 14)  # the "current holder" mapping, pinned for reproducibility
BARS_START, BARS_END = date(2016, 1, 4), date(2019, 12, 31)
FINAL_WINDOW = date(2019, 12, 16)

EQUITY_RULE_V4: dict[str, Any] = {
    **EQUITY_RULE_V3,
    "code": "RESEARCH_US_EQUITY_V4",
    "version": 4,
    "identity": "each ticker as of the formation session (provider asof = formation); plus "
    f"tickers mapped as of {MAPPED_ASOF.isoformat()} (current holder) whose formation "
    "fingerprint (last close, median dollar volume) no formation-date security has, labelled "
    "TICKER, or TICKER~CUR when the ticker meant another security then; members with "
    "identical 2016-2019 histories are one entity",
    "mapped_asof": MAPPED_ASOF.isoformat(),
    "supersedes": "RESEARCH_US_EQUITY_V3 (current-holder symbol mapping: duplicated renamed "
    "companies, shadowed formation-era securities)",
}
ETF_RULE_V5: dict[str, Any] = {
    **ETF_RULE_V4,
    "code": "RESEARCH_ETF_CANDIDATES_V5",
    "version": 5,
    "identity": EQUITY_RULE_V4["identity"],
    "mapped_asof": MAPPED_ASOF.isoformat(),
    "supersedes": "REDACTED_FOR_GITHUB"
    "different funds under the default symbol mapping)",
}


def _num(x: Any) -> str:
    return f"{Decimal(str(x)).normalize():f}"


def fingerprint(obs: list[tuple[float, float]]) -> tuple[str, str]:
    """(last close, median dollar volume) of a formation window, as stored in candidates."""
    return (
        f"{Decimal(str(round(obs[-1][0], 6))):f}",
        f"{Decimal(str(round(statistics.median(d for _, d in obs), 2))):f}",
    )


def series_sha(rows: list[tuple[date, Any]]) -> str:
    """Entity hash: the traded RAW closes of 2016-2019, one line per session."""
    lines = sorted(f"{d.isoformat()}|{_num(c)}" for d, c in rows)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _histories(
    client: Any, requests: set[tuple[str, date]]
) -> dict[tuple[str, date], list[tuple[date, float]]]:
    """Full-range traded RAW closes per (ticker, asof). A ticker missing from a batch's
    response (two tickers mapped to one security) is fetched alone."""
    out: dict[tuple[str, date], list[tuple[date, float]]] = {}
    by_asof: dict[date, list[str]] = defaultdict(list)
    for t, a in sorted(requests):
        by_asof[a].append(t)

    def fetch(tickers: list[str], asof: date) -> dict[str, list[tuple[date, float]]]:
        got: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for sym, bar in sources.daily_bars(client, tickers, BARS_START, BARS_END, asof):
            if float(bar.get("v") or 0) > 0:
                got[sym].append((date.fromisoformat(bar["t"][:10]), float(bar["c"])))
        return got

    for asof, tickers in sorted(by_asof.items()):
        for i in range(0, len(tickers), 50):
            batch = tickers[i : i + 50]
            got = fetch(batch, asof)
            for t in batch:
                rows = got.get(t) or fetch([t], asof).get(t, [])
                out[(t, asof)] = rows
    return out


def _stored_sha(session: Session, asset_id: uuid.UUID) -> str | None:
    b = MarketBarRecord
    seen: dict[date, Any] = {}
    for at, close in session.execute(
        select(b.event_time, b.close)
        .where(
            b.asset_id == asset_id,
            b.feed == "sip",
            b.timeframe == "1Day",
            b.adjustment == "RAW",
            b.quality_status == "VALID",
            b.volume > 0,
            b.source == "HISTORICAL_REST",
        )
        .order_by(b.id)
    ):
        d = at.astimezone(sources.EASTERN).date()
        if BARS_START <= d <= BARS_END:
            seen.setdefault(d, close)
    return series_sha(list(seen.items())) if seen else None


def form(
    factory: sessionmaker[Session], rule: dict[str, Any] = EQUITY_RULE_V4, progress: Any = print
) -> dict[str, Any]:
    formations = [date.fromisoformat(d) for d in rule["formations"]]
    until = date.fromisoformat(rule["until"])
    etf = rule["code"].startswith("RESEARCH_ETF_CANDIDATES")
    excluded = set(EXPLORATION_SYMBOLS) | (set() if etf else {"SPY"})
    with factory() as session:
        s = ResearchSecurityRecord
        master: dict[str, ResearchSecurityRecord] = {}
        for known in session.scalars(
            select(s).where(s.provider == "alpaca").order_by(s.symbol, s.revision)
        ):
            master[known.symbol] = known
        session.expunge_all()
        if session.scalar(select(ResearchUniverseRecord.id).where(
            ResearchUniverseRecord.code == rule["code"]
        )):  # fmt: skip
            raise RuntimeError(f"{rule['code']} already exists")
    tickers = sorted(
        t
        for t, r in master.items()
        if (r.security_type == "ETF") == etf and eligible_symbol(t) and t not in excluded
    )
    rows_by_f: dict[date, list[dict[str, Any]]] = {}
    with sources.alpaca_client() as client:
        for f in formations:
            window = [(sessions_back(f, rule["window_sessions"] - 1), f)]
            if etf:  # fund history requirement, under the same mapping
                window.append((BARS_START, HISTORY_START))
            pit = scan(client, tickers, window, traded_only=True, asof_date=f)
            cur = scan(client, tickers, window, traded_only=True, asof_date=MAPPED_ASOF)
            entries: dict[str, dict[str, Any]] = {}
            fps: set[tuple[str, str]] = set()
            cur_fp = {t: fingerprint(o) for (t, w), o in cur.window.items() if o and w == f}
            for (t, w), obs in sorted(pit.window.items()):
                if not obs or w != f:
                    continue
                fp = fingerprint(obs)
                fps.add(fp)
                entries[t] = {
                    "ticker": t,
                    "asof": f,
                    "obs": obs,
                    "same": cur_fp.get(t) == fp,
                    "first": pit.first_bar.get(t),
                }
            for (t, w), obs in sorted(cur.window.items()):
                if not obs or w != f or fingerprint(obs) in fps:
                    continue
                fps.add(fingerprint(obs))
                label = t if t not in entries else f"{t}~CUR"
                entries[label] = {
                    "ticker": t,
                    "asof": MAPPED_ASOF,
                    "obs": obs,
                    "same": True,
                    "first": cur.first_bar.get(t),
                }
            rows = []
            for label, e in sorted(entries.items()):
                obs = e["obs"]
                close, dvol = obs[-1][0], statistics.median(d for _, d in obs)
                reason = None
                if len(obs) < rule["min_bars"]:
                    reason = "too_few_bars"
                elif close < float(rule["min_close"]):
                    reason = "price_below_minimum"
                elif dvol < float(rule["min_median_dollar_volume"]):
                    reason = "dollar_volume_below_minimum"
                elif etf and (e["first"] or date.max) > HISTORY_START:
                    reason = "history_shorter_than_one_year"
                rows.append({**e, "label": label, "close": close, "dvol": dvol, "reason": reason})
            ranked = sorted(
                (r for r in rows if r["reason"] is None), key=lambda r: (-r["dvol"], r["label"])
            )
            for i, r in enumerate(ranked):
                r["rank"] = i + 1
                r["member"] = i < rule["members"]
            rows_by_f[f] = rows
            progress(f"{f}: {len(rows)} candidates, {len(ranked)} eligible")
        needed = {
            (r["ticker"], r["asof"]) for rows in rows_by_f.values() for r in rows if r.get("member")
        }
        histories = _histories(client, needed)
    progress(f"histories for {len(histories)} member identities")
    retrieved = sources.now_utc()
    counts: dict[str, Any] = {}
    with factory() as session, session.begin():
        session.add(
            ResearchUniverseRecord(
                code=rule["code"],
                version=rule["version"],
                description=rule["identity"],
                rule=rule,
                rule_sha256=rule_sha(rule),
                created_by=CREATED_BY,
            )
        )
        entity_of = {k: series_sha(v) for k, v in histories.items()}
        records: dict[str, ResearchSecurityRecord] = {}
        reused = created = 0
        used_labels: set[str] = set()
        for f in formations:
            nxt = formations[formations.index(f) + 1] if f != formations[-1] else until
            sessions = sessions_between(f, nxt)
            effective_from = sessions[1].day if len(sessions) > 1 else nxt
            members = 0
            for r in rows_by_f[f]:
                member = bool(r.get("member"))
                sec_id = asset_id = None
                if member:
                    key = (r["ticker"], r["asof"])
                    sha = entity_of[key]
                    rec = records.get(sha)
                    if rec is None:
                        hist = histories[key]
                        # A ticker-named row is reused only for the security that holds
                        # the ticker's name in this identity (dividends match by symbol).
                        plain = r["label"] == r["ticker"]
                        asset = _matching_asset(session, r["ticker"], sha) if plain else None
                        if asset is None:
                            asset = _new_asset(session, r["label"], r["ticker"], r["asof"])
                            created += 1
                        else:
                            reused += 1
                        label = r["label"]
                        while label in used_labels:
                            label += "#"
                        used_labels.add(label)
                        last = hist[-1][0] if hist else None
                        typed = master.get(r["ticker"])
                        rec = ResearchSecurityRecord(
                            provider=PROVIDER,
                            symbol=label,
                            revision=0,
                            provider_asset_id=None,
                            name=None,
                            exchange=None,
                            provider_status="asof-identity",
                            security_type=typed.security_type if typed and r["same"] else "UNKNOWN",
                            type_basis=(
                                (typed.type_basis if typed else "not in the security master")
                                if r["same"]
                                else "formation-era security of a ticker now held by another; "
                                "treated as non-fund"
                            ),
                            cik=None,
                            scan_start=BARS_START,
                            scan_end=BARS_END,
                            first_bar=hist[0][0] if hist else None,
                            last_bar=last,
                            delisted_on=last if last and last < FINAL_WINDOW else None,
                            delisting_basis=(
                                f"entity history ({r['ticker']} as of {r['asof']}) has no traded "
                                f"bar after {last}; reason unknown"
                                if last and last < FINAL_WINDOW
                                else None
                            ),
                            asset_id=asset.id,
                            retrieved_at=retrieved,
                            details={
                                "request_symbol": r["ticker"],
                                "asof": r["asof"].isoformat(),
                                "entity_sha256": sha,
                                "same_as_current_holder": r["same"],
                                "traded_sessions": len(hist),
                            },
                        )
                        session.add(rec)
                        session.flush()
                        records[sha] = rec
                    sec_id, asset_id = rec.id, rec.asset_id
                members += member
                session.add(
                    ResearchUniverseCandidateRecord(
                        universe_code=rule["code"],
                        formation_session=f,
                        symbol=r["label"],
                        security_id=sec_id,
                        asset_id=asset_id,
                        last_close=Decimal(str(round(r["close"], 6))),
                        median_dollar_volume=Decimal(str(round(r["dvol"], 2))),
                        sessions_observed=len(r["obs"]),
                        eligible=r["reason"] is None,
                        ineligible_reason=r["reason"],
                        rank=r.get("rank"),
                        member=member,
                        effective_from=effective_from if member else None,
                        effective_to=nxt if member else None,
                    )
                )
            counts[f.isoformat()] = members
        counts["entities"] = len(records)
        counts["assets_reused"] = reused
        counts["assets_created"] = created
        counts["mapped_members"] = sum(
            1
            for rows in rows_by_f.values()
            for r in rows
            if r.get("member") and r["asof"] == MAPPED_ASOF
        )
    return counts


def _matching_asset(session: Session, ticker: str, sha: str) -> Asset | None:
    """An existing asset row of the ticker whose stored bars are exactly this history."""
    for asset in session.scalars(
        select(Asset)
        .where(Asset.symbol == ticker, Asset.asset_class.in_(("EQUITY", "ETF")))
        .order_by(Asset.status != "ACTIVE", Asset.created_at)
    ):
        if _stored_sha(session, asset.id) == sha:
            return asset
    return None


def _new_asset(session: Session, label: str, ticker: str, asof: date) -> Asset:
    asset = Asset(
        id=uuid.uuid4(),
        symbol=label,
        asset_class="EQUITY",
        name=f"{label} (research identity: {ticker} as of {asof.isoformat()})",
        exchange_code=None,
        quote_currency="USD",
        status="INACTIVE",
        instrument_type="UNCLASSIFIED",
        metadata_source="research-identity",
    )
    session.add(asset)
    session.flush()
    return asset


__all__ = [
    "EQUITY_RULE_V4",
    "ETF_RULE_V5",
    "MAPPED_ASOF",
    "PROVIDER",
    "fingerprint",
    "form",
    "series_sha",
]
