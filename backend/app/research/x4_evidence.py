"""X4.1 (D-245): historical universe evidence capture. Evidence only: no universe is formed,
no database row is written (the simulation database is read for a snapshot of V4/V5 and the
security master), GET-only requests, credentials never stored or printed.

Why: the X4 audit found the research universes take their candidate tickers from the
provider's *2026* asset list, which drops delisted tickers, so the 2016-2019 universes miss
companies that later disappeared (survivorship look-ahead). Rebuilding them needs evidence of
what was listed and traded *then*, kept so that later milestones work offline.

Evidence, under :data:`ROOT` (``config/simulation/evidence/x4``), every artifact listed in
``manifest.json`` with its source, request parameters, retrieval time and SHA-256:

* ``provider/assets``: the provider's asset list today (active and inactive), raw.
* ``provider/sweeps/<formation>/<source>``: every 1-4 letter ticker, and the 5-letter and
  dotted share-class candidates of independent lists (``classes``), requested with SIP RAW
  daily bars over the formation's 60-session window with ``asof`` = formation (the security
  that held the ticker then). Raw response pages per request, failures included.
* ``provider/history``: the first week of the provider's history (2016-01-04..08) for every
  ticker that traded at the 2016 formation (the ETF rule's history requirement).
* ``provider/dividends``: cash-dividend responses for discovered tickers missing from the
  provider's asset list (availability evidence; joined by symbol, so reuse is possible).
* ``archive``: Internet Archive (Wayback) captures of Nasdaq Trader's symbol directories
  (``nasdaqlisted.txt``, ``otherlisted.txt``) and Nasdaq.com company lists, bracketing each
  formation; each capture's SHA-1 is checked against the archive's own digest.
* ``sec``: EDGAR quarterly form indexes 2015-2020 (``form.gz`` as published); every Form 25,
  25-NSE (and amendments) filed 2016-2019, full submission text; company submissions JSON for
  the issuers of equity removal notices and for the audit's known cases; SEC investment-company
  series and class files (2010-2026 as published, 2015-2020 used); SEC current ticker files
  (labelled current, not point-in-time).
* ``derived``: reports computed offline from the raw evidence only (formation metrics,
  discovery, typing evidence, removal notices, validation, coverage), each hashed. ``verify``
  recomputes every file hash and re-derives every report.

Rules: an artifact is never replaced (a capture resumes by skipping what the manifest holds);
compressed artifacts are deterministic (gzip without timestamp); unsuccessful requests are
stored with their status and error, never dropped; nothing is called exhaustive that is not
(5-letter and dotted tickers come only from the candidate lists, see the coverage report).
"""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import itertools
import json
import os
import re
import statistics
import string
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting import dividends
from app.backtesting.data import sessions_back
from app.data_engine.providers.alpaca.reference import PAPER_TRADING_API_BASE_URL
from app.research import sources
from app.research.universe import EXPLORATION_SYMBOLS

ROOT = Path("config/simulation/evidence/x4")
FORMATIONS = (date(2016, 12, 30), date(2017, 12, 29), date(2018, 12, 31))
WINDOW_SESSIONS = 60
HISTORY = (date(2016, 1, 4), date(2016, 1, 8))
BATCH = 100  # tickers per bars request
CHUNK = 50  # requests per sweep artifact
SEC_QUARTERS = [(y, q) for y in range(2015, 2021) for q in range(1, 5)]
REMOVAL_YEARS = range(2016, 2020)
REMOVAL_FORMS = ("25", "25-NSE", "25/A", "25-NSE/A")
FUND_FILE_PAGE = (
    "https://www.sec.gov/data-research/sec-markets-data/investment-company-series-class-information"
)
SEC_CURRENT = (
    "https://www.sec.gov/files/company_tickers.json",
    "https://www.sec.gov/files/company_tickers_exchange.json",
    "https://www.sec.gov/files/company_tickers_mf.json",
)
# Point-in-time symbol lists kept by the Internet Archive (family -> original URL).
ARCHIVE_FAMILIES = {
    "symdir_nasdaqlisted": "nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "symdir_otherlisted": "nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    "companylist_nyse": "www.nasdaq.com/screening/companies-by-industry.aspx?exchange=NYSE"
    "&render=download",
    "companylist_nasdaq": "www.nasdaq.com/screening/companies-by-industry.aspx?exchange=NASDAQ"
    "&render=download",
    "companylist_amex": "www.nasdaq.com/screening/companies-by-name.aspx?exchange=AMEX"
    "&render=download",
}
ARCHIVE_RANGE = ("2015", "2020")
# Deliberately excluded from the research universes (universe.EXPLORATION_SYMBOLS; SPY from
# the equity universe only, and it is a fund): absent by rule, not by survivorship.
EXCLUDED_BY_RULE = frozenset(EXPLORATION_SYMBOLS)
ETP_NAME = re.compile(r"\b(ETFs?|ETNs?|Exchange[- ]Traded)\b", re.I)
# The SEC lists its series/class file without a year in the name under 2016 (evidence: the
# captured listing page, sec/funds/listing_page.html).
FUND_FILE_YEARS = {"investment_company_series_class.csv.gz": 2016}
# Issuers of the audit's known cases (CIK -> why), always captured.
KNOWN_CIKS = {
    "0001122304": "REDACTED_FOR_GITHUB",
    "0001105705": "REDACTED_FOR_GITHUB",
    "0001011006": "REDACTED_FOR_GITHUB",
    "0001175580": "REDACTED_FOR_GITHUB",
    "0000756894": "REDACTED_FOR_GITHUB",
    "0001327811": "REDACTED_FOR_GITHUB",
    "0001329099": "REDACTED_FOR_GITHUB",
    "0001100663": "REDACTED_FOR_GITHUB",
    "0000930667": "REDACTED_FOR_GITHUB",
    "0000912750": "REDACTED_FOR_GITHUB",
    "0000721683": "REDACTED_FOR_GITHUB",
    "0000921112": "REDACTED_FOR_GITHUB",
    "0000701374": "REDACTED_FOR_GITHUB",
}
EQUITY_CLASS = re.compile(
    r"common|ordinary|depositary|depository|\bads\b|\badr\b|shares|stock|units|"
    r"beneficial interest|limited partnership|partnership interest",
    re.I,
)
NOT_EQUITY = re.compile(r"note|debenture|bond|preferred|warrant|right|due \d{4}|%", re.I)
WAYBACK_INTERVAL = 1.0  # seconds between Internet Archive requests


# ---------------------------------------------------------------------- the store
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gz(data: bytes) -> bytes:
    """Deterministic gzip (no timestamp, no file name)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, filename="") as f:
        f.write(data)
    return buf.getvalue()


def read_json(path: Path) -> Any:
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)


class Store:
    """Write-once artifacts under ``root`` with a manifest of their provenance."""

    KIND = "x4_evidence_manifest"
    DECISION = "D-245"

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.manifest_path = root / "manifest.json"
        self.entries: dict[str, dict[str, Any]] = {}
        self.cache: dict[Any, Any] = {}  # offline parses of raw evidence (never persisted)
        if self.manifest_path.exists():
            self.entries = json.loads(self.manifest_path.read_text(encoding="utf-8"))["artifacts"]

    def has(self, rel: str) -> bool:
        return rel in self.entries

    def put(self, rel: str, data: bytes, source: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """Store ``data`` at ``rel`` once. A second call for the same path is refused."""
        path = self.root / rel
        if rel in self.entries or path.exists():
            raise FileExistsError(f"{rel} exists: evidence is never replaced")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        content = gzip.decompress(data) if rel.endswith(".gz") else data
        entry = {
            "path": rel,
            "sha256": sha256(data),
            "bytes": len(data),
            "content_sha256": sha256(content),
            "source": source,
            "retrieved_at": datetime.now(UTC).isoformat(),
            **extra,
        }
        self.entries[rel] = entry
        return entry

    def put_json(self, rel: str, obj: Any, source: dict[str, Any], **extra: Any) -> dict[str, Any]:
        data = canonical(obj)
        return self.put(rel, gz(data) if rel.endswith(".gz") else data, source, **extra)

    def save(self) -> str:
        """Merge with the manifest on disk (captures may run in parallel processes, each
        adding different paths) under a lock file, then write it atomically."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / "manifest.lock"
        for _ in range(600):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.1)
        else:
            raise TimeoutError("manifest lock held for a minute")
        try:
            if self.manifest_path.exists():
                disk = json.loads(self.manifest_path.read_text(encoding="utf-8"))["artifacts"]
                for rel, e in disk.items():
                    mine = self.entries.get(rel)
                    if mine is not None and mine["sha256"] != e["sha256"]:
                        raise ValueError(f"{rel}: two different artifacts for one path")
                    self.entries.setdefault(rel, e)
            body = json.dumps(
                {"kind": self.KIND, "decision": self.DECISION, "artifacts": self.entries},
                indent=1,
                sort_keys=True,
            )
            tmp = self.root / f"manifest.{os.getpid()}.tmp"
            tmp.write_text(body, encoding="utf-8", newline="\n")
            tmp.replace(self.manifest_path)
        finally:
            os.close(fd)
            lock.unlink()
        return sha256(body.encode())

    def path(self, rel: str) -> Path:
        return self.root / rel


# ---------------------------------------------------------------------- HTTP (raw)
def _get(
    client: httpx.Client, url: str, params: dict[str, Any] | None = None, attempts: int = 8
) -> tuple[int | None, Any, str | None, int]:
    """(status, body, error, attempts). Retries rate limits, 5xx and transport errors; the
    final outcome is returned, never raised, so a failure can be stored as evidence."""
    error: str | None = None
    status: int | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = client.get(url, params=params)
        except httpx.HTTPError as exc:
            error, status = type(exc).__name__, None
            time.sleep(min(2 * attempt, 30))
            continue
        status = r.status_code
        if status == 429 or status >= 500:
            error = f"HTTP {status}"
            time.sleep(min(5 + 10 * (attempt - 1), 60))
            continue
        try:
            body: Any = r.json()
        except ValueError:
            body = r.text
        return status, body, None if status < 400 else f"HTTP {status}", attempt
    return status, None, error, attempts


# ---------------------------------------------------------------------- provider
def window(formation: date) -> tuple[date, date]:
    return sessions_back(formation, WINDOW_SESSIONS - 1), formation


def letter_tickers(length: int) -> list[str]:
    return ["".join(p) for p in itertools.product(string.ascii_uppercase, repeat=length)]


def _bars_request(
    client: httpx.Client, symbols: list[str], start: date, end: date, asof: date
) -> dict[str, Any]:
    """One bars request (all pages), raw."""
    params: dict[str, Any] = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": f"{start.isoformat()}T00:00:00Z",
        "end": f"{end.isoformat()}T23:59:59Z",
        "feed": "sip",
        "adjustment": "raw",
        "limit": "10000",
        "sort": "asc",
        "asof": asof.isoformat(),
    }
    pages: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        p = {**params, **({"page_token": token} if token else {})}
        status, body, error, attempts = _get(
            client, f"{sources.DATA_API_BASE_URL}/v2/stocks/bars", p
        )
        pages.append(
            {
                "page_token": token,
                "status": status,
                "attempts": attempts,
                "error": error,
                "body": body,
            }
        )
        if error or not isinstance(body, dict):
            break
        token = body.get("next_page_token")
        if not token:
            break
        if token in seen:
            pages[-1]["error"] = "the provider repeated a page token"
            break
        seen.add(token)
    return {
        "symbols": symbols,
        "params": {k: v for k, v in params.items() if k != "symbols"},
        "pages": pages,
        "ok": all(pg["error"] is None for pg in pages),
    }


def sweep(
    store: Store,
    formation: date,
    source: str,
    tickers: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Request ``tickers`` in batches; one artifact per :data:`CHUNK` requests. Resumes."""
    lo, hi = (start, end) if start and end else window(formation)
    batches = [tickers[i : i + BATCH] for i in range(0, len(tickers), BATCH)]
    chunks = [batches[i : i + CHUNK] for i in range(0, len(batches), CHUNK)]
    written = skipped = 0
    with sources.alpaca_client() as client:
        for n, chunk in enumerate(chunks):
            rel = f"provider/sweeps/{formation.isoformat()}/{source}/chunk_{n:05d}.json.gz"
            if store.has(rel):
                skipped += 1
                continue
            requests = [_bars_request(client, b, lo, hi, formation) for b in chunk]
            store.put_json(
                rel,
                {
                    "formation": formation.isoformat(),
                    "source": source,
                    "window": [lo.isoformat(), hi.isoformat()],
                    "requests": requests,
                },
                {
                    "kind": "alpaca_bars",
                    "endpoint": "/v2/stocks/bars",
                    "asof": formation.isoformat(),
                    "window": [lo.isoformat(), hi.isoformat()],
                    "feed": "sip",
                    "adjustment": "raw",
                    "tickers": sum(len(b) for b in chunk),
                    "requests": len(chunk),
                },
                failed_requests=sum(1 for r in requests if not r["ok"]),
            )
            store.save()
            written += 1
            progress(f"{formation} {source} chunk {n + 1}/{len(chunks)}")
    return {"chunks": len(chunks), "written": written, "skipped": skipped}


def capture_assets(store: Store) -> dict[str, Any]:
    out = {}
    with sources.alpaca_client() as client:
        for status in ("active", "inactive"):
            rel = f"provider/assets/assets_{status}.json.gz"
            if store.has(rel):
                continue
            code, body, error, attempts = _get(
                client,
                f"{PAPER_TRADING_API_BASE_URL}/v2/assets",
                {"status": status, "asset_class": "us_equity"},
            )
            store.put_json(
                rel,
                {"status": code, "error": error, "attempts": attempts, "body": body},
                {
                    "kind": "alpaca_assets",
                    "endpoint": "/v2/assets",
                    "status": status,
                    "note": "today's list (not point in time)",
                },
            )
            out[status] = len(body) if isinstance(body, list) else error
    store.save()
    return out


def capture_history(store: Store, progress: Callable[[str], None] = print) -> dict[str, Any]:
    """The provider's first history week for every ticker that traded at the 2016 formation."""
    f = FORMATIONS[0]
    traded = sorted(t for t, m in formation_metrics(store, f).items() if m["traded_bars"])
    return sweep(store, f, "history", traded, start=HISTORY[0], end=HISTORY[1], progress=progress)


def capture_dividends(store: Store, progress: Callable[[str], None] = print) -> dict[str, Any]:
    """Cash-dividend responses for discovered eligible tickers missing from today's asset
    list (ex-dates 2016-01-05..2019-12-31, processing-date window of D-244)."""
    wanted = {
        r["ticker"]
        for f in FORMATIONS
        for r in discovered(store, f)
        if set(r["reasons"]) & {"absent_from_provider_assets_today", "absent_from_research_master"}
    }
    covered = {
        t
        for rel in store.entries
        if rel.startswith("provider/dividends/")
        for t in read_json(store.path(rel))["symbols"]
    }
    tickers = sorted(wanted - covered)
    lo, hi = dividends.process_window(date(2016, 1, 5), date(2019, 12, 31))
    written = 0
    with sources.alpaca_client() as client:
        for n in range(0, len(tickers), 50):
            part = tickers[n : n + 50]
            rel = f"provider/dividends/chunk_{sha256(','.join(part).encode())[:12]}.json.gz"
            if store.has(rel):
                continue
            pages: list[dict[str, Any]] = []
            token: str | None = None
            while True:
                p = {
                    "symbols": ",".join(part),
                    "types": "cash_dividend",
                    "start": lo.isoformat(),
                    "end": hi.isoformat(),
                    "limit": "1000",
                    **({"page_token": token} if token else {}),
                }
                code, body, error, attempts = _get(
                    client, f"{sources.DATA_API_BASE_URL}{dividends.PATH}", p
                )
                pages.append({"status": code, "error": error, "attempts": attempts, "body": body})
                token = body.get("next_page_token") if isinstance(body, dict) else None
                if error or not token:
                    break
            store.put_json(
                rel,
                {"symbols": part, "pages": pages},
                {
                    "kind": "alpaca_corporate_actions",
                    "endpoint": dividends.PATH,
                    "types": "cash_dividend",
                    "process_window": [lo.isoformat(), hi.isoformat()],
                    "note": "symbol-joined: a reused ticker returns its other holders' rows too",
                },
            )
            store.save()
            written += 1
            progress(f"dividends chunk {n // 50 + 1}")
    return {"tickers": len(tickers), "chunks_written": written}


# ---------------------------------------------------------------------- Internet Archive
def wayback_sha1(data: bytes) -> str:
    """The archive's digest: base32 of the SHA-1 of the captured payload."""
    return base64.b32encode(hashlib.sha1(data).digest()).decode()  # noqa: S324


def capture_archive(store: Store, progress: Callable[[str], None] = print) -> dict[str, Any]:
    """For every family, the archive's capture index (raw) and the captures bracketing each
    formation (the last on or before it and the first after it)."""
    out: dict[str, Any] = {}
    headers = {"User-Agent": sources.SEC_HEADERS["User-Agent"]}
    with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
        for family, url in ARCHIVE_FAMILIES.items():
            rel = f"archive/cdx/{family}.json"
            if not store.has(rel):
                params = {
                    "url": url,
                    "from": ARCHIVE_RANGE[0],
                    "to": ARCHIVE_RANGE[1],
                    "output": "json",
                    "fl": "timestamp,original,statuscode,digest,length",
                }
                code, body, error, attempts = _get(
                    client, "http://web.archive.org/cdx/search/cdx", params
                )
                store.put_json(
                    rel,
                    {"status": code, "error": error, "attempts": attempts, "body": body},
                    {"kind": "wayback_cdx", "url": url, "range": list(ARCHIVE_RANGE)},
                )
                store.save()
                time.sleep(WAYBACK_INTERVAL)
            rows = _cdx_rows(read_json(store.path(rel)))
            chosen = _bracketing(rows)
            out[family] = {"captures_listed": len(rows), "chosen": sorted(chosen)}
            for ts in sorted(chosen):
                rel_c = f"archive/{family}/{ts}.raw"
                if store.has(rel_c):
                    continue
                row = next(r for r in rows if r["timestamp"] == ts)
                code_, data, err = _wayback_raw(client, ts, row["original"])
                ok = data is not None and wayback_sha1(data) == row["digest"]
                store.put(
                    rel_c,
                    data if data is not None else b"",
                    {
                        "kind": "wayback_capture",
                        "original": row["original"],
                        "timestamp": ts,
                        "replay": f"http://web.archive.org/web/{ts}id_/{row['original']}",
                    },
                    http_status=code_,
                    error=err,
                    archive_digest=row["digest"],
                    digest_verified=ok,
                    brackets=chosen[ts],
                )
                store.save()
                progress(f"archive {family} {ts} verified={ok}")
                time.sleep(WAYBACK_INTERVAL)
    return out


def _cdx_rows(doc: dict[str, Any]) -> list[dict[str, str]]:
    body = doc.get("body")
    if not isinstance(body, list) or not body:
        return []
    head = body[0]
    rows = [dict(zip(head, r, strict=False)) for r in body[1:]]
    return [r for r in rows if r.get("statuscode") == "200"]


def _bracketing(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """timestamp -> the formations (with side) it brackets."""
    out: dict[str, list[str]] = defaultdict(list)
    for f in FORMATIONS:
        key = f.strftime("%Y%m%d")
        before = [r["timestamp"] for r in rows if r["timestamp"][:8] <= key]
        after = [r["timestamp"] for r in rows if r["timestamp"][:8] > key]
        if before:
            out[max(before)].append(f"{f.isoformat()}:before")
        if after:
            out[min(after)].append(f"{f.isoformat()}:after")
    return dict(out)


def _wayback_raw(
    client: httpx.Client, ts: str, original: str
) -> tuple[int | None, bytes | None, str | None]:
    url = f"http://web.archive.org/web/{ts}id_/{original}"
    for attempt in range(1, 7):
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            err = type(exc).__name__
            time.sleep(5 * attempt)
            continue
        if r.status_code == 200:
            return 200, r.content, None
        err = f"HTTP {r.status_code}"
        if r.status_code not in (429, 502, 503, 504):
            return r.status_code, None, err
        time.sleep(10 * attempt)
    return None, None, err


# ---------------------------------------------------------------------- SEC
def capture_sec(store: Store, progress: Callable[[str], None] = print) -> dict[str, Any]:
    sec = sources.Sec()
    out: dict[str, Any] = {}

    def fetch(url: str) -> tuple[int, bytes]:
        r = sec.get(url)
        return r.status_code, r.content

    # Quarterly form indexes, as published.
    for y, q in SEC_QUARTERS:
        rel = f"sec/full-index/{y}Q{q}_form.gz"
        if store.has(rel):
            continue
        url = f"https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/form.gz"
        code, data = fetch(url)
        store.put(rel, data, {"kind": "edgar_full_index", "url": url}, http_status=code)
        store.save()
        progress(f"form index {y}Q{q} {code}")
    # Removal notices 2016-2019, one bundle per quarter.
    notices = removal_index(store)
    for (y, q), rows in sorted(notices.items()):
        rel = f"sec/form25/{y}Q{q}.jsonl.gz"
        if store.has(rel):
            continue
        lines = []
        for row in rows:
            url = f"https://www.sec.gov/Archives/{row['path']}"
            code, data = fetch(url)
            lines.append(
                canonical(
                    {**row, "url": url, "http_status": code, "text": data.decode("latin-1")}
                ).decode()
            )
        store.put(
            rel,
            gz("\n".join(lines).encode()),
            {"kind": "edgar_removal_notices", "forms": list(REMOVAL_FORMS), "quarter": f"{y}Q{q}"},
            documents=len(rows),
            failed=sum(1 for ln in lines if json.loads(ln)["http_status"] != 200),
        )
        store.save()
        progress(f"removal notices {y}Q{q}: {len(rows)}")
    # Investment company series and class files (as listed by the SEC page).
    rel = "sec/funds/listing_page.html"
    if not store.has(rel):
        code, data = fetch(FUND_FILE_PAGE)
        store.put(rel, data, {"kind": "sec_page", "url": FUND_FILE_PAGE}, http_status=code)
        store.save()
    page = store.path(rel).read_text(encoding="latin-1")
    for href in sorted(set(re.findall(r'href="([^"]+\.csv)"', page))):
        name = href.rsplit("/", 1)[-1]
        rel_f = f"sec/funds/{name}.gz"
        if store.has(rel_f):
            continue
        url = f"https://www.sec.gov{href}" if href.startswith("/") else href
        code, data = fetch(url)
        store.put(rel_f, gz(data), {"kind": "sec_series_class", "url": url}, http_status=code)
        store.save()
        progress(f"fund file {name} {code}")
    # Current ticker files (not point in time; labelled).
    for url in SEC_CURRENT:
        rel_c = f"sec/current/{url.rsplit('/', 1)[-1]}.gz"
        if store.has(rel_c):
            continue
        code, data = fetch(url)
        store.put(
            rel_c,
            gz(data),
            {"kind": "sec_current_tickers", "url": url, "note": "current, not point in time"},
            http_status=code,
        )
        store.save()
    # Company submissions: equity removal issuers and the known cases.
    ciks = submission_ciks(store)
    out["submission_ciks"] = len(ciks)
    for n in range(0, len(ciks), 250):
        rel_s = f"sec/submissions/bundle_{n // 250:03d}.jsonl.gz"
        if store.has(rel_s):
            continue
        lines = []
        for cik in ciks[n : n + 250]:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            code, data = fetch(url)
            lines.append(
                canonical(
                    {
                        "cik": cik,
                        "url": url,
                        "http_status": code,
                        "text": data.decode("utf-8", "replace"),
                    }
                ).decode()
            )
        store.put(
            rel_s,
            gz("\n".join(lines).encode()),
            {"kind": "sec_submissions", "ciks": ciks[n : n + 250]},
            failed=sum(1 for ln in lines if json.loads(ln)["http_status"] != 200),
        )
        store.save()
        progress(f"submissions bundle {n // 250 + 1}")
    return out


def removal_index(store: Store) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Removal notices (Forms 25, 25-NSE and amendments) filed 2016-2019, from the indexes."""
    out: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for y, q in SEC_QUARTERS:
        if y not in REMOVAL_YEARS:
            continue
        p = store.path(f"sec/full-index/{y}Q{q}_form.gz")
        if not p.exists():
            continue
        for line in gzip.decompress(p.read_bytes()).decode("latin-1").splitlines():
            m = re.match(r"^(\S+)\s+(.+?)\s+(\d{3,10})\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$", line)
            if m and m.group(1) in REMOVAL_FORMS:
                out[(y, q)].append(
                    {
                        "form": m.group(1),
                        "company": m.group(2).strip(),
                        "cik": f"{int(m.group(3)):010d}",
                        "filed": m.group(4),
                        "path": m.group(5),
                    }
                )
    return out


def parse_notice(textbody: str) -> dict[str, Any]:
    """Issuer, exchange, class and rule provision of a removal notice (XML or text)."""

    def tag(name: str, scope: str = "") -> str | None:
        src = textbody
        if scope:
            m = re.search(rf"<{scope}>(.*?)</{scope}>", textbody, re.S)
            src = m.group(1) if m else ""
        m = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", src, re.S)
        return m.group(1).strip() if m else None

    filer = re.search(r"FILED BY:.*?COMPANY CONFORMED NAME:\s*(.+?)\n", textbody, re.S)
    return {
        "issuer_cik": tag("cik", "issuer"),
        "issuer_name": tag("entityName", "issuer"),
        "exchange_cik": tag("cik", "exchange"),
        "exchange_name": tag("entityName", "exchange")
        or (filer.group(1).strip() if filer else None),
        "class": tag("descriptionClassSecurity"),
        "rule": tag("ruleProvision"),
    }


def submission_ciks(store: Store) -> list[str]:
    """Issuers whose removal notice is for an equity class, plus :data:`KNOWN_CIKS`."""
    ciks = set(KNOWN_CIKS)
    for _key, notice in iter_notices(store):
        cls = notice.get("class") or ""
        if EQUITY_CLASS.search(cls) and not NOT_EQUITY.search(cls):
            ciks.add(notice["cik"])
    return sorted(ciks)


def iter_notices(store: Store) -> Iterable[tuple[str, dict[str, Any]]]:
    for rel in sorted(k for k in store.entries if k.startswith("sec/form25/")):
        for line in gzip.decompress(store.path(rel).read_bytes()).decode().splitlines():
            row = json.loads(line)
            parsed = parse_notice(row.get("text") or "")
            yield rel, {**{k: v for k, v in row.items() if k != "text"}, **parsed}


# ---------------------------------------------------------------------- symbol lists
SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def parse_symbol_list(family: str, data: bytes) -> list[dict[str, Any]]:
    """Rows of a symbol list: symbol (provider notation), name, exchange, etf flag."""
    textbody = data.decode("latin-1")
    out = []
    if family.startswith("symdir"):
        lines = [ln for ln in textbody.splitlines() if "|" in ln]
        if not lines:
            return []
        head = lines[0].split("|")
        for ln in lines[1:]:
            if ln.startswith("File Creation Time"):
                continue
            row = dict(zip(head, ln.split("|"), strict=False))
            sym = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
            if row.get("Test Issue") == "Y":
                continue
            out.append(
                {
                    "symbol": sym,
                    "name": (row.get("Security Name") or "").strip(),
                    "exchange": "NASDAQ"
                    if family.endswith("nasdaqlisted")
                    else row.get("Exchange"),
                    "etf": {"Y": True, "N": False}.get((row.get("ETF") or "").strip()),
                }
            )
    else:
        reader = csv.DictReader(io.StringIO(textbody))
        for row in reader:
            sym = (row.get("Symbol") or "").strip().replace("^", "-").replace("/", ".")
            out.append(
                {
                    "symbol": sym,
                    "name": (row.get("Name") or "").strip(),
                    "exchange": family.rsplit("_", 1)[-1].upper(),
                    "etf": None,
                }
            )
    return [r for r in out if r["symbol"]]


def symbol_lists(store: Store) -> list[dict[str, Any]]:
    """Every archived list capture: family, timestamp, brackets, rows."""
    key = ("lists", len(store.entries))
    if key in store.cache:
        return store.cache[key]  # type: ignore[no-any-return]
    out = []
    for rel, e in sorted(store.entries.items()):
        if not rel.startswith("archive/") or not rel.endswith(".raw") or not e.get("http_status"):
            continue
        family = rel.split("/")[1]
        out.append(
            {
                "family": family,
                "timestamp": e["source"]["timestamp"],
                "brackets": e.get("brackets", []),
                "digest_verified": e["digest_verified"],
                "rows": parse_symbol_list(family, store.path(rel).read_bytes()),
            }
        )
    store.cache[key] = out
    return out


def fund_files(store: Store) -> dict[str, set[str]]:
    """File name -> exchange-style class tickers (5 letters ending X are mutual funds)."""
    key = ("funds", len(store.entries))
    if key in store.cache:
        return store.cache[key]  # type: ignore[no-any-return]
    out: dict[str, set[str]] = {}
    for rel in sorted(
        k for k in store.entries if k.startswith("sec/funds/") and k.endswith(".csv.gz")
    ):
        lines = gzip.decompress(store.path(rel).read_bytes()).decode("latin-1").splitlines()
        start = next((i for i, ln in enumerate(lines) if "ticker" in ln.lower()), None)
        if start is None:
            out[rel] = set()
            continue
        rows = list(csv.reader(lines[start:]))
        head = [h.strip().lower() for h in rows[0]]
        ti = next(i for i, h in enumerate(head) if "ticker" in h)
        out[rel] = {r[ti].strip().upper() for r in rows[1:] if len(r) > ti and r[ti].strip()}
    store.cache[key] = out
    return out


def class_candidates(store: Store) -> dict[str, list[str]]:
    """5-letter and dotted share-class candidates from independent lists (archived symbol
    lists, SEC fund files, SEC current tickers, today's provider list), by source."""
    by: dict[str, set[str]] = defaultdict(set)

    def keep(sym: str) -> bool:
        s = sym.strip().upper()
        return bool(SYMBOL.match(s)) and (len(s.split(".")[0]) == 5 or "." in s)

    for lst in symbol_lists(store):
        by[f"archive:{lst['family']}"].update(r["symbol"] for r in lst["rows"] if keep(r["symbol"]))
    for name, ticks in fund_files(store).items():
        by[f"sec_funds:{name.rsplit('/', 1)[-1]}"].update(
            t for t in ticks if keep(t) and not t.endswith("X")
        )
    for rel in (
        "sec/current/company_tickers.json.gz",
        "sec/current/company_tickers_exchange.json.gz",
    ):
        if store.has(rel):
            doc = read_json(store.path(rel))
            rows = (
                doc.values() if isinstance(doc, dict) and "data" not in doc else doc.get("data", [])
            )
            for r in rows:
                t = (r.get("ticker") if isinstance(r, dict) else r[2]) or ""
                t = t.replace("-", ".")
                if keep(t):
                    by[f"sec_current:{rel.rsplit('/', 1)[-1]}"].add(t)
    for status in ("active", "inactive"):
        rel = f"provider/assets/assets_{status}.json.gz"
        if store.has(rel):
            body = read_json(store.path(rel)).get("body") or []
            by[f"provider_assets:{status}"].update(
                a["symbol"] for a in body if isinstance(a, dict) and keep(a.get("symbol", ""))
            )
    return {k: sorted(v) for k, v in sorted(by.items())}


# ---------------------------------------------------------------------- derived (offline)
def _iter_sweep(
    store: Store, formation: date, source: str | None = None
) -> Iterable[tuple[str, dict[str, Any]]]:
    prefix = f"provider/sweeps/{formation.isoformat()}/"
    for rel in sorted(k for k in store.entries if k.startswith(prefix)):
        if source is not None and rel.split("/")[3] != source:
            continue
        yield rel, read_json(store.path(rel))


def formation_metrics(store: Store, formation: date) -> dict[str, dict[str, Any]]:
    """Ticker -> traded bars, last close, median dollar volume in the formation window, from
    the raw sweeps (every source except ``history``)."""
    key = ("metrics", formation, len(store.entries))
    if key not in store.cache:
        store.cache[key] = _formation_metrics(store, formation)
    return store.cache[key]  # type: ignore[no-any-return]


def _formation_metrics(store: Store, formation: date) -> dict[str, dict[str, Any]]:
    obs: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    zero: Counter[str] = Counter()
    for rel, doc in _iter_sweep(store, formation):
        if rel.split("/")[3] == "history":
            continue
        for req in doc["requests"]:
            for pg in req["pages"]:
                body = pg.get("body")
                if not isinstance(body, dict):
                    continue
                for sym, bars in (body.get("bars") or {}).items():
                    for b in bars:
                        if float(b.get("v") or 0) > 0:
                            obs[sym].append(
                                (b["t"][:10], float(b["c"]), float(b["c"]) * float(b["v"]))
                            )
                        else:
                            zero[sym] += 1
    out: dict[str, dict[str, Any]] = {}
    for sym in sorted(set(obs) | set(zero)):
        rows = sorted(set(obs.get(sym, [])))
        out[sym] = {
            "traded_bars": len(rows),
            "zero_volume_bars": zero.get(sym, 0),
            "first": rows[0][0] if rows else None,
            "last": rows[-1][0] if rows else None,
            "last_close": rows[-1][1] if rows else None,
            "mdv": statistics.median(r[2] for r in rows) if rows else 0.0,
        }
    return out


def sweep_coverage(store: Store, formation: date) -> dict[str, Any]:
    """Requested tickers, requests and failures per source."""
    by: dict[str, Counter[str]] = defaultdict(Counter)
    failed: list[dict[str, Any]] = []
    for rel, doc in _iter_sweep(store, formation):
        src = rel.split("/")[3]
        for req in doc["requests"]:
            by[src]["requests"] += 1
            by[src]["tickers"] += len(req["symbols"])
            if not req["ok"]:
                by[src]["failed_requests"] += 1
                failed.append(
                    {
                        "source": src,
                        "artifact": rel,
                        "first": req["symbols"][0],
                        "last": req["symbols"][-1],
                        "tickers": len(req["symbols"]),
                        "errors": [p["error"] for p in req["pages"] if p["error"]],
                    }
                )
    return {"by_source": {k: dict(v) for k, v in sorted(by.items())}, "failed": failed}


def db_snapshot(factory: sessionmaker[Session]) -> dict[str, Any]:
    """Read-only snapshot of what the audit compares against: V4/V5 candidates and members,
    the security master's symbols and types, and the identity records' delistings."""
    with factory() as s:
        cands = s.execute(
            text(
                "select universe_code, formation_session::text, symbol, asset_id::text, "
                "last_close::text, "
                "median_dollar_volume::text, eligible, member from research_universe_candidates "
                "where universe_code in ('RESEARCH_US_EQUITY_V4','RESEARCH_ETF_CANDIDATES_V5') "
                "order by 1, 2, 3"
            )
        ).all()
        master = s.execute(
            text(
                "select distinct on (symbol) symbol, provider_status, security_type, type_basis "
                "from research_securities where provider = 'alpaca' order by symbol, revision desc"
            )
        ).all()
        ident = s.execute(
            text(
                "select distinct on (asset_id) asset_id::text, symbol, delisted_on::text, "
                "delisting_basis, security_type, type_basis from research_securities "
                "where provider = 'alpaca-asof' order by asset_id, created_at desc, revision desc"
            )
        ).all()
    return {
        "candidates": [list(r) for r in cands],
        "master": [list(r) for r in master],
        "identities": [list(r) for r in ident],
    }


def _snapshot(store: Store) -> dict[str, Any]:
    snap: dict[str, Any] = read_json(store.path("db/research_snapshot.json.gz"))
    return snap


def _asset_symbols(store: Store) -> set[str]:
    out: set[str] = set()
    for status in ("active", "inactive"):
        rel = f"provider/assets/assets_{status}.json.gz"
        if store.has(rel):
            out.update(a["symbol"] for a in read_json(store.path(rel)).get("body") or [])
    return out


def _thresholds(snap: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for code, f, _s, _a, _lc, mdv, _e, member in snap["candidates"]:
        if member and mdv is not None:
            key = "V4" if code.endswith("V4") else "V5"
            out[f][key] = min(out[f].get(key, float("inf")), float(mdv))
    return {k: dict(v) for k, v in out.items()}


def _match(snap: dict[str, Any], f: date, m: dict[str, Any]) -> list[str]:
    """V4/V5 candidates at ``f`` with the same last close and dollar volume (5%: the 2018
    window was formed with 2018-12-05 as a session)."""
    out = []
    for code, fs, sym, _a, lc, mdv, _e, member in snap["candidates"]:
        if fs != f.isoformat() or lc is None or not m["mdv"]:
            continue
        if abs(float(lc) - m["last_close"]) < 1e-6 and abs(float(mdv) / m["mdv"] - 1) < 0.05:
            out.append(f"{code[-2:]}:{sym}{'*' if member else ''}")
    return out


def eligible(m: dict[str, Any]) -> bool:
    """V4's eligibility: >= 50 traded bars, last close >= $5, median dollar volume >= $1M."""
    return m["traded_bars"] >= 50 and (m["last_close"] or 0) >= 5 and m["mdv"] >= 1_000_000


def typing_evidence(store: Store, formation: date, tickers: set[str]) -> dict[str, dict[str, Any]]:
    """Point-in-time listing evidence: the bracketing archived lists' rows and the SEC fund
    file of the formation's year, per ticker. A provisional type, never a decision."""
    lists = [
        x
        for x in symbol_lists(store)
        if any(b.startswith(formation.isoformat()) for b in x["brackets"])
    ]
    funds = fund_files(store)

    def year_file(y: int) -> str | None:
        return next(
            (
                k
                for k in funds
                if k.endswith(f"_{y}.csv.gz") or FUND_FILE_YEARS.get(k.rsplit("/", 1)[-1]) == y
            ),
            None,
        )

    y = formation.year
    used = {yy: year_file(yy) for yy in (y - 1, y, y + 1)}
    out: dict[str, dict[str, Any]] = {}
    for t in sorted(tickers):
        rows = [
            {
                "family": x["family"],
                "timestamp": x["timestamp"],
                "side": [
                    b.split(":")[1] for b in x["brackets"] if b.startswith(formation.isoformat())
                ],
                "name": r["name"],
                "exchange": r["exchange"],
                "etf": r["etf"],
            }
            for x in lists
            for r in x["rows"]
            if r["symbol"] == t
        ]
        member = {yy: (f is not None and t in funds[f]) for yy, f in used.items()}
        if used[y] is not None:
            in_fund_file = member[y]
            basis = f"SEC fund file {y}"
        elif used[y - 1] is not None and used[y + 1] is not None:
            in_fund_file = member[y - 1] and member[y + 1]
            basis = f"SEC fund files {y - 1} and {y + 1} (no {y} file)"
        else:
            in_fund_file, basis = False, "no SEC fund file around the formation"
        flags = {r["etf"] for r in rows if r["etf"] is not None}
        company_list = any(r["family"].startswith("companylist") for r in rows)
        # The directories' ETF flag marks funds only; ETNs are named as such.
        etp_name = any(ETP_NAME.search(r["name"] or "") for r in rows)
        if in_fund_file or True in flags or etp_name:
            provisional = "FUND_OR_ETP"
        elif flags == {False} or (company_list and not flags):
            provisional = "OPERATING_COMPANY"
        else:
            provisional = "UNRESOLVED"
        out[t] = {
            "lists": rows,
            "sec_fund_files": {str(k): v for k, v in used.items()},
            "fund_basis": basis,
            "in_sec_fund_file": in_fund_file,
            "provisional_type": provisional,
        }
    return out


def discovered(store: Store, formation: date) -> list[dict[str, Any]]:
    """Eligible tickers at ``formation`` that today's provider list or the research master
    lacks, or that no V4/V5 candidate represents (``reasons``; ``missing_from_universes``
    REDACTED_FOR_GITHUB"""
    snap = _snapshot(store)
    master = {r[0] for r in snap["master"]}
    assets = _asset_symbols(store)
    thr = _thresholds(snap).get(formation.isoformat(), {})
    out = []
    for t, m in formation_metrics(store, formation).items():
        if not eligible(m):
            continue
        matched = _match(snap, formation, m)
        reasons = [
            r
            for r, hit in (
                ("absent_from_provider_assets_today", t not in assets),
                ("absent_from_research_master", t not in master),
                ("no_v4_v5_candidate_match", not matched),
            )
            if hit
        ]
        if not reasons:
            continue
        out.append(
            {
                "ticker": t,
                **m,
                "reasons": reasons,
                "excluded_by_rule": t in EXCLUDED_BY_RULE,
                "missing_from_universes": not matched and t not in EXCLUDED_BY_RULE,
                "in_research_master": t in master,
                "matched_candidates": matched,
                "above_v4_bar": m["mdv"] >= thr.get("V4", float("inf")),
                "above_v5_bar": m["mdv"] >= thr.get("V5", float("inf")),
            }
        )
    return sorted(out, key=lambda r: -r["mdv"])


def derive(store: Store, persist: bool = True) -> dict[str, str]:
    """Every report, from raw evidence only; returns path -> content sha256."""
    reports: dict[str, Any] = {}
    snap = _snapshot(store)
    thr = _thresholds(snap)
    for f in FORMATIONS:
        key = f.isoformat()
        metrics = formation_metrics(store, f)
        found = discovered(store, f)
        tickers = {r["ticker"] for r in found}
        reports[f"derived/{key}/formation_metrics.json.gz"] = metrics
        reports[f"derived/{key}/discovery.json"] = {
            "formation": key,
            "window": [d.isoformat() for d in window(f)],
            "thresholds_from_v4_v5": thr.get(key, {}),
            "coverage": sweep_coverage(store, f),
            "tickers_with_bars": len(metrics),
            "tickers_traded": sum(1 for m in metrics.values() if m["traded_bars"]),
            "eligible": sum(1 for m in metrics.values() if eligible(m)),
            "eligible_flagged": len(found),
            "eligible_by_reason": dict(Counter(x for r in found for x in r["reasons"])),
            "missing_from_universes": sum(1 for r in found if r["missing_from_universes"]),
            "missing_above_v4_bar": [
                r["ticker"] for r in found if r["missing_from_universes"] and r["above_v4_bar"]
            ],
            "missing_above_v5_bar": [
                r["ticker"] for r in found if r["missing_from_universes"] and r["above_v5_bar"]
            ],
            "absent": found,
        }
        reports[f"derived/{key}/typing.json"] = typing_evidence(store, f, tickers)
    notices = [n for _rel, n in iter_notices(store)]
    reports["derived/removal_notices.json.gz"] = notices
    reports["derived/class_candidates.json"] = class_candidates(store)
    reports["derived/validation.json"] = validation(store, reports)
    reports["derived/coverage.json"] = coverage(store, reports)
    out: dict[str, str] = {}
    for rel, obj in sorted(reports.items()):
        data = canonical(obj)
        out[rel] = sha256(data)
        if persist and not store.has(rel):
            store.put(
                rel,
                gz(data) if rel.endswith(".gz") else data,
                {"kind": "derived", "from": "raw evidence only (offline)"},
            )
    if persist:
        store.save()
    return out


# ---------------------------------------------------------------------- validation
KNOWN_STOCKS_2016 = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
KNOWN_FUNDS_2016 = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
KNOWN_MISTYPED = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
TRANSFERS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
ISHARES_2017 = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def validation(*_args: object, **_kwargs: object) -> object:
    raise NotImplementedError("REDACTED_FOR_GITHUB")


def coverage(store: Store, reports: dict[str, Any]) -> dict[str, Any]:
    """What is complete, what is not, and every failure."""
    gaps: list[str] = []
    per: dict[str, Any] = {}
    for f in FORMATIONS:
        key = f.isoformat()
        cov = reports[f"derived/{key}/discovery.json"]["coverage"]
        src = cov["by_source"]
        expected = {f"len{n}": 26**n for n in (1, 2, 3, 4)}
        complete = {
            s: src.get(s, {}).get("tickers") == n and not src.get(s, {}).get("failed_requests")
            for s, n in expected.items()
        }
        per[key] = {
            "sources": src,
            "complete_letter_sweeps": complete,
            "failed_requests": cov["failed"],
        }
        for s, ok in complete.items():
            if not ok:
                gaps.append(f"{key}: {s} sweep incomplete or with failed requests")
    archives: dict[str, list[dict[str, Any]]] = {}
    for lst in symbol_lists(store):
        archives.setdefault(lst["family"], []).append(
            {
                "timestamp": lst["timestamp"],
                "brackets": lst["brackets"],
                "digest_verified": lst["digest_verified"],
                "rows": len(lst["rows"]),
            }
        )
    for family in ARCHIVE_FAMILIES:
        for f in FORMATIONS:
            sides = {
                b.split(":")[1]
                for x in archives.get(family, [])
                for b in x["brackets"]
                if b.startswith(f.isoformat())
            }
            for side in ("before", "after"):
                if side not in sides:
                    gaps.append(f"{family}: no archived capture {side} {f.isoformat()}")
    failed_archive = [
        k
        for k, e in store.entries.items()
        if k.startswith("archive/") and k.endswith(".raw") and not e.get("digest_verified")
    ]
    sec_fail = {
        k: e.get("failed") or (0 if e.get("http_status") in (None, 200) else 1)
        for k, e in store.entries.items()
        if k.startswith("sec/")
    }
    return {
        "formations": per,
        "archives": archives,
        "archive_captures_failed_or_unverified": failed_archive,
        "sec_failures": {k: v for k, v in sec_fail.items() if v},
        "gaps": gaps,
        "not_exhaustive": [
            "5-letter and dotted share-class tickers: only the candidates of the archived "
            "lists, SEC fund and ticker files and today's provider list were requested",
            "tickers with other characters (preferreds, units, warrants) are not requested",
            "OTC securities are not part of the universes and are not swept",
            "a ticker that traded only under a different symbol notation at the provider is "
            "not found",
            "SEC company submissions: equity-removal issuers 2016-2019 and the known cases only",
        ],
    }


# ---------------------------------------------------------------------- verification
def verify(store: Store) -> dict[str, Any]:
    """Recompute every artifact's hash and every derived report from the raw evidence."""
    bad = []
    for rel, e in sorted(store.entries.items()):
        p = store.path(rel)
        if not p.exists():
            bad.append({"path": rel, "problem": "missing"})
            continue
        data = p.read_bytes()
        if sha256(data) != e["sha256"]:
            bad.append({"path": rel, "problem": "sha256 differs"})
    recomputed = derive(store, persist=False)
    derived_mismatch = [
        rel
        for rel, h in recomputed.items()
        if rel in store.entries and store.entries[rel]["content_sha256"] != h
    ]
    return {
        "artifacts": len(store.entries),
        "hash_failures": bad,
        "derived_reports": len(recomputed),
        "derived_mismatches": derived_mismatch,
        "ok": not bad and not derived_mismatch,
    }


__all__ = [
    "FORMATIONS",
    "ROOT",
    "Store",
    "capture_archive",
    "capture_assets",
    "capture_dividends",
    "capture_history",
    "capture_sec",
    "class_candidates",
    "db_snapshot",
    "derive",
    "discovered",
    "formation_metrics",
    "gz",
    "letter_tickers",
    "parse_notice",
    "parse_symbol_list",
    "sweep",
    "verify",
    "wayback_sha1",
]
