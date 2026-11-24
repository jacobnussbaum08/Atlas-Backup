"""X4.6.1 (D-253): corrections to the historical universe and member data found by X4.6.

Earlier artifacts are never changed. This module writes separately versioned revisions:

* ``x44_historical_universe_v2.json.gz``: the X4.4 universe with corrected eligibility (members
  removed before their formation; a ranking series spliced across share classes) and the top N
  recomputed under the unchanged V4/V5 rules. Every changed record carries its provenance, and
  the revision lists what it supersedes.
* ``x45_member_data_v2.json.gz``: the X4.5 member data for the revised universe, with corrected
  REDACTED_FOR_GITHUB
  newly admitted members (captured into this module's store) and documented terminal values.

Evidence (write-once store ``config/simulation/evidence/x461``, GET only):

* ``sec/filings/<accession>.txt.gz``: EDGAR filings named by the stored X4.1 form indexes around
  each exit (completion 8-Ks, 6-Ks, successor 8-K12Bs, tender-offer amendments) and around the
  REDACTED_FOR_GITHUB
* ``provider/...``: bars and corporate actions of the newly admitted members (as X4.5).
"""

from __future__ import annotations

import gzip
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.research import sources
from app.research import x4_evidence as x4

ROOT = Path("config/simulation/evidence/x461")
CAPTURE_FORMS = ("8-K", "6-K", "8-K12B", "8-K12G3", "SC TO-T/A")
EXIT_WINDOW = (-5, 7)  # days before the last trade, days after the removal filing

# Filings named in the stored indexes that bear on the two open questions besides exits.
EXTRA_FILINGS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


class Store(x4.Store):
    KIND = "x461_evidence_manifest"
    DECISION = "D-253"

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)


def _day(d: str, n: int) -> str:
    return (date.fromisoformat(d) + timedelta(days=n)).isoformat()


_LINE = re.compile(
    r"^(?P<form>.+?)\s{2,}(?P<name>.+?)\s{2,}(?P<cik>\d+)\s{2,}(?P<date>\d{4}-\d{2}-\d{2})"
    r"\s{2,}(?P<file>\S+)\s*$"
)


def index_filings(x4s: x4.Store, ciks: set[int]) -> dict[int, list[dict[str, str]]]:
    """Every filing of some CIKs in the stored quarterly form indexes (2016-2020)."""
    out: dict[int, list[dict[str, str]]] = defaultdict(list)
    for rel in sorted(k for k in x4s.entries if k.startswith("sec/full-index/")):
        if rel.split("/")[-1][:4] not in ("2016", "2017", "2018", "2019", "2020"):
            continue
        text = gzip.decompress(x4s.path(rel).read_bytes()).decode("latin-1")
        for line in text.splitlines():
            m = _LINE.match(line)
            if m and int(m["cik"]) in ciks:
                out[int(m["cik"])].append(
                    {"form": m["form"].strip(), "filed": m["date"], "path": m["file"],
                     "name": m["name"].strip()}
                )  # fmt: skip
    return {k: sorted(v, key=lambda x: (x["filed"], x["path"])) for k, v in out.items()}


def exit_slots(validation: dict[str, Any]) -> list[dict[str, Any]]:
    """The slots X4.6 found without a terminal value: their issuer, last trade and notice."""
    out = []
    for r in validation["slots"]:
        for x in r["restrictions"]:
            if x["code"] != "TERMINAL_VALUE_NOT_IN_DATASET":
                continue
            ex = r["checks"]["exit_evidence"]
            n = (ex.get("notices") or [{}])[0]
            out.append(
                {
                    "slot": r["slot"],
                    "entity_key": None,
                    "last_traded": (r["usable_range"] or [None, None])[1],
                    "notice": n.get("accession"),
                    "notice_filed": n.get("filed"),
                }
            )
    return out


def filing_plan(
    validation: dict[str, Any], dataset: dict[str, Any], x4s: x4.Store
) -> list[dict[str, str]]:
    """The filings to capture: per exit, the completion-type filings of its issuer from five
    days before its last trade to seven days after its removal notice; plus EXTRA_FILINGS."""
    ent = {s["slot"]: s["entity_key"] for s in dataset["slots"]}
    exits = exit_slots(validation)
    ciks = {int(ent[e["slot"]].split(":")[1]) for e in exits}
    idx = index_filings(x4s, ciks)
    plan: dict[str, dict[str, str]] = {}
    for e in exits:
        cik = int(ent[e["slot"]].split(":")[1])
        anchor_lo = e["last_traded"] or e["notice_filed"]
        anchor_hi = e["notice_filed"] or e["last_traded"]
        if not anchor_lo or not anchor_hi:
            continue
        lo, hi = _day(anchor_lo, EXIT_WINDOW[0]), _day(anchor_hi, EXIT_WINDOW[1])
        for f in idx.get(cik, []):
            if f["form"] in CAPTURE_FORMS and lo <= f["filed"] <= hi:
                acc = f["path"].rsplit("/", 1)[1].removesuffix(".txt")
                plan.setdefault(
                    acc,
                    {"accession": acc, "cik": str(cik), "form": f["form"], "filed": f["filed"],
                     "path": f["path"], "reason": f"exit of {e['slot']}"},
                )  # fmt: skip
    for acc, meta in EXTRA_FILINGS.items():
        plan.setdefault(
            acc,
            {"accession": acc, **meta,
             "path": f"edgar/data/{meta['cik']}/{acc}.txt"},
        )  # fmt: skip
    return [plan[k] for k in sorted(plan)]


def capture_filings(store: Store, plan: list[dict[str, str]]) -> dict[str, Any]:
    sec = sources.Sec()
    done, failed = 0, []
    for p in plan:
        rel = f"sec/filings/{p['accession']}.txt.gz"
        if store.has(rel):
            continue
        url = f"https://www.sec.gov/Archives/{p['path']}"
        r = sec.get(url)
        store.put(
            rel,
            x4.gz(r.content),
            {"kind": "edgar_filing", "url": url, **{k: p[k] for k in ("cik", "form", "filed",
                                                                       "reason")}},
            http_status=r.status_code,
        )  # fmt: skip
        if r.status_code != 200:
            failed.append(p["accession"])
        done += 1
        if done % 20 == 0:
            store.save()
    store.save()
    return {"planned": len(plan), "captured": done, "failed": failed}
