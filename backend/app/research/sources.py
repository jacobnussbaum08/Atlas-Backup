"""External sources of the research foundation (D-239). GET-only; credentials never printed.

* Alpaca Trading API (paper host, reference endpoints only): the provider's asset list,
  active and inactive. Its inactive list is incomplete for older delistings: the
  validation measures that gap (``validation.KNOWN_DELISTINGS``).
* Alpaca Market Data: daily bars (SIP) for any symbol, including delisted ones.
* SEC EDGAR: ``company_tickers.json`` (operating companies, current tickers -> CIK),
  ``company_tickers_mf.json`` (funds and ETFs, current), ``submissions/CIK*.json`` (filing
  lists) and filing headers (the SIC code as filed). Requests identify a generic research
  client and stay under SEC's 10 requests per second.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.data_engine.providers.alpaca.adapter import DATA_API_BASE_URL, AlpacaMarketDataProvider
from app.data_engine.providers.alpaca.config import get_alpaca_settings
from app.data_engine.providers.alpaca.reference import PAPER_TRADING_API_BASE_URL

EASTERN = ZoneInfo("America/New_York")
SEC_HEADERS = {"User-Agent": "AtlasResearch/1.0 (research client)"}
SEC_MIN_INTERVAL = 0.12  # seconds between SEC requests (< 10 per second)


def alpaca_client() -> httpx.Client:
    headers = AlpacaMarketDataProvider(get_alpaca_settings()).authentication_headers()
    return httpx.Client(headers=headers, timeout=120)


def alpaca_assets(client: httpx.Client) -> list[dict[str, Any]]:
    """Every US equity asset Alpaca lists, active and inactive."""
    out: list[dict[str, Any]] = []
    for status in ("active", "inactive"):
        r = client.get(
            f"{PAPER_TRADING_API_BASE_URL}/v2/assets",
            params={"status": status, "asset_class": "us_equity"},
        )
        r.raise_for_status()
        out.extend(r.json())
    return out


def get(client: httpx.Client, url: str, params: dict[str, Any]) -> httpx.Response:
    """GET with back-off on the provider's rate limit (429); other errors raise."""
    for attempt in range(8):
        r = client.get(url, params=params)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        time.sleep(5 + 10 * attempt)
    r.raise_for_status()
    return r


def daily_bars(
    client: httpx.Client,
    symbols: Sequence[str],
    start: date,
    end: date,
    asof: date | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """(symbol, bar) for every SIP daily bar (RAW) of ``symbols`` in [start, end].

    ``asof`` pins the provider's symbol mapping: each ticker means the security that held it
    on that date (with that security's history under later tickers). Without it the provider
    maps to the ticker's *current* holder, falling back to the raw ticker when the holder has
    no bars in the range, so the identity depends on the requested range."""
    token: str | None = None
    seen: set[str] = set()
    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": f"{start.isoformat()}T00:00:00Z",
            "end": f"{end.isoformat()}T23:59:59Z",
            "feed": "sip",
            "adjustment": "raw",
            "limit": "10000",
            "sort": "asc",
        }
        if asof is not None:
            params["asof"] = asof.isoformat()
        if token:
            params["page_token"] = token
        for attempt in range(5):
            r = client.get(f"{DATA_API_BASE_URL}/v2/stocks/bars", params=params)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            break
        body = r.json()
        for sym, rows in (body.get("bars") or {}).items():
            for row in rows:
                yield sym, row
        token = body.get("next_page_token")
        if not token:
            return
        if token in seen:
            raise RuntimeError("the provider repeated a page token")
        seen.add(token)


class Sec:
    def __init__(self) -> None:
        self.client = httpx.Client(headers=SEC_HEADERS, timeout=60)
        self._last = 0.0

    def get(self, url: str) -> httpx.Response:
        wait = SEC_MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            self._last = time.monotonic()
            r = self.client.get(url)
            if r.status_code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            return r
        return r

    def company_tickers(self) -> dict[str, str]:
        """Current ticker -> zero-padded CIK of operating companies."""
        r = self.get("https://www.sec.gov/files/company_tickers.json")
        r.raise_for_status()
        return {v["ticker"].upper(): f"{int(v['cik_str']):010d}" for v in r.json().values()}

    def fund_tickers(self) -> set[str]:
        """Current tickers of registered funds (mutual funds and ETFs)."""
        r = self.get("https://www.sec.gov/files/company_tickers_mf.json")
        r.raise_for_status()
        body = r.json()
        fields = body["fields"]
        i = fields.index("symbol")
        return {str(row[i]).upper() for row in body["data"] if row[i]}

    def filings(self, cik: str) -> list[dict[str, Any]]:
        """10-K and 10-Q filings (form, filing date, accession) of a company."""
        r = self.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        if r.status_code != 200:
            return []
        body = r.json()
        blocks = [body.get("filings", {}).get("recent", {})]
        for f in body.get("filings", {}).get("files", []):
            rr = self.get(f"https://data.sec.gov/submissions/{f['name']}")
            if rr.status_code == 200:
                blocks.append(rr.json())
        out = []
        for b in blocks:
            for form, filed, acc in zip(
                b.get("form", []),
                b.get("filingDate", []),
                b.get("accessionNumber", []),
                strict=False,
            ):
                if form in ("10-K", "10-Q", "10-K405", "20-F", "40-F"):
                    out.append({"form": form, "filed": date.fromisoformat(filed), "acc": acc})
        return sorted(out, key=lambda x: x["filed"])

    def sic_as_filed(self, cik: str, accession: str) -> int | None:
        """The SIC code in a filing's header (the classification at that filing)."""
        acc = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}-index-headers.html"
        r = self.get(url)
        if r.status_code != 200:
            return None
        text = r.text
        key = "STANDARD INDUSTRIAL CLASSIFICATION:"
        i = text.find(key)
        if i < 0:
            return None
        segment = text[i : i + 200]
        lb, rb = segment.find("["), segment.find("]")
        if lb < 0 or rb < lb:
            return None
        code = segment[lb + 1 : rb].strip()
        return int(code) if code.isdigit() else None


def now_utc() -> datetime:

    return datetime.now(UTC)


__all__ = [
    "DATA_API_BASE_URL",
    "EASTERN",
    "Sec",
    "alpaca_assets",
    "alpaca_client",
    "daily_bars",
    "get",
    "now_utc",
]
