"""X4.3 (D-247): evidence added for historical entity resolution, versioned apart from X4.1.

The X4.1 store (``config/simulation/evidence/x4``) is never written to. What X4.3 needs beyond
it is captured here, in its own write-once store (``config/simulation/evidence/x43``) with its
own manifest, under the same rules: every artifact keeps its source and retrieval time, and a
path is never replaced.

* ``db/provider_identities.json.gz``: a read-only snapshot of the simulation database's
  point-in-time provider identities (``research_securities`` with provider ``alpaca-asof``,
  every revision): the request (ticker, ``asof``), first and last bar, the recorded delisting
  and the entity hash. These are the identities whose prices the research universes use.
* ``db/provider_bars/<h>.json.gz``: those identities' stored daily bars (SIP, raw and
  split-adjusted close, volume; latest revision per session), chunked by the first hex digit
  of the asset id.
* ``provider/asof/<asof>.json.gz``: raw provider bars for the investigated tickers, requested
  with several ``asof`` mapping dates, to locate where the provider's ticker mapping changes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.research import sources
from app.research import x4_evidence as x4

ROOT = Path("config/simulation/evidence/x43")
BARS_START, BARS_END = date(2016, 1, 4), date(2019, 12, 31)
# REDACTED_FOR_GITHUB
# REDACTED_FOR_GITHUB
PROBE_TICKERS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
# Mapping dates: the formations, the current-holder date the research master pinned, and the
# sessions either side of each provider history break the X3.1/X4 audits found.
PROBE_ASOF: tuple[date, ...] = (
    date(2016, 12, 30),
    date(2017, 6, 23),
    date(2017, 6, 26),
    date(2017, 8, 1),
    date(2017, 8, 2),
    date(2017, 9, 19),
    date(2017, 9, 20),
    date(2017, 12, 29),
    date(2018, 12, 31),
    date(2019, 1, 2),
    date(2026, 11, 14),
)


class Store(x4.Store):
    KIND = "x43_evidence_manifest"
    DECISION = "D-247"

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)


# ---------------------------------------------------------------------- database (read-only)
IDENTITY_SQL = """
select symbol, revision, provider_status, security_type, type_basis, cik, scan_start::text,
       scan_end::text, first_bar::text, last_bar::text, delisted_on::text, delisting_basis,
       asset_id::text, details, created_at::text
from research_securities where provider = 'alpaca-asof'
order by asset_id, created_at, revision
"""
BARS_SQL = """
select distinct on (asset_id, adjustment, event_time)
       asset_id::text, adjustment, (event_time at time zone 'UTC')::date::text, close::text,
       volume::text
from market_bars
where provider = 'alpaca' and feed = 'sip' and timeframe = '1Day'
  and observation_kind = 'HISTORICAL' and adjustment in ('RAW', 'SPLIT')
  and asset_id in (select asset_id from research_securities where provider = 'alpaca-asof')
order by asset_id, adjustment, event_time, revision desc
"""


def provider_identities(factory: sessionmaker[Session]) -> dict[str, Any]:
    """The identities and their bars, read in one read-only transaction."""
    with factory() as s:
        s.execute(text("set transaction read only"))
        database = s.scalar(text("select current_database()"))
        head = s.scalar(text("select version_num from alembic_version"))
        cols = [
            "symbol", "revision", "provider_status", "security_type", "type_basis", "cik",
            "scan_start", "scan_end", "first_bar", "last_bar", "delisted_on", "delisting_basis",
            "asset_id", "details", "created_at",
        ]  # fmt: skip
        ids = [dict(zip(cols, r, strict=True)) for r in s.execute(text(IDENTITY_SQL))]
        bars: dict[str, dict[str, dict[str, list[str | None]]]] = defaultdict(dict)
        for asset, adj, day, close, volume in s.execute(text(BARS_SQL)):
            row = bars[asset].setdefault(day, {})
            row[adj] = [close, volume]
        s.rollback()
    none: list[str | None] = [None, None]
    series = {
        a: [
            [
                d,
                (v.get("RAW") or none)[0],
                (v.get("SPLIT") or none)[0],
                (v.get("RAW") or v.get("SPLIT") or none)[1],
            ]
            for d, v in sorted(days.items())
        ]
        for a, days in sorted(bars.items())
    }
    return {"database": database, "alembic_head": head, "identities": ids, "bars": series}


def capture_identities(factory: sessionmaker[Session], store: Store) -> dict[str, Any]:
    snap = provider_identities(factory)
    source = {
        "kind": "simulation_database_snapshot",
        "database": snap["database"],
        "alembic_head": snap["alembic_head"],
        "query": "research_securities where provider = 'alpaca-asof' (every revision)",
    }
    if not store.has("db/provider_identities.json.gz"):
        store.put_json("db/provider_identities.json.gz", snap["identities"], source)
    chunks: dict[str, dict[str, Any]] = defaultdict(dict)
    for asset, rows in snap["bars"].items():
        chunks[asset[0]][asset] = rows
    bar_source = {
        **source,
        "query": "market_bars: alpaca sip 1Day HISTORICAL, RAW and SPLIT close and volume, "
        "latest revision per session, for the alpaca-asof identities",
        "columns": ["session", "raw_close", "split_adjusted_close", "volume"],
    }
    for h, body in sorted(chunks.items()):
        rel = f"db/provider_bars/{h}.json.gz"
        if not store.has(rel):
            store.put_json(rel, body, bar_source)
    store.save()
    return {
        "identities": len(snap["identities"]),
        "assets_with_bars": len(snap["bars"]),
        "bars": sum(len(v) for v in snap["bars"].values()),
    }


# ---------------------------------------------------------------------- provider (read-only GET)
def capture_asof(store: Store, client: Any = None) -> dict[str, Any]:
    """Raw bars of :data:`PROBE_TICKERS` for each :data:`PROBE_ASOF` mapping date."""
    own = client is None
    client = client or sources.alpaca_client()
    out = {}
    try:
        for asof in PROBE_ASOF:
            rel = f"provider/asof/{asof.isoformat()}.json.gz"
            if store.has(rel):
                continue
            body = x4._bars_request(client, list(PROBE_TICKERS), BARS_START, BARS_END, asof)
            store.put_json(
                rel,
                body,
                {
                    "kind": "alpaca_bars",
                    "url": f"{sources.DATA_API_BASE_URL}/v2/stocks/bars",
                    "symbols": list(PROBE_TICKERS),
                    "start": BARS_START.isoformat(),
                    "end": BARS_END.isoformat(),
                    "asof": asof.isoformat(),
                    "feed": "sip",
                    "adjustment": "raw",
                },
            )
            out[rel] = sum(
                len(rows)
                for page in body["pages"]
                for rows in ((page.get("body") or {}).get("bars") or {}).values()
            )
            store.save()
    finally:
        if own:
            client.close()
    return out


def probe_bars(store: Store) -> dict[date, dict[str, list[tuple[str, float]]]]:
    """asof -> ticker -> [(session, raw close)] from the captured probes."""
    out: dict[date, dict[str, list[tuple[str, float]]]] = {}
    for rel in sorted(k for k in store.entries if k.startswith("provider/asof/")):
        asof = date.fromisoformat(rel.rsplit("/", 1)[-1][:10])
        by: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for page in x4.read_json(store.path(rel))["pages"]:
            for sym, rows in ((page.get("body") or {}).get("bars") or {}).items():
                by[sym] += [(r["t"][:10], float(r["c"])) for r in rows]
        out[asof] = {k: sorted(v) for k, v in by.items()}
    return out


def verify(store: Store) -> dict[str, Any]:
    bad = []
    for rel, e in sorted(store.entries.items()):
        p = store.path(rel)
        if not p.exists():
            bad.append({"path": rel, "problem": "missing"})
        elif x4.sha256(p.read_bytes()) != e["sha256"]:
            bad.append({"path": rel, "problem": "sha256 differs"})
    return {"artifacts": len(store.entries), "hash_failures": bad, "ok": not bad}


__all__ = [
    "PROBE_ASOF",
    "PROBE_TICKERS",
    "ROOT",
    "Store",
    "capture_asof",
    "capture_identities",
    "probe_bars",
    "provider_identities",
    "verify",
]
