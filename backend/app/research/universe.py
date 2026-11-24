"""Security master and reconstructed research universes (D-239).

Two universes, each a fixed rule stored with its SHA-256 (``research_universes``) and
applied at each formation session to data available by that session only:

* ``RESEARCH_US_EQUITY_V1`` (reserved V2 universe, 2016-2019 stage): US-listed securities on
  NYSE, NASDAQ, NYSE American, NYSE Arca and Cboe BZX, excluding funds and ETFs, the 21
  exploration symbols and SPY. Eligible: >= 50 daily bars in the 60 sessions ending at the
  formation, last close >= $5, median daily dollar volume >= $1M. Members: the 500 most
  liquid by median dollar volume, effective from the next session to the next formation.
* ``RESEARCH_ETF_CANDIDATES_V1`` (for H-B): exchange-traded funds by the same eligibility,
  formed once on the last session of 2015, trading since at least 2015-01-02, the 150 most
  liquid, effective 2016-01-04..2019-12-31. H-B's pre-registration chooses its set from
  these by a rule.

Candidates come from the provider's active and inactive listings. Securities delisted
before the provider's inactive list begins are missing (survivorship gap): the validation
measures it against a list of known delistings. Membership uses only bars up to the
formation session; delisting dates come from the bars (the last session traded).
"""

from __future__ import annotations

import hashlib
import json
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_back, sessions_between
from app.database.models import (
    Asset,
    ResearchSecurityRecord,
    ResearchUniverseCandidateRecord,
    ResearchUniverseRecord,
)
from app.research import sources

PROVIDER = "alpaca"
MAJOR_EXCHANGES = ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
EXPLORATION_SYMBOLS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
SCAN_START, SCAN_END = date(2015, 1, 2), date(2019, 12, 31)
CREATED_BY = "operator:research-foundation"

EQUITY_RULE: dict[str, Any] = {
    "code": "RESEARCH_US_EQUITY_V1",
    "version": 1,
    "formations": ["2015-12-31", "2016-12-30", "2017-12-29", "2018-12-31"],
    "until": "2019-12-31",
    "candidates": "provider US equity assets, active and inactive, on "
    + "/".join(MAJOR_EXCHANGES)
    + "; excluding registered funds/ETFs (SEC fund list or 'ETF'/'ETN' in the name), symbols "
    "with characters other than A-Z and '.', the 21 exploration symbols and SPY",
    "window_sessions": 60,
    "min_bars": 50,
    "min_close": "5",
    "min_median_dollar_volume": "1000000",
    "members": 500,
    "rank": "median daily dollar volume (close x volume), descending; ties by symbol",
    "effective": "from the session after the formation to the next formation session, "
    "inclusive (the last formation: to 'until')",
}
ETF_RULE: dict[str, Any] = {
    "code": "RESEARCH_ETF_CANDIDATES_V1",
    "version": 1,
    "formations": ["2015-12-31"],
    "until": "2019-12-31",
    "candidates": "provider US equity assets on " + "/".join(MAJOR_EXCHANGES) + " that are "
    "registered funds (SEC fund list) or have 'ETF' in their name; excluding the 21 "
    "exploration symbols; first bar on or before 2015-01-02 (trading for a full year)",
    "window_sessions": 60,
    "min_bars": 50,
    "min_close": "5",
    "min_median_dollar_volume": "1000000",
    "members": 150,
    "rank": "median daily dollar volume (close x volume), descending; ties by symbol",
    "effective": "2016-01-04 to 2019-12-31",
}


# REDACTED_FOR_GITHUB
# REDACTED_FOR_GITHUB
ETF_RULE_V2: dict[str, Any] = {
    "code": "RESEARCH_ETF_CANDIDATES_V2",
    "version": 2,
    "formations": ["2016-12-30"],
    "until": "2019-12-31",
    "candidates": ETF_RULE["candidates"].replace(
        "first bar on or before 2015-01-02 (trading for a full year)",
        "a bar in the provider's first week of history (2016-01-04..2016-01-08)",
    ),
    "window_sessions": 60,
    "min_bars": 50,
    "min_close": "5",
    "min_median_dollar_volume": "1000000",
    "members": 150,
    "rank": ETF_RULE["rank"],
    "effective": "2017-01-03 to 2019-12-31",
    "supersedes": "RESEARCH_ETF_CANDIDATES_V1 (no members: no 2015 provider data)",
}
HISTORY_START = date(2016, 1, 8)  # the provider's first week of history

# Corrected versions (D-239): typing from the newest security revision, which detects
# REDACTED_FOR_GITHUB
# fund tickers. The formations, thresholds and sizes are unchanged.
EQUITY_RULE_V2: dict[str, Any] = {
    **EQUITY_RULE,
    "code": "RESEARCH_US_EQUITY_V2",
    "version": 2,
    "formations": ["2016-12-30", "2017-12-29", "2018-12-31"],
    "typing": "newest research_securities revision (ticker-reuse and fund-collision corrections)",
    "supersedes": "RESEARCH_US_EQUITY_V1 (typed from current listings; no 2015 provider data)",
}
ETF_RULE_V3: dict[str, Any] = {
    **ETF_RULE_V2,
    "code": "RESEARCH_ETF_CANDIDATES_V3",
    "version": 3,
    "typing": EQUITY_RULE_V2["typing"],
    "supersedes": "RESEARCH_ETF_CANDIDATES_V2 (stocks with reused tickers typed as ETFs)",
}

# REDACTED_FOR_GITHUB
# (the last close repeated) after a security stops trading, e.g. Team Health's TMH from
# REDACTED_FOR_GITHUB
# They hid reused tickers (TMH, USG typed as today's funds) and delistings. Only traded bars
# (volume > 0) count: in the formation windows, in reuse detection and in delisting
# inference. Formations, thresholds and sizes are unchanged.
TRADED_BARS = "traded sessions only (volume > 0): zero-volume placeholder bars are not trading"
EQUITY_RULE_V3: dict[str, Any] = {
    **EQUITY_RULE_V2,
    "code": "RESEARCH_US_EQUITY_V3",
    "version": 3,
    "bars": TRADED_BARS,
    "traded_bars_only": True,
    "typing": EQUITY_RULE_V2["typing"] + "; reuse detected from traded months only",
    "supersedes": "RESEARCH_US_EQUITY_V2 (placeholder bars hid reused tickers and delistings)",
}
ETF_RULE_V4: dict[str, Any] = {
    **ETF_RULE_V3,
    "code": "RESEARCH_ETF_CANDIDATES_V4",
    "version": 4,
    "bars": TRADED_BARS,
    "traded_bars_only": True,
    "typing": EQUITY_RULE_V3["typing"],
    "supersedes": "RESEARCH_ETF_CANDIDATES_V3 (stocks TMH and USG typed as today's funds)",
}
# The equity universe's third correction, point-in-time identity, is ``identity.py``
# (``RESEARCH_US_EQUITY_V4``, ``RESEARCH_ETF_CANDIDATES_V5``).
CURRENT_UNIVERSES = ("RESEARCH_US_EQUITY_V4", "RESEARCH_ETF_CANDIDATES_V5")


def rule_sha(rule: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()


@dataclass
class Scan:
    """Per-symbol facts gathered from the bars of the scanned windows."""

    first_bar: dict[str, date] = field(default_factory=dict)
    last_bar: dict[str, date] = field(default_factory=dict)
    window: dict[tuple[str, date], list[tuple[float, float]]] = field(
        default_factory=lambda: defaultdict(list)
    )


def classify(asset: dict[str, Any], funds: set[str], companies: set[str]) -> tuple[str, str]:
    sym, name = asset["symbol"].upper(), (asset.get("name") or "").upper()
    if sym in funds:
        return "ETF", "SEC registered-fund ticker list"
    if " ETF" in f" {name}" or " ETN" in f" {name}":
        return "ETF", "provider name contains ETF/ETN"
    if sym in companies:
        return "STOCK", "SEC operating-company ticker list"
    return "UNKNOWN", "not in SEC current lists (typically delisted); treated as non-fund"


def eligible_symbol(sym: str) -> bool:
    """Plain tickers, or a share-class suffix (BRK.B); not units, rights or warrants."""
    base, _, suffix = sym.partition(".")
    if not base.isalpha() or not base.isupper():
        return False
    return suffix == "" or suffix in ("A", "B", "C")


def scan(
    client: Any,
    symbols: list[str],
    windows: list[tuple[date, date]],
    traded_only: bool = False,
    asof_end: bool = False,
    asof_date: date | None = None,
) -> Scan:
    """Bars of each window; ``asof_end`` pins the symbol mapping to the window's end,
    ``asof_date`` to a fixed date (sources.daily_bars)."""
    out = Scan()
    batch = 100
    for start, end in windows:
        asof = end if asof_end else asof_date
        for i in range(0, len(symbols), batch):
            for sym, bar in sources.daily_bars(client, symbols[i : i + batch], start, end, asof):
                if traded_only and not float(bar.get("v") or 0) > 0:
                    continue  # a zero-volume placeholder, not a traded session
                day = date.fromisoformat(bar["t"][:10])
                out.first_bar[sym] = min(out.first_bar.get(sym, day), day)
                out.last_bar[sym] = max(out.last_bar.get(sym, day), day)
                out.window[(sym, end)].append((float(bar["c"]), float(bar["c"]) * float(bar["v"])))
    return out


def build(factory: sessionmaker[Session], progress: Any = print) -> dict[str, Any]:
    """Fetch listings, scan the formation windows, store the security master, both
    universe rules, every candidate with its metrics and the members."""
    retrieved = sources.now_utc()
    with sources.alpaca_client() as client:
        assets = [a for a in sources.alpaca_assets(client) if a.get("exchange") in MAJOR_EXCHANGES]
        sec = sources.Sec()
        companies = set(sec.company_tickers())
        funds = sec.fund_tickers()
        progress(f"listings: {len(assets)} provider assets on major exchanges")
        by_symbol: dict[str, dict[str, Any]] = {}
        for a in sorted(assets, key=lambda x: (x["symbol"], x.get("status") != "active")):
            by_symbol.setdefault(a["symbol"].upper(), a)  # an active listing wins
        symbols = sorted(s for s in by_symbol if eligible_symbol(s))
        formations = [date.fromisoformat(d) for d in EQUITY_RULE["formations"]]
        windows = [(sessions_back(f, EQUITY_RULE["window_sessions"] - 1), f) for f in formations]
        # The first trading days of the scan (ETF history requirement) and of 2020 (to see
        # whether trading continued after the reserved period).
        windows.append((SCAN_START, date(2015, 1, 9)))
        windows.append((date(2019, 12, 16), SCAN_END))
        data = scan(client, symbols, windows)
        progress(f"scanned {len(symbols)} symbols; {len(data.first_bar)} with bars")
    with factory() as session, session.begin():
        for code, rule in ((EQUITY_RULE["code"], EQUITY_RULE), (ETF_RULE["code"], ETF_RULE)):
            if session.scalar(
                select(ResearchUniverseRecord.id).where(ResearchUniverseRecord.code == code)
            ):
                raise RuntimeError(f"{code} already exists: a changed rule needs a new code")
            session.add(
                ResearchUniverseRecord(
                    code=code,
                    version=rule["version"],
                    description=rule["candidates"],
                    rule=rule,
                    rule_sha256=rule_sha(rule),
                    created_by=CREATED_BY,
                )
            )
        securities: dict[str, ResearchSecurityRecord] = {}
        for sym in symbols:
            a = by_symbol[sym]
            kind, basis = classify(a, funds, companies)
            rec = ResearchSecurityRecord(
                provider=PROVIDER,
                symbol=sym,
                revision=0,
                provider_asset_id=a.get("id"),
                name=(a.get("name") or None) and a["name"][:255],
                exchange=a.get("exchange"),
                provider_status=a.get("status", "unknown"),
                security_type=kind,
                type_basis=basis,
                cik=None,
                scan_start=SCAN_START,
                scan_end=SCAN_END,
                first_bar=data.first_bar.get(sym),
                last_bar=data.last_bar.get(sym),
                delisted_on=None,  # set from full bars after the fetch (revision 1)
                delisting_basis=None,
                retrieved_at=retrieved,
                details={"scan": "formation windows only; first/last within them"},
            )
            session.add(rec)
            securities[sym] = rec
        session.flush()
        counts = {}
        for rule in (EQUITY_RULE, ETF_RULE):
            counts[rule["code"]] = _form(session, rule, securities, data)
    return {"assets": len(assets), "symbols": len(symbols), "members": counts}


def _form(
    session: Session,
    rule: dict[str, Any],
    securities: dict[str, ResearchSecurityRecord],
    data: Scan,
) -> dict[str, int]:
    etf = rule["code"].startswith("RESEARCH_ETF_CANDIDATES")
    history_start = HISTORY_START if rule.get("version", 1) >= 2 else SCAN_START
    formations = [date.fromisoformat(d) for d in rule["formations"]]
    until = date.fromisoformat(rule["until"])
    excluded = set(EXPLORATION_SYMBOLS) | (set() if etf else {"SPY"})
    out: dict[str, int] = {}
    for k, formation in enumerate(formations):
        nxt = formations[k + 1] if k + 1 < len(formations) else until
        sessions = sessions_between(formation, nxt)
        effective_from = sessions[1].day if len(sessions) > 1 else nxt
        rows = []
        for sym, sec in securities.items():
            if (sec.security_type == "ETF") != etf or sym in excluded:
                continue
            obs = data.window.get((sym, formation), [])
            if not obs:
                continue
            closes = [c for c, _ in obs]
            dvol = statistics.median([d for _, d in obs])
            reason = None
            if len(obs) < rule["min_bars"]:
                reason = "too_few_bars"
            elif closes[-1] < float(rule["min_close"]):
                reason = "price_below_minimum"
            elif dvol < float(rule["min_median_dollar_volume"]):
                reason = "dollar_volume_below_minimum"
            elif etf and (data.first_bar.get(sym) or date.max) > history_start:
                reason = "history_shorter_than_one_year"
            rows.append((sym, sec, closes[-1], dvol, len(obs), reason))
        ranked = sorted((r for r in rows if r[5] is None), key=lambda r: (-r[3], r[0]))
        rank_of = {r[0]: i + 1 for i, r in enumerate(ranked)}
        members = 0
        for sym, sec, close, dvol, n, reason in rows:
            rank = rank_of.get(sym)
            member = rank is not None and rank <= rule["members"]
            members += member
            session.add(
                ResearchUniverseCandidateRecord(
                    universe_code=rule["code"],
                    formation_session=formation,
                    symbol=sym,
                    security_id=sec.id,
                    asset_id=_asset_for(session, sec) if member else None,
                    last_close=Decimal(str(round(close, 6))),
                    median_dollar_volume=Decimal(str(round(dvol, 2))),
                    sessions_observed=n,
                    eligible=reason is None,
                    ineligible_reason=reason,
                    rank=rank,
                    member=member,
                    effective_from=effective_from if member else None,
                    effective_to=nxt if member else None,
                )
            )
        out[formation.isoformat()] = members
    return out


def add_rule(
    factory: sessionmaker[Session], rule: dict[str, Any], progress: Any = print
) -> dict[str, int]:
    """Form an additional universe rule over the stored security master (revision 0)."""
    formations = [date.fromisoformat(d) for d in rule["formations"]]
    windows = [(sessions_back(f, rule["window_sessions"] - 1), f) for f in formations]
    windows.append((date(2016, 1, 4), HISTORY_START))
    with factory() as session:
        s = ResearchSecurityRecord
        secs: dict[str, ResearchSecurityRecord] = {}
        for r in session.scalars(select(s).order_by(s.symbol, s.revision)):
            secs[r.symbol] = r  # the newest revision wins
        session.expunge_all()
    etf = rule["code"].startswith("RESEARCH_ETF_CANDIDATES")
    symbols = sorted(sym for sym, r in secs.items() if (r.security_type == "ETF") == etf)
    with sources.alpaca_client() as client:
        data = scan(client, symbols, windows, traded_only=bool(rule.get("traded_bars_only")))
    progress(f"scanned {len(symbols)} symbols for {rule['code']}")
    with factory() as session, session.begin():
        if session.scalar(
            select(ResearchUniverseRecord.id).where(ResearchUniverseRecord.code == rule["code"])
        ):
            raise RuntimeError(f"{rule['code']} already exists")
        session.add(
            ResearchUniverseRecord(
                code=rule["code"],
                version=rule["version"],
                description=rule["candidates"],
                rule=rule,
                rule_sha256=rule_sha(rule),
                created_by=CREATED_BY,
            )
        )
        attached = {sym: session.merge(r, load=False) for sym, r in secs.items() if sym in symbols}
        return _form(session, rule, attached, data)


_ASSETS: dict[str, uuid.UUID] = {}
REUSED_SUFFIX = "(formation-era security; ticker since reused)"


def _asset_for(session: Session, sec: ResearchSecurityRecord) -> uuid.UUID:
    """The ``assets`` row of a member (an existing one of the same symbol and class is
    reused; otherwise one is created, labelled ``research-security-master``). The link is
    recorded as the security's next revision."""
    reused = sec.type_basis.startswith("ticker reuse")
    found = _ASSETS.get(sec.symbol)
    if found is None and sec.asset_id is not None:  # linked by an earlier formation
        linked = session.get(Asset, sec.asset_id)
        # A reused ticker's link counts only if it is its own formation-era row (a revision
        # may carry the current holder's row from before the reuse was detected).
        if not reused or (linked is not None and (linked.name or "").endswith(REUSED_SUFFIX)):
            found = sec.asset_id
    if found is not None:
        _ASSETS[sec.symbol] = found
        return found
    asset_class = "ETF" if sec.security_type == "ETF" else "EQUITY"
    if reused:
        # Its own row: the current holder of the ticker is a different security.
        own = Asset(
            id=uuid.uuid4(),
            symbol=sec.symbol,
            asset_class="EQUITY",
            name=f"{sec.symbol} {REUSED_SUFFIX}",
            exchange_code=None,
            quote_currency="USD",
            status="INACTIVE",
            instrument_type="UNCLASSIFIED",
            metadata_source="research-security-master",
        )
        session.add(own)
        session.flush()
        session.add(_revise(sec, asset_id=own.id))
        session.flush()
        _ASSETS[sec.symbol] = own.id
        return own.id
    # Asset discovery registered the provider's listings already (mostly as EQUITY with
    # exchange MIC codes): reuse the symbol's row, an active one first.
    existing = session.scalars(
        select(Asset)
        .where(Asset.symbol == sec.symbol, Asset.asset_class.in_(("EQUITY", "ETF")))
        .order_by(Asset.status != "ACTIVE", Asset.created_at)
    ).first()
    if existing is None:
        existing = Asset(
            id=uuid.uuid4(),
            symbol=sec.symbol,
            asset_class=asset_class,
            name=sec.name,
            exchange_code=None,  # provider exchange names are not MIC codes
            quote_currency="USD",
            status="ACTIVE" if sec.provider_status == "active" else "INACTIVE",
            instrument_type={"ETF": "ETF", "STOCK": "STOCK"}.get(sec.security_type, "UNCLASSIFIED"),
            metadata_source="research-security-master",
        )
        session.add(existing)
        session.flush()
    session.add(_revise(sec, asset_id=existing.id))
    session.flush()
    _ASSETS[sec.symbol] = existing.id
    return existing.id


def _newest(session: Session, symbol: str) -> ResearchSecurityRecord | None:
    s = ResearchSecurityRecord
    return session.scalars(
        select(s).where(s.symbol == symbol).order_by(s.revision.desc()).limit(1)
    ).first()


def _revise(sec: ResearchSecurityRecord, **changes: Any) -> ResearchSecurityRecord:
    fields = {
        k: getattr(sec, k)
        for k in (
            "provider",
            "symbol",
            "provider_asset_id",
            "name",
            "exchange",
            "provider_status",
            "security_type",
            "type_basis",
            "cik",
            "scan_start",
            "scan_end",
            "first_bar",
            "last_bar",
            "delisted_on",
            "delisting_basis",
            "asset_id",
            "retrieved_at",
            "details",
        )
    }
    fields.update(changes)
    return ResearchSecurityRecord(revision=sec.revision + 1, **fields)


__all__ = [
    "CURRENT_UNIVERSES",
    "EQUITY_RULE",
    "EQUITY_RULE_V2",
    "EQUITY_RULE_V3",
    "ETF_RULE",
    "ETF_RULE_V2",
    "ETF_RULE_V3",
    "ETF_RULE_V4",
    "EXPLORATION_SYMBOLS",
    "add_rule",
    "build",
    "rule_sha",
]
