"""X4.3.2 (D-249): evidence added for the identity-graph revision, versioned apart.

A third write-once store, ``config/simulation/evidence/x432``, with its own manifest. Neither
the X4.1 store nor the X4.3 store is written to.

* ``provider/corporate_actions/<sha>.json.gz``: the provider's corporate-action records
  (every type except cash dividends: splits, spin-offs, mergers, name changes, redemptions,
  worthless removals) for the tickers under investigation. Requested by processing date over
  the window that holds every 2016-2019 event (D-244), raw.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from app.backtesting import dividends
from app.research import sources
from app.research import x4_evidence as x4

ROOT = Path("config/simulation/evidence/x432")
CA_TYPES = (
    "forward_split,reverse_split,unit_split,stock_dividend,spin_off,cash_merger,stock_merger,"
    "stock_and_cash_merger,redemption,name_change,worthless_removal,rights_distribution"
)
# REDACTED_FOR_GITHUB
CA_TICKERS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


class Store(x4.Store):
    KIND = "x432_evidence_manifest"
    DECISION = "D-249"

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)


def capture_corporate_actions(
    store: Store, tickers: tuple[str, ...] = CA_TICKERS, client: Any = None
) -> dict[str, Any]:
    lo, hi = dividends.process_window(date(2016, 1, 5), date(2019, 12, 31))
    rel = f"provider/corporate_actions/{x4.sha256(','.join(tickers).encode())[:12]}.json.gz"
    if store.has(rel):
        return {"skipped": rel}
    own = client is None
    client = client or sources.alpaca_client()
    pages: list[dict[str, Any]] = []
    try:
        token: str | None = None
        while True:
            params = {
                "symbols": ",".join(tickers),
                "types": CA_TYPES,
                "start": lo.isoformat(),
                "end": hi.isoformat(),
                "limit": "1000",
                **({"page_token": token} if token else {}),
            }
            code, body, error, attempts = x4._get(
                client, f"{sources.DATA_API_BASE_URL}{dividends.PATH}", params
            )
            pages.append({"status": code, "error": error, "attempts": attempts, "body": body})
            token = body.get("next_page_token") if isinstance(body, dict) else None
            if error or not token:
                break
    finally:
        if own:
            client.close()
    store.put_json(
        rel,
        {"symbols": list(tickers), "pages": pages},
        {
            "kind": "alpaca_corporate_actions",
            "endpoint": dividends.PATH,
            "types": CA_TYPES,
            "start": lo.isoformat(),
            "end": hi.isoformat(),
            "note": "start/end filter on the processing date (D-244)",
        },
    )
    store.save()
    return {
        "artifact": rel,
        "pages": len(pages),
        "errors": [p["error"] for p in pages if p["error"]],
    }


def corporate_actions(store: Store) -> list[dict[str, Any]]:
    """Every captured record, flattened: type, symbols, dates and the raw record."""
    out = []
    for rel in sorted(k for k in store.entries if k.startswith("provider/corporate_actions/")):
        for page in x4.read_json(store.path(rel))["pages"]:
            body = (page.get("body") or {}).get("corporate_actions") or {}
            for kind, rows in sorted(body.items()):
                for i, r in enumerate(rows or []):
                    out.append({"type": kind, "artifact": rel, "index": i, **r})
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
    "CA_TICKERS",
    "ROOT",
    "Store",
    "capture_corporate_actions",
    "corporate_actions",
    "verify",
]
