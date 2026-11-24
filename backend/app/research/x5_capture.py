# ruff: noqa: E501  (the capture tables list one request per line)
"""X5 (D-254): evidence for corporate actions, terminal values and total-return verification.

Write-once store ``config/simulation/evidence/x5`` (GET only, nothing else):

* ``sec/filings/<accession>.txt.gz``: EDGAR filings named by the stored X4.1 form indexes:
  REDACTED_FOR_GITHUB
  REDACTED_FOR_GITHUB
* ``provider/prices/<ticker>_<asof>.json.gz``: daily SIP bars (raw) of a security received in
  an exit or distributed in a spin-off, the ticker mapped as of the event date, over a short
  window around it.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.research import sources
from app.research import x4_evidence as x4
from app.research import x45_data as x45d
from app.research import x461_corrections as x461c

ROOT = Path("config/simulation/evidence/x5")


class Store(x4.Store):
    KIND = "x5_evidence_manifest"
    DECISION = "D-254"

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)


def _f(cik: str, acc: str, form: str, filed: str, reason: str) -> dict[str, str]:
    return {"accession": acc, "cik": cik, "form": form, "filed": filed, "reason": reason,
            "path": f"edgar/data/{cik}/{acc}.txt"}  # fmt: skip


FILINGS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection

# Securities received in exits or distributed in spin-offs: (ticker, event date, purpose).
PRICES = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
PRICE_WINDOW = (-10, 14)


def _day(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


def capture_filings(store: Store) -> dict[str, Any]:
    return x461c.capture_filings(store, FILINGS)  # type: ignore[arg-type]


def capture_prices(store: Store, client: Any = None) -> dict[str, Any]:
    own = client is None
    client = client or sources.alpaca_client()
    done = 0
    try:
        for ticker, when, purpose in PRICES:
            rel = f"provider/prices/{ticker}_{when}.json.gz"
            if store.has(rel):
                continue
            lo, hi = _day(when, PRICE_WINDOW[0]), _day(when, PRICE_WINDOW[1])
            body = x45d._bars(client, [ticker], lo, hi, when, "raw")
            store.put_json(rel, body, {"kind": "alpaca_bars", "ticker": ticker, "asof": when,
                                       "start": lo, "end": hi, "adjustment": "raw",
                                       "purpose": purpose})  # fmt: skip
            done += 1
        store.save()
    finally:
        if own:
            client.close()
    return {"captured": done, "planned": len(PRICES)}
