"""Phantom-split exclusions X1 (D-240). Simulation database only; computes no strategy result.

The provider encodes some spin-offs, rights issues and ticker transfers as splits. The replay
infers a split from them, rescales held quantities and rescales split-adjusted history (D-239
check ``split_consistency``). X1 removes the affected assets from research use:

REDACTED_FOR_GITHUB
  REDACTED_FOR_GITHUB
REDACTED_FOR_GITHUB
  REDACTED_FOR_GITHUB

REDACTED_FOR_GITHUB
REDACTED_FOR_GITHUB

* :func:`evidence` writes the evidence artifact (never replaced) from the database and the
  provider's corporate-action records (GET only).
* :func:`register` records the pre-registration document's hash and one append-only
  ``research_exclusions`` row per asset, once; it refuses if X1 already exists, if a file
  changed since it was hashed, or if an event is no longer found in the data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.data import sessions_between
from app.database.models import (
    Asset,
    MarketBarRecord,
    ResearchDataReservationRecord,
    ResearchExclusionRecord,
    ResearchPreregistrationRecord,
    ResearchUniverseCandidateRecord,
)
from app.research import sources, store
from app.research.bars import END_TS, START_TS, identity_of
from app.research.universe import CURRENT_UNIVERSES
from app.research.validation import dataset_sha256, inferred_splits

CODE = "X1_PHANTOM_SPLITS"
PREREGISTRATION = "X1_PHANTOM_SPLIT_EXCLUSIONS_V1"
UNIVERSE = "RESEARCH_US_EQUITY_V4"
CREATED_BY = "operator:research-foundation"
IN_SPAN, LOOKBACK = "event inside a membership span", "event before a later membership span"

# (label of the asset's first V4 span, event session, basis). The asset is the one whose
# inferred splits contain a phantom on that session: resolved from the data, never typed.
EXCLUDED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
NOT_EXCLUDED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
# Public-record context for each event. The author's note, NOT provider data: the provider's
# own records are in the evidence (``provider_corporate_actions``).
CONTEXT = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _series_sha(session: Session, asset_id: uuid.UUID, adjustment: str) -> str:
    b = MarketBarRecord
    lines = [
        f"{at.isoformat()}|{close}|{volume}"
        for at, close, volume in session.execute(
            select(b.event_time, b.close, b.volume)
            .where(
                b.asset_id == asset_id,
                b.feed == "sip",
                b.timeframe == "1Day",
                b.adjustment == adjustment,
                b.event_time >= START_TS,
                b.event_time <= END_TS,
            )
            .order_by(b.event_time, b.id)
        )
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _spans(session: Session, asset_id: uuid.UUID, event: date) -> list[dict[str, Any]]:
    c = ResearchUniverseCandidateRecord
    out = []
    for r in session.execute(
        select(
            c.universe_code, c.symbol, c.formation_session, c.rank, c.effective_from, c.effective_to
        )
        .where(c.asset_id == asset_id, c.member.is_(True), c.universe_code.in_(CURRENT_UNIVERSES))
        .order_by(c.effective_from)
    ):
        f, t = r.effective_from, r.effective_to
        if event < f:
            relation, n = "event before span", len(sessions_between(event, f)) - 1
        elif event > t:
            relation, n = "event after span", len(sessions_between(t, event)) - 1
        else:
            relation, n = "event inside span", len(sessions_between(f, event)) - 1
        out.append(
            {
                "universe": r.universe_code,
                "label": r.symbol,
                "formation": r.formation_session.isoformat(),
                "rank": r.rank,
                "effective_from": f.isoformat(),
                "effective_to": t.isoformat(),
                "relation": relation,
                "sessions_apart": n,
            }
        )
    return out


def resolve(session: Session, label: str, event: date) -> tuple[uuid.UUID, dict[str, Any]]:
    """The one current-universe asset labelled ``label`` with a phantom split on ``event``."""
    c = ResearchUniverseCandidateRecord
    ids = set(
        session.scalars(
            select(c.asset_id).where(
                c.member.is_(True),
                c.universe_code.in_(CURRENT_UNIVERSES),
                c.symbol.in_([label, f"{label}~CUR"]),
                c.asset_id.is_not(None),
            )
        )
    )
    found = [
        (a, e)
        for a in sorted(ids, key=str)
        for e in inferred_splits(session, a)
        if e["phantom"] and e["session"] == event.isoformat()
    ]
    if len(found) != 1:
        raise ValueError(f"{label} {event}: expected one phantom split, found {len(found)}")
    a, e = found[0]
    assert a is not None  # noqa: S101  # filtered
    return a, e


def _provider_actions(client: Any, symbol: str, event: date) -> dict[str, Any]:
    from app.data_engine.providers.alpaca.adapter import DATA_API_BASE_URL

    params = {
        "symbols": symbol,
        "start": (event - timedelta(days=7)).isoformat(),
        "end": (event + timedelta(days=7)).isoformat(),
        "limit": "1000",
    }
    r = client.get(f"{DATA_API_BASE_URL}/v1/corporate-actions", params=params)
    if r.status_code != 200:
        return {"request": params, "http_status": r.status_code}
    keep = ("ex_date", "old_rate", "new_rate", "rate", "new_symbol", "source_symbol", "symbol")
    records = [
        {"type": typ, **{k: a[k] for k in keep if a.get(k) is not None}}
        for typ, rows in sorted((r.json().get("corporate_actions") or {}).items())
        for a in rows
    ]
    return {"request": params, "records": records}


def _entry(session: Session, client: Any, label: str, event: date, basis: str) -> dict[str, Any]:
    asset_id, e = resolve(session, label, event)
    asset = session.get(Asset, asset_id)
    ident = identity_of(session, asset_id)
    details = (ident.details or {}) if ident else {}
    request_symbol = details.get("request_symbol", label)
    return {
        "label": label,
        "asset_id": str(asset_id),
        "asset_name": asset.name if asset else None,
        "basis": basis,
        "event_session": event.isoformat(),
        "inferred_split": {k: v for k, v in e.items() if k != "phantom"},
        "spans": _spans(session, asset_id, event),
        "identity": None
        if ident is None
        else {
            "provider": ident.provider,
            "symbol": ident.symbol,
            "revision": ident.revision,
            "asof": details.get("asof"),
            "request_symbol": request_symbol,
            "same_as_current_holder": details.get("same_as_current_holder"),
            "delisted_on": ident.delisted_on.isoformat() if ident.delisted_on else None,
        },
        "bars_sha256": {
            "RAW": _series_sha(session, asset_id, "RAW"),
            "SPLIT": _series_sha(session, asset_id, "SPLIT"),
        },
        "provider_corporate_actions": _provider_actions(client, request_symbol, event)
        if client is not None
        else None,
        "context_not_provider_data": CONTEXT.get(label),
    }


def lookback_bounds() -> dict[str, Any]:
    """Sessions of history the replay reads per registered strategy at its default
    parameters (``BacktestService.load``), and with the coordinator. Parameters and weekly
    timeframes can raise them: the registry allows SMA lengths up to 400 periods."""
    from app.backtesting.pipeline import default_intelligence_config, sessions_needed
    from app.backtesting.service import LIQUIDITY_LOOKBACK
    from app.strategies.periods import sessions_for
    from app.strategies.registry import default_registry

    reg = default_registry()
    per: dict[str, int] = {}
    for sid in sorted(s.strategy_id for s in reg):
        strategy = reg.get(sid)
        params = strategy.default_parameters()
        for tf in params.timeframes:
            resolved = reg.features.resolve_all(strategy.required_features(params, tf))
            window = max((f.window for f in resolved if f.window), default=1)
            per[f"{sid}/{tf}"] = sessions_for(tf, window)
    coordinator = sessions_needed(default_intelligence_config())
    return {
        "feature_sessions_by_strategy_default": per,
        "coordinator_sessions": coordinator,
        "max_feature_sessions_default": max(per.values()),
        "max_read_sessions_default_with_coordinator": max(max(per.values()), coordinator),
        "loaded_sessions_bound": max(max(per.values()), coordinator) + LIQUIDITY_LOOKBACK + 10,
    }


def evidence(
    factory: sessionmaker[Session], path: Path, *, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Write the evidence artifact for X1 (``path`` must not exist)."""
    if path.exists():
        raise FileExistsError(path)
    now = datetime.now(UTC)
    with factory() as session, sources.alpaca_client() as client:
        excluded = [_entry(session, client, *x) for x in EXCLUDED]
        kept = [_entry(session, client, *x) for x in NOT_EXCLUDED]
        r = ResearchDataReservationRecord
        reservations = {
            res.code: {
                "dataset_sha256": res.dataset_sha256,
                "created_at": res.created_at.isoformat(),
            }
            for res in session.scalars(
                select(r).where(
                    r.code.in_(
                        [
                            "V1_2016_2019",
                            "V2_RESEARCH_US_EQUITY_V4",
                            "V2_RESEARCH_ETF_CANDIDATES_V5",
                        ]
                    )
                )
            )
        }
        ids = {uuid.UUID(x["asset_id"]) for x in excluded}
        counts: dict[str, Any] = {}
        c = ResearchUniverseCandidateRecord
        for code in CURRENT_UNIVERSES:
            per: dict[str, dict[str, int]] = {}
            for f, a in session.execute(
                select(c.formation_session, c.asset_id).where(
                    c.universe_code == code, c.member.is_(True)
                )
            ):
                slot = per.setdefault(f.isoformat(), {"members": 0, "excluded": 0})
                slot["members"] += 1
                slot["excluded"] += a in ids
            for slot in per.values():
                slot["tradable"] = slot["members"] - slot["excluded"]
            assets = store.universe_members(session, code, known_at=now, apply_exclusions=False)
            counts[code] = {
                "formations": dict(sorted(per.items())),
                "distinct_assets": len(assets),
                "distinct_assets_excluded": len(set(assets) & ids),
                "distinct_assets_tradable": len(set(assets) - ids),
            }
        doc = {
            "code": CODE,
            "preregistration": PREREGISTRATION,
            "universe": UNIVERSE,
            "generated_at": now.isoformat(),
            "dataset_sha256_now": dataset_sha256(session),
            "reservations": reservations,
            "excluded": excluded,
            "not_excluded": kept,
            "member_counts": counts,
            "lookback": lookback_bounds(),
            **(extra or {}),
        }
    path.write_text(json.dumps(doc, indent=1, default=str))
    return {"path": str(path), "sha256": sha256_file(path), "excluded": len(excluded)}


def register(
    factory: sessionmaker[Session],
    document: Path,
    evidence_path: Path,
    *,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Record X1 once: the pre-registration (no reservation codes: it authorizes no run) and
    one exclusion row per asset, in one transaction."""
    doc_sha, ev_sha = sha256_file(document), sha256_file(evidence_path)
    ev = json.loads(evidence_path.read_text())
    if ev_sha not in document.read_text():
        raise ValueError("the pre-registration document does not cite the evidence's sha256")
    if ev["code"] != CODE or len(ev["excluded"]) != len(EXCLUDED):
        raise ValueError("the evidence is not X1's")
    with factory() as session, session.begin():
        p, x = ResearchPreregistrationRecord, ResearchExclusionRecord
        if session.scalar(select(p.id).where(p.code == PREREGISTRATION)) is not None:
            raise ValueError(f"{PREREGISTRATION} is already registered (append-only)")
        if session.scalar(select(x.id).where(x.code == CODE).limit(1)) is not None:
            raise ValueError(f"{CODE} is already registered (append-only)")
        rows = []
        for entry, (label, event, basis) in zip(ev["excluded"], EXCLUDED, strict=True):
            asset_id, _ = resolve(session, label, event)  # the event is still in the data
            if str(asset_id) != entry["asset_id"]:
                raise ValueError(f"{label}: the evidence names another asset")
            progress(f"{label} {event} -> {asset_id}")
            rows.append(
                ResearchExclusionRecord(
                    code=CODE,
                    asset_id=asset_id,
                    symbol=label,
                    universe_code=UNIVERSE,
                    scope="ASSET",
                    event_session=event,
                    reason=f"phantom split ({basis}); {CONTEXT[label]}",
                    preregistration_code=PREREGISTRATION,
                    document_sha256=doc_sha,
                    evidence={"evidence_sha256": ev_sha, **entry},
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
        session.add_all(rows)
    return {"preregistration": PREREGISTRATION, "document_sha256": doc_sha, "excluded": len(rows)}


__all__ = ["CODE", "EXCLUDED", "PREREGISTRATION", "evidence", "register", "resolve"]
