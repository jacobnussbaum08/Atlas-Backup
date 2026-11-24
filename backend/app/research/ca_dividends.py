"""X3, first scope: historical dividend identity corrections (D-243). Simulation database only.

The audit (D-241, CA-14/CA-15) found renamed companies whose dividends the provider files
under former tickers. The replay credits dividends only under the asset's own symbol, so
those holders never receive them, while a reused ticker can credit another security's
dividend. This module builds a correction set of append-only records
(``corporate_action_events``) and registers it (``corporate_action_sets``); the replay
applies it only when a run names it (:mod:`app.backtesting.corrections`).

**Identity is never inferred from ticker similarity.**

* Candidate links (asset <- provider ticker) are an explicit list, :data:`LINKS`. Each
  entry names the audit evidence that proposed it.
* A candidate is only a hypothesis. Every single dividend is verified on its own dates:
  * the provider's close for the ticker, *as of the session before the ex-date*, must equal
    the asset's stored RAW close on that session and on the session before it
    (:data:`TOLERANCE`);
  * the ``asof`` request pins the provider's mapping to that date, so the ticker means the
    security that held it then;
  * a dividend matches on both sessions (``VERIFIED``), on neither (``REJECTED``: another
    security), or the data cannot decide (``UNRESOLVED``).
* Dividends already filed under the asset's own ticker are checked the same way. One whose
  ticker meant another security on its dates becomes a ``CASH_DIVIDEND_VOID``.
* A verified dividend the asset already receives (same provider action, or same ex-date and
  rate) is ``REJECTED`` as a duplicate. Nothing is counted twice.

Provider responses (dividend rows, closes) are saved in a look-ups artifact, never replaced,
so the evidence regenerates offline. No provider data, exclusion (X1, X2), reservation or
ETF V5 row changes. X2's windows are untouched: a correction never releases one.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery.sessions import previous_trading_day
from app.backtesting import corrections
from app.backtesting.data import CALENDAR
from app.backtesting.dividends import parse
from app.database.models import (
    Asset,
    CashDividendRecord,
    CashDividendRetrievalRecord,
    CorporateActionEventRecord,
    CorporateActionSetRecord,
    MarketBarRecord,
    ResearchPreregistrationRecord,
)
from app.research.bars import END_TS, START_TS, identity_of
from app.research.exclusions import sha256_file

SET_CODE = "CA_DIVIDEND_IDENTITY_V1"
PREREGISTRATION = "X3_DIVIDEND_IDENTITY_CORRECTIONS_V1"
SCOPE = "DIVIDEND_IDENTITY"
CREATED_BY = "operator:research-foundation"
PROVIDER = "alpaca"
FIRST_EX, LAST_EX = date(2016, 1, 5), date(2019, 12, 31)  # cum session needs a stored bar
TOLERANCE = Decimal("0.002")
# The provider's corporate-actions range filters on the processing date, not the ex-date: a
# late-December ex-date paid in January is missed by a request ending on 2019-12-31. Fetch
# past the window and keep ex-dates inside it.
FETCH_START, FETCH_END = FIRST_EX - timedelta(days=4), LAST_EX + timedelta(days=60)
AUDIT = "ca_audit_6.json"
PROBES = "ca_identity_probes_1.json"


@dataclass(frozen=True)
class Link:
    """A candidate identity link: ``ticker``'s dividends may belong to the asset. ``source``
    names the audit evidence that proposed it; only per-dividend verification decides."""

    label: str
    asset_id: str
    ticker: str
    source: str


_FORMER = "ca_audit_6.dividend_identity.former_or_other_tickers (four equal closes)"
LINKS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def _full_ids(session: Session) -> dict[str, uuid.UUID]:
    """Each link's asset: the audit's asset id, which must exist with the link's label as
    its symbol (one asset per label)."""
    out: dict[str, uuid.UUID] = {}
    for link in LINKS:
        asset = session.get(Asset, uuid.UUID(link.asset_id))
        if asset is None or asset.symbol != link.label:
            raise ValueError(f"{link.label}: asset {link.asset_id} is not that label")
        if out.setdefault(link.label, asset.id) != asset.id:
            raise ValueError(f"{link.label}: two assets")
    return out


def _raw_closes(session: Session, asset_id: uuid.UUID) -> dict[date, Decimal]:
    b = MarketBarRecord
    out: dict[date, tuple[int, int, Decimal]] = {}
    for at, close, revision, row_id in session.execute(
        select(b.event_time, b.close, b.revision, b.id).where(
            b.asset_id == asset_id,
            b.feed == "sip",
            b.timeframe == "1Day",
            b.adjustment == "RAW",
            b.event_time >= START_TS,
            b.event_time <= END_TS,
        )
    ):
        day = at.astimezone(UTC).date()
        key = (revision, row_id)
        if day not in out or key > out[day][:2]:
            out[day] = (revision, row_id, Decimal(close))
    return {d: v[2] for d, v in out.items()}


def _own_dividends(session: Session, symbol: str) -> tuple[list[dict[str, Any]], bool]:
    """The provider dividends stored under ``symbol`` (newest revision per action), and
    whether a retrieval covers 2016-2019 (what the replay's loader would credit)."""
    d = CashDividendRecord
    newest: dict[str, CashDividendRecord] = {}
    for row in session.scalars(
        select(d)
        .where(d.provider == PROVIDER, d.symbol == symbol, d.ex_date >= FIRST_EX)
        .where(d.ex_date <= LAST_EX)
        .order_by(d.provider_action_id, d.revision)
    ):
        newest[row.provider_action_id] = row
    r = CashDividendRetrievalRecord
    covered = (
        session.scalar(
            select(r.id)
            .where(r.provider == PROVIDER, r.symbol == symbol, r.status == "OK")
            .where(r.range_start <= FIRST_EX, r.range_end >= LAST_EX)
            .limit(1)
        )
        is not None
    )
    rows = [
        {
            "provider_action_id": x.provider_action_id,
            "revision": x.revision,
            "symbol": x.symbol,
            "ex_date": x.ex_date.isoformat(),
            "rate": str(Decimal(x.rate)),
            "special": x.special,
            "foreign": x.foreign,
            "payable_date": x.payable_date.isoformat() if x.payable_date else None,
            "cash_dividends_id": x.id,
        }
        for x in sorted(newest.values(), key=lambda x: (x.ex_date, x.provider_action_id))
    ]
    return rows, covered


class Lookups:
    """Provider responses for the evidence (GET only), saved for offline regeneration."""

    def __init__(self, data: dict[str, Any] | None, *, offline: bool) -> None:
        self.data = data or {
            "dividend_request": {"start": FETCH_START.isoformat(), "end": FETCH_END.isoformat()},
            "dividends": {},
            "closes": {},
        }
        self.offline = offline
        self._client: Any = None
        self.requests = 0

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            from app.data_engine.providers.alpaca.adapter import (
                DATA_API_BASE_URL,
                AlpacaMarketDataProvider,
            )
            from app.data_engine.providers.alpaca.config import get_alpaca_settings

            headers = AlpacaMarketDataProvider(get_alpaca_settings()).authentication_headers()
            self._client = httpx.Client(base_url=DATA_API_BASE_URL, headers=headers, timeout=120)
        return self._client

    def dividends(self, tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
        missing = [t for t in tickers if t not in self.data["dividends"]]
        if missing and self.offline:
            raise LookupError(f"no saved dividends for {missing}")
        from app.backtesting.dividends import fetch_rows

        for i in range(0, len(missing), 50):
            part = missing[i : i + 50]
            rows = fetch_rows(self._http(), part, FETCH_START, FETCH_END)
            self.requests += 1
            for t in part:
                self.data["dividends"][t] = sorted(
                    (r for r in rows if str(r.get("symbol", "")).upper() == t),
                    key=lambda r: (r.get("ex_date", ""), str(r.get("id", ""))),
                )
        return {t: self.data["dividends"][t] for t in tickers}

    def closes(self, tickers: list[str], cum: date) -> dict[str, dict[str, str]]:
        """ticker -> {session: close} for the sessions up to ``cum``, as of ``cum``."""
        key = cum.isoformat()
        have = self.data["closes"].setdefault(key, {})
        missing = sorted(t for t in tickers if t not in have)
        if missing and self.offline:
            raise LookupError(f"no saved closes for {missing} as of {key}")
        if missing:
            from app.research import sources

            for i in range(0, len(missing), 100):
                part = missing[i : i + 100]
                got: dict[str, dict[str, str]] = {t: {} for t in part}
                for sym, bar in sources.daily_bars(
                    self._http(), part, cum - timedelta(days=10), cum, asof=cum
                ):
                    got.setdefault(sym, {})[bar["t"][:10]] = str(bar["c"])
                self.requests += 1
                have.update(got)
        return {t: have.get(t, {}) for t in tickers}

    def shut(self) -> None:
        if self._client is not None:
            self._client.close()


def _entity(record: Any) -> str | None:
    return None if record is None else (record.details or {}).get("entity_sha256")


def _match(a: Decimal | None, b: str | None) -> bool | None:
    if a is None or b is None:
        return None
    return abs(Decimal(b) / a - 1) <= TOLERANCE


def verify(
    asset_closes: dict[date, Decimal], ticker_closes: dict[str, str], ex: date
) -> dict[str, Any]:
    """Identity on the session before ``ex`` (cum) and the one before it."""
    cum = previous_trading_day(CALENDAR, ex).day
    pre = previous_trading_day(CALENDAR, cum).day
    sessions = []
    for day in (cum, pre):
        a, t = asset_closes.get(day), ticker_closes.get(day.isoformat())
        sessions.append(
            {
                "session": day.isoformat(),
                "asset_raw_close": None if a is None else str(a),
                "ticker_close_asof_cum": t,
                "match": _match(a, t),
            }
        )
    results = [s["match"] for s in sessions]
    if results == [True, True]:
        identity = "SAME_SECURITY"
    elif results == [False, False]:
        identity = "OTHER_SECURITY"
    else:
        identity = "UNDECIDED"
    return {"identity": identity, "tolerance": str(TOLERANCE), "sessions": sessions}


def _row(r: dict[str, Any]) -> dict[str, Any]:
    p = parse([r])
    if not p:
        raise ValueError(f"malformed provider dividend {r}")
    x = p[0]
    return {
        "provider_action_id": x["provider_action_id"],
        "symbol": x["symbol"],
        "ex_date": x["ex_date"].isoformat(),
        "rate": str(x["rate"]),
        "special": x["special"],
        "foreign": x["foreign"],
        "payable_date": x["payable_date"].isoformat() if x["payable_date"] else None,
    }


def build(
    factory: sessionmaker[Session],
    out: Path,
    lookups_path: Path,
    evidence_path: Path,
    seed: Path | None = None,
) -> dict[str, Any]:
    """Classify every candidate dividend and write the evidence (never replaced). Reads
    ``lookups_path`` if it exists (offline), otherwise writes it from GET requests; a new
    one may reuse the close responses of an earlier look-ups file (``seed``, the same
    requests), recorded with its hash."""
    if evidence_path.exists():
        raise FileExistsError(evidence_path)
    offline = lookups_path.exists()
    cache = Lookups(json.loads(lookups_path.read_text()) if offline else None, offline=offline)
    seeded = None
    if not offline and seed is not None:
        cache.data["closes"] = json.loads(seed.read_text())["closes"]
        cache.data["closes_seeded_from"] = {"path": seed.as_posix(), "sha256": sha256_file(seed)}
    seeded = cache.data.get("closes_seeded_from")
    audit = json.loads((out / AUDIT).read_text())
    try:
        with factory() as session:
            ids = _full_ids(session)
            labels = sorted(ids)
            assets = {label: session.get(Asset, ids[label]) for label in labels}
            closes = {label: _raw_closes(session, ids[label]) for label in labels}
            ident = {label: identity_of(session, ids[label]) for label in labels}
            own = {label: _own_dividends(session, assets[label].symbol) for label in labels}  # type: ignore[union-attr]
        tickers = sorted({link.ticker for link in LINKS})
        provider = cache.dividends(tickers)
        candidates: list[dict[str, Any]] = []
        for link in LINKS:
            for r in provider[link.ticker]:
                row = _row(r)
                if FIRST_EX <= date.fromisoformat(row["ex_date"]) <= LAST_EX:
                    candidates.append({"kind": "LINK", "link": link, "row": row, "raw": r})
        for label in labels:
            symbol = assets[label].symbol  # type: ignore[union-attr]
            if "~" in symbol:
                continue  # not a provider ticker: the loader credits nothing under it
            for row in own[label][0]:
                candidates.append(
                    {"kind": "OWN", "label": label, "ticker": symbol, "row": row, "raw": None}
                )
        need: dict[date, set[str]] = defaultdict(set)
        for c in candidates:
            ticker = c["link"].ticker if c["kind"] == "LINK" else c["ticker"]
            ex = date.fromisoformat(c["row"]["ex_date"])
            need[previous_trading_day(CALENDAR, ex).day].add(ticker)
        for cum in sorted(need):
            cache.closes(sorted(need[cum]), cum)
    finally:
        if not offline:
            lookups_path.write_text(json.dumps(cache.data, indent=1, sort_keys=True))
        cache.shut()
    lookups_sha = sha256_file(lookups_path)

    records: list[dict[str, Any]] = []
    for c in candidates:
        row = c["row"]
        ex = date.fromisoformat(row["ex_date"])
        cum = previous_trading_day(CALENDAR, ex).day
        if c["kind"] == "LINK":
            label, ticker = c["link"].label, c["link"].ticker
        else:
            label, ticker = c["label"], c["ticker"]
        check = verify(closes[label], cache.data["closes"][cum.isoformat()].get(ticker, {}), ex)
        records.append(
            {
                "kind": c["kind"],
                "label": label,
                "asset_id": str(ids[label]),
                "ticker": ticker,
                "source": c["link"].source if c["kind"] == "LINK" else "own symbol (stored)",
                "dividend": row,
                "provider_raw": c["raw"],
                "verification": check,
            }
        )
    # Decisions: credits, duplicates, voids (deterministic order).
    records.sort(key=lambda r: (r["label"], r["dividend"]["ex_date"], r["kind"], r["ticker"]))
    own_terms = {(label, d["provider_action_id"]): d for label in labels for d in own[label][0]}
    own_by_terms = {
        (label, d["ex_date"], Decimal(d["rate"])) for label in labels for d in own[label][0]
    }
    voided_terms: set[tuple[str, str, Decimal]] = set()
    for r in records:
        if r["kind"] == "OWN":
            ident_ = r["verification"]["identity"]
            r["decision"] = {"SAME_SECURITY": "KEEP", "OTHER_SECURITY": "VOID"}.get(
                ident_, "UNRESOLVED"
            )
            if r["decision"] == "VOID":
                d = r["dividend"]
                voided_terms.add((r["label"], d["ex_date"], Decimal(d["rate"])))
    credited: set[tuple[str, str, Decimal]] = set()
    for r in records:
        if r["kind"] != "LINK":
            continue
        d = r["dividend"]
        terms = (r["label"], d["ex_date"], Decimal(d["rate"]))
        identity = r["verification"]["identity"]
        if identity == "OTHER_SECURITY":
            r["decision"], r["reason"] = "REJECTED", "the ticker meant another security"
        elif identity == "UNDECIDED":
            r["decision"], r["reason"] = "UNRESOLVED", "identity not decided on both sessions"
        elif (r["label"], d["provider_action_id"]) in own_terms or (
            terms in own_by_terms and terms not in voided_terms
        ):
            r["decision"], r["reason"] = "REJECTED", "duplicate of a dividend the asset has"
        elif terms in credited:
            r["decision"], r["reason"] = "REJECTED", "duplicate of another credited record"
        else:
            r["decision"], r["reason"] = "CREDIT", "verified on both sessions"
            credited.add(terms)

    # The audit's 333 (CA-14) against these decisions.
    by_terms: dict[tuple[str, str, str, Decimal], str] = {}
    for r in records:
        if r["kind"] == "LINK":
            d = r["dividend"]
            by_terms[(r["label"], r["ticker"], d["ex_date"], Decimal(d["rate"]))] = r["decision"]
    audit_rows = []
    for e in audit["dividend_identity"]:
        for m in e["missed"]:
            key = (e["asset_symbol"], m["ticker"], m["ex_date"], Decimal(str(m["rate"])))
            audit_rows.append(
                {
                    "label": e["asset_symbol"],
                    "ticker": m["ticker"],
                    "ex_date": m["ex_date"],
                    "rate": str(m["rate"]),
                    "decision": by_terms.get(key, "NOT_A_CANDIDATE"),
                }
            )
    in_audit = {(a["label"], a["ticker"], a["ex_date"]) for a in audit_rows}
    summary = {
        "candidates": len(records),
        "decisions": dict(sorted(Counter(f"{r['kind']}:{r['decision']}" for r in records).items())),
        "audit_missed": len(audit_rows),
        "audit_missed_decisions": dict(sorted(Counter(a["decision"] for a in audit_rows).items())),
        "credits_beyond_audit_list": sum(
            1
            for r in records
            if r["decision"] == "CREDIT"
            and (r["label"], r["ticker"], r["dividend"]["ex_date"]) not in in_audit
        ),
    }
    doc = {
        "code": SET_CODE,
        "preregistration": PREREGISTRATION,
        "scope": SCOPE,
        "generated_at": datetime.now(UTC).isoformat(),
        "window": {"first_ex": FIRST_EX.isoformat(), "last_ex": LAST_EX.isoformat()},
        "tolerance": str(TOLERANCE),
        "lookups": {
            "path": lookups_path.as_posix(),
            "sha256": lookups_sha,
            "dividend_request": cache.data.get("dividend_request"),
            "closes_seeded_from": seeded,
        },
        "audit_artifacts": {
            name: {"path": (out / name).as_posix(), "sha256": sha256_file(out / name)}
            for name in (AUDIT, PROBES)
        },
        "links": [
            {
                "label": k.label,
                "asset_id": str(ids[k.label]),
                "ticker": k.ticker,
                "source": k.source,
            }
            for k in LINKS
        ],
        "assets": {
            label: {
                "asset_id": str(ids[label]),
                "symbol": assets[label].symbol,  # type: ignore[union-attr]
                "identity_sha256": _entity(ident[label]),
                "own_symbol_retrieval_covers_2016_2019": own[label][1],
                "own_symbol_dividends": len(own[label][0]),
            }
            for label in labels
        },
        "records": records,
        "audit_missed": audit_rows,
        "summary": summary,
    }
    evidence_path.write_text(json.dumps(doc, indent=1, default=str))
    return {
        "evidence": str(evidence_path),
        "sha256": sha256_file(evidence_path),
        "lookups_sha256": lookups_sha,
        "provider_requests": cache.requests,
        **summary,
    }


def _events(ev: dict[str, Any], ev_sha: str) -> list[dict[str, Any]]:
    """The records to register, as column values (deterministic)."""
    assets = ev["assets"]
    out: list[dict[str, Any]] = []
    succession: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in ev["records"]:
        if r["kind"] == "LINK" and r["decision"] == "CREDIT":
            succession[(r["label"], r["ticker"])].append(
                r["verification"]["sessions"][0]["session"]
            )
    for (label, ticker), days in sorted(succession.items()):
        out.append(
            {
                "code": f"TS-{label}-{ticker}",
                "asset_label": label,
                "event_type": corrections.SUCCESSION,
                "provider_ticker": ticker,
                "valid_from": date.fromisoformat(min(days)),
                "valid_to": date.fromisoformat(max(days)),
                "known_on": date.fromisoformat(max(days)),
                "verification_status": corrections.VERIFIED,
                "verification": {"verified_cum_sessions": sorted(days)},
                "evidence": {
                    "sources": sorted(
                        {
                            lk["source"]
                            for lk in ev["links"]
                            if (lk["label"], lk["ticker"]) == (label, ticker)
                        }
                    )
                },
            }
        )
    status = {"CREDIT": "VERIFIED", "UNRESOLVED": "UNRESOLVED", "REJECTED": "REJECTED"}
    for r in ev["records"]:
        d = r["dividend"]
        if r["kind"] == "LINK":
            event_type, state = corrections.CREDIT, status[r["decision"]]
            prefix = "DIV"
        elif r["decision"] == "VOID":
            event_type, state, prefix = corrections.VOID, corrections.VERIFIED, "VOID"
        elif r["decision"] == "UNRESOLVED":
            event_type, state, prefix = corrections.VOID, "UNRESOLVED", "VOID"
        else:
            continue  # an own dividend verified as the asset's: nothing to correct
        link = f"TS-{r['label']}-{r['ticker']}" if r["kind"] == "LINK" else None
        out.append(
            {
                "code": (
                    f"{prefix}-{r['label']}-{r['ticker']}-{d['ex_date']}-{d['provider_action_id']}"
                ),
                "asset_label": r["label"],
                "event_type": event_type,
                "provider_ticker": r["ticker"],
                "provider_action_id": d["provider_action_id"],
                "ex_date": date.fromisoformat(d["ex_date"]),
                "payable_date": date.fromisoformat(d["payable_date"])
                if d["payable_date"]
                else None,
                "cash_amount": Decimal(d["rate"]),
                "special": d["special"],
                "foreign": d["foreign"],
                "link_code": link if state == corrections.VERIFIED else None,
                "known_on": date.fromisoformat(d["ex_date"]),
                "verification_status": state,
                "verification": {
                    **r["verification"],
                    "decision": r["decision"],
                    "reason": r.get("reason"),
                },
                "evidence": {
                    "source": r["source"],
                    "provider_raw": r["provider_raw"],
                    "dividend": d,
                },
            }
        )
    for e in out:
        e["asset_id"] = uuid.UUID(assets[e["asset_label"]]["asset_id"])
        e["identity_sha256"] = assets[e["asset_label"]]["identity_sha256"]
        e["provider"] = PROVIDER
        e["evidence_sha256"] = ev_sha
    return out


def register(
    factory: sessionmaker[Session],
    document: Path,
    evidence_path: Path,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Register the records and the set once, in one transaction. Refuses if the set exists,
    if the document does not cite the evidence, or if the stored data the evidence relied on
    changed (asset RAW closes, own dividends, identities)."""
    doc_sha, ev_sha = sha256_file(document), sha256_file(evidence_path)
    if ev_sha not in document.read_text(encoding="utf-8"):
        raise ValueError("the pre-registration document does not cite the evidence's sha256")
    ev = json.loads(evidence_path.read_text())
    if ev["code"] != SET_CODE:
        raise ValueError("the evidence is not this set's")
    events = _events(ev, ev_sha)
    with factory() as session, session.begin():
        s = CorporateActionSetRecord
        if session.scalar(select(s.id).where(s.code == SET_CODE)) is not None:
            raise ValueError(f"{SET_CODE} is already registered (append-only)")
        p = ResearchPreregistrationRecord
        if session.scalar(select(p.id).where(p.code == PREREGISTRATION)) is not None:
            raise ValueError(f"{PREREGISTRATION} is already registered (append-only)")
        for label, a in ev["assets"].items():
            asset_id = uuid.UUID(a["asset_id"])
            now = _entity(identity_of(session, asset_id))
            if now != a["identity_sha256"]:
                raise ValueError(f"{label}: identity changed since the evidence")
            closes = _raw_closes(session, asset_id)
            for r in ev["records"]:
                if r["label"] != label:
                    continue
                for v in r["verification"]["sessions"]:
                    got = closes.get(date.fromisoformat(v["session"]))
                    if (None if got is None else str(got)) != v["asset_raw_close"]:
                        raise ValueError(f"{label} {v['session']}: RAW close changed")
        rows = [CorporateActionEventRecord(**e, created_by=CREATED_BY) for e in events]
        session.add_all(rows)
        session.flush()
        members = [r for r in rows if r.verification_status == corrections.VERIFIED]
        sha = corrections.content_sha256([corrections.event_content(r) for r in members])
        session.add(
            CorporateActionSetRecord(
                code=SET_CODE,
                version=1,
                scope=SCOPE,
                description="Historical dividend identity corrections, first scope (D-243)",
                rule={
                    "members": "every VERIFIED record of the evidence",
                    "credit": "provider dividend under a candidate ticker whose close as of the "
                    "cum session equals the asset's RAW close on the cum and previous sessions",
                    "void": "provider dividend under the asset's own ticker that fails both",
                    "tolerance": ev["tolerance"],
                    "window": ev["window"],
                },
                members=[{"code": r.code, "revision": r.revision} for r in members],
                content_sha256=sha,
                preregistration_code=PREREGISTRATION,
                document_sha256=doc_sha,
                evidence_sha256=ev_sha,
                created_by=CREATED_BY,
            )
        )
        session.add(
            ResearchPreregistrationRecord(
                code=PREREGISTRATION,
                document_path=document.as_posix(),
                document_sha256=doc_sha,
                reservation_codes=[],
                created_by=CREATED_BY,
            )
        )
        counts = Counter(f"{r.event_type}:{r.verification_status}" for r in rows)
        progress(json.dumps(dict(sorted(counts.items()))))
    return {
        "set": SET_CODE,
        "content_sha256": sha,
        "document_sha256": doc_sha,
        "members": len(members),
        "records": dict(sorted(counts.items())),
    }


__all__ = ["LINKS", "PREREGISTRATION", "SET_CODE", "Link", "build", "register", "verify"]
