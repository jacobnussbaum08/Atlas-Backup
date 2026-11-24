"""Cash dividends for the backtest (D-237): retrieval, storage and point-in-time lookup.

Retrieval (``python -m app.backtesting.dividends --symbols ... --start ... --end ...``)
sends GET-only requests to Alpaca's corporate-actions endpoint
(``/v1/corporate-actions``, type ``cash_dividend``) and stores every returned dividend in
``cash_dividends`` (append-only; a changed value for the same provider id is a new
revision) and one ``cash_dividend_retrievals`` row per symbol and requested range (the
coverage record). Credentials are never printed or stored.

What the provider gives: ex-date, rate per share, process date, special and foreign
flags. It gives **no declaration date**, so a dividend's original announcement time is
unknown. Dividends are therefore never a decision input: the replay uses them only to
account for cash at the ex-date (``cash-at-ex-date-v1``), which no earlier decision can
see. Foreign (ADR) rates are the provider's gross amounts; withholding taxes and ADR fees
are not modelled.

Point-in-time and reproducibility: a run uses the rows *retrieved* by its dataset
knowledge time, newest revision first, so a later retrieval never changes a stored run.
Coverage: an asset with no successful retrieval covering the run's period has unknown
dividends; the run records it as ``NOT_RETRIEVED`` and credits nothing for it (labelled),
it does not fail.

No double counting: positions are valued at RAW (unadjusted) closes, which fall by the
dividend on the ex-date, and features use the split-adjusted (not dividend-adjusted)
series. The dividend cash offsets the price drop exactly once.
"""

from __future__ import annotations

import argparse
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.backtesting.config import DIVIDEND_POLICY, DIVIDEND_POLICY_V1
from app.backtesting.corrections import CREDIT, VOID, DividendCorrection
from app.database.models import Asset, CashDividendRecord, CashDividendRetrievalRecord

PROVIDER = "alpaca"
PATH = "/v1/corporate-actions"
OK, FAILED = "OK", "FAILED"
RETRIEVED, NOT_RETRIEVED = "RETRIEVED", "NOT_RETRIEVED"
POLICY_V1, POLICY_V2 = DIVIDEND_POLICY_V1, DIVIDEND_POLICY
PROVIDER_ROW_STATS = ("provider_duplicates_collapsed", "shared_symbol_joins")
# X3.1 (D-244): the provider's ``start``/``end`` filter the *processing* date (for 2019 on,
# usually the payable date; for older rows the ex-date), not the ex-date. A request ending on
# 2019-12-31 silently misses ex-dates of late December paid in January. Retrievals are now
# asked for in ex-date terms: the provider is queried over a processing-date window that
# opens PROCESS_LEAD before the first ex-date (one stored row is processed 13 days before its
# ex-date) and closes PROCESS_LAG after the last (the longest gap stored is 178 days), and
# the retrieval record's range is the ex-date range, marked ``details.basis``.
EX_DATE_BASIS = "ex_date"
PROCESS_LEAD = timedelta(days=31)
PROCESS_LAG = timedelta(days=366)


@dataclass(frozen=True)
class Dividend:
    asset_id: uuid.UUID
    symbol: str
    ex_date: date
    rate: Decimal
    special: bool
    foreign: bool
    provider_action_id: str
    revision: int
    # D-243: the correction-set record that credits it (None: the asset's own provider row).
    correction: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "symbol": self.symbol,
            "ex_date": self.ex_date.isoformat(),
            "rate": str(self.rate),
            "special": self.special,
            "foreign": self.foreign,
            "provider_action_id": self.provider_action_id,
            "revision": self.revision,
        }
        if self.correction is not None:
            out["correction"] = self.correction
        return out


class PointInTimeDividends:
    """The cash dividends of a run's assets known by its knowledge time."""

    def __init__(
        self,
        by_day: dict[date, list[Dividend]],
        coverage: dict[uuid.UUID, str],
        corrections: Counter[str] | None = None,
    ) -> None:
        self._by_day = by_day
        self.coverage = coverage
        # D-243: what a correction set changed (empty: no set applied).
        self.corrections: Counter[str] = corrections if corrections is not None else Counter()

    @classmethod
    def load(
        cls,
        session: Session,
        assets: dict[uuid.UUID, str],
        *,
        start: date,
        end: date,
        known_at: datetime,
        corrections: Sequence[DividendCorrection] = (),
        policy: str = POLICY_V1,
    ) -> PointInTimeDividends:
        """Provider dividends of the assets' own symbols (covered histories only), then the
        correction set's records (D-243): VOIDs withhold a provider dividend that belongs to
        another security; CREDITs add a verified dividend filed under a former ticker, unless
        the asset already has it (same provider action, or same ex-date and rate), so nothing
        is counted twice.

        ``policy`` ``cash-at-ex-date-v2`` (X3.1, D-244): only retrievals whose range is in
        ex-date terms (:data:`EX_DATE_BASIS`) cover a period; provider rows with the same
        symbol, ex-date and rate under different action ids are one dividend; and a symbol
        two run assets share (point-in-time identities of one ticker) is joined to each of
        them, for the correction set to void where the ticker meant the other security.
        ``cash-at-ex-date-v1`` is the D-237 behaviour, kept for stored runs."""
        if policy not in (POLICY_V1, POLICY_V2):
            raise ValueError(f"unknown dividend policy {policy!r}")
        v2 = policy == POLICY_V2
        by_symbol: dict[str, list[uuid.UUID]] = defaultdict(list)
        for a, s in sorted(assets.items(), key=lambda kv: str(kv[0])):
            by_symbol[s].append(a)
        # v1: one asset per symbol (the last one, as D-237 did).
        symbols = {s: a for a, s in assets.items()}
        r = CashDividendRetrievalRecord
        query = select(r.symbol).where(
            r.provider == PROVIDER,
            r.status == OK,
            r.symbol.in_(list(symbols)),
            r.range_start <= start,
            r.range_end >= end,
            r.retrieved_at <= known_at,
        )
        if v2:
            query = query.where(r.details["basis"].astext == EX_DATE_BASIS)
        covered = set(session.scalars(query))
        d = CashDividendRecord
        newest: dict[str, CashDividendRecord] = {}
        for row in session.scalars(
            select(d)
            .where(
                d.provider == PROVIDER,
                d.symbol.in_(list(symbols)),
                d.ex_date >= start,
                d.ex_date <= end,
                d.retrieved_at <= known_at,
            )
            .order_by(d.provider_action_id, d.revision)
        ):
            newest[row.provider_action_id] = row  # the newest revision known wins
        by_day: dict[date, list[Dividend]] = defaultdict(list)
        stats: Counter[str] = Counter()
        seen_terms: set[tuple[str, date, Decimal]] = set()
        # v2 keeps, of exact duplicates, the non-special row first, then the lowest id.
        order = (
            (lambda x: (x.ex_date, x.symbol, Decimal(x.rate), x.special, x.provider_action_id))
            if v2
            else (lambda x: (x.ex_date, x.symbol, x.provider_action_id))
        )
        for row in sorted(newest.values(), key=order):
            if row.symbol not in covered:
                continue  # only complete (covered) histories are used
            if v2:
                terms = (row.symbol, row.ex_date, Decimal(row.rate))
                if terms in seen_terms:
                    stats["provider_duplicates_collapsed"] += 1
                    continue
                seen_terms.add(terms)
                if len(by_symbol[row.symbol]) > 1:
                    stats["shared_symbol_joins"] += len(by_symbol[row.symbol])
            for asset_id in by_symbol[row.symbol] if v2 else [symbols[row.symbol]]:
                by_day[row.ex_date].append(
                    Dividend(
                        asset_id=asset_id,
                        symbol=row.symbol,
                        ex_date=row.ex_date,
                        rate=Decimal(row.rate),
                        special=row.special,
                        foreign=row.foreign,
                        provider_action_id=row.provider_action_id,
                        revision=row.revision,
                    )
                )
        if v2:
            coverage = {
                a: (RETRIEVED if s in covered else NOT_RETRIEVED) for a, s in assets.items()
            }
        else:
            coverage = {
                a: (RETRIEVED if s in covered else NOT_RETRIEVED) for s, a in symbols.items()
            }
        if v2:
            for day in by_day:  # one deterministic order per session (as v1's)
                by_day[day].sort(key=lambda d: (d.symbol, d.provider_action_id, str(d.asset_id)))
        if corrections:
            _apply(by_day, corrections, set(assets), start, end, stats)
        return cls(dict(by_day), coverage, stats)

    def corrected_assets(self) -> set[uuid.UUID]:
        """Assets receiving at least one dividend from a correction set (D-243)."""
        return {d.asset_id for ds in self._by_day.values() for d in ds if d.correction}

    def on(self, day: date) -> list[Dividend]:
        return self._by_day.get(day, [])

    def count(self) -> int:
        return sum(len(v) for v in self._by_day.values())


def _apply(
    by_day: dict[date, list[Dividend]],
    corrections: Sequence[DividendCorrection],
    assets: set[uuid.UUID],
    start: date,
    end: date,
    stats: Counter[str],
) -> None:
    voids = {(c.asset_id, c.provider_action_id) for c in corrections if c.action == VOID}
    for day in list(by_day):
        kept = [d for d in by_day[day] if (d.asset_id, d.provider_action_id) not in voids]
        stats["voided"] += len(by_day[day]) - len(kept)
        by_day[day] = kept
    by_action = {(d.asset_id, d.provider_action_id) for ds in by_day.values() for d in ds}
    by_terms = {(d.asset_id, d.ex_date, d.rate) for ds in by_day.values() for d in ds}
    for c in sorted(corrections, key=lambda c: (c.ex_date, str(c.asset_id), c.code)):
        if c.action != CREDIT or c.asset_id not in assets or not start <= c.ex_date <= end:
            continue
        if (c.asset_id, c.provider_action_id) in by_action or (
            c.asset_id,
            c.ex_date,
            c.rate,
        ) in by_terms:
            stats["duplicates_skipped"] += 1
            continue
        by_day.setdefault(c.ex_date, []).append(
            Dividend(
                asset_id=c.asset_id,
                symbol=c.provider_ticker,
                ex_date=c.ex_date,
                rate=c.rate,
                special=c.special,
                foreign=c.foreign,
                provider_action_id=c.provider_action_id,
                revision=0,
                correction=c.code,
            )
        )
        by_action.add((c.asset_id, c.provider_action_id))
        by_terms.add((c.asset_id, c.ex_date, c.rate))
        stats["credited_events"] += 1
    for day in list(by_day):
        by_day[day] = sorted(
            by_day[day], key=lambda d: (d.symbol, d.provider_action_id, str(d.asset_id))
        )
        if not by_day[day]:
            del by_day[day]


# ---------------------------------------------------------------------- retrieval
def _decimal(value: Any) -> Decimal:
    # The provider sends JSON numbers; str() keeps the printed digits (no binary noise).
    return Decimal(str(value))


def parse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize provider rows (malformed ones are skipped and counted by the caller)."""
    out = []
    for row in rows:
        try:
            rate = _decimal(row["rate"])
            ex = date.fromisoformat(row["ex_date"])
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        if rate <= 0:
            continue
        out.append(
            {
                "provider_action_id": str(row.get("id") or f"{row.get('symbol')}:{ex}:{rate}"),
                "symbol": str(row["symbol"]).upper(),
                "ex_date": ex,
                "rate": rate,
                "special": bool(row.get("special", False)),
                "foreign": bool(row.get("foreign", False)),
                "process_date": date.fromisoformat(row["process_date"])
                if row.get("process_date")
                else None,
                "payable_date": date.fromisoformat(row["payable_date"])
                if row.get("payable_date")
                else None,
                "cusip": (row.get("cusip") or None),
                "raw": row,
            }
        )
    return out


def fetch_rows(
    client: httpx.Client,
    symbols: Sequence[str],
    start: date,
    end: date,
    pages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Provider rows whose *processing* date is in [start, end] (the provider's filter).
    ``pages`` receives every response body as returned (evidence)."""
    rows: list[dict[str, Any]] = []
    token: str | None = None
    seen: set[str] = set()
    while True:
        params = {
            "symbols": ",".join(symbols),
            "types": "cash_dividend",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": "1000",
        }
        if token:
            params["page_token"] = token
        response = client.get(PATH, params=params)
        response.raise_for_status()
        body = response.json()
        if pages is not None:
            pages.append({"params": {k: v for k, v in params.items()}, "body": body})
        rows.extend((body.get("corporate_actions") or {}).get("cash_dividends") or [])
        token = body.get("next_page_token")
        if not token:
            return rows
        if token in seen:
            raise RuntimeError("the provider repeated a page token")
        seen.add(token)


def store(
    session: Session,
    symbols: Sequence[str],
    start: date,
    end: date,
    rows: list[dict[str, Any]],
    *,
    retrieved_at: datetime,
    status: str = OK,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Append new dividends and revisions; one retrieval row per symbol. ``details`` is
    recorded on each retrieval row (X3.1: ``basis`` and the processing-date window)."""
    assets = {
        a.symbol: a.id
        for a in session.scalars(select(Asset).where(Asset.symbol.in_(list(symbols))))
    }
    counts = {"new": 0, "revised": 0, "unchanged": 0}
    per_symbol: dict[str, int] = defaultdict(int)
    for row in rows:
        per_symbol[row["symbol"]] += 1
        existing = session.scalars(
            select(CashDividendRecord)
            .where(
                CashDividendRecord.provider == PROVIDER,
                CashDividendRecord.provider_action_id == row["provider_action_id"],
            )
            .order_by(CashDividendRecord.revision.desc())
            .limit(1)
        ).first()
        same = existing is not None and (
            existing.ex_date == row["ex_date"]
            and Decimal(existing.rate) == row["rate"]
            and existing.special == row["special"]
            and existing.foreign == row["foreign"]
            and existing.symbol == row["symbol"]
        )
        if same:
            counts["unchanged"] += 1
            continue
        revision = 0 if existing is None else existing.revision + 1
        counts["new" if existing is None else "revised"] += 1
        session.add(
            CashDividendRecord(
                provider=PROVIDER,
                provider_action_id=row["provider_action_id"],
                revision=revision,
                asset_id=assets.get(row["symbol"]),
                symbol=row["symbol"],
                ex_date=row["ex_date"],
                rate=row["rate"],
                special=row["special"],
                foreign=row["foreign"],
                process_date=row["process_date"],
                payable_date=row["payable_date"],
                cusip=row["cusip"],
                retrieved_at=retrieved_at,
                raw=row["raw"],
            )
        )
    for symbol in symbols:
        session.add(
            CashDividendRetrievalRecord(
                provider=PROVIDER,
                symbol=symbol,
                range_start=start,
                range_end=end,
                dividends=per_symbol.get(symbol, 0),
                status=status,
                retrieved_at=retrieved_at,
                details={**(details or {}), **({} if error is None else {"error": error[:500]})},
            )
        )
    return counts


def process_window(start: date, end: date) -> tuple[date, date]:
    """The processing-date window that holds every dividend going ex in [start, end]."""
    return start - PROCESS_LEAD, end + PROCESS_LAG


def retrieve(
    factory: sessionmaker[Session],
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Every dividend of ``symbols`` going ex in [start, end] (X3.1: queried over
    :func:`process_window`, recorded with ``basis`` ``ex_date``)."""
    from app.data_engine.providers.alpaca.adapter import (
        DATA_API_BASE_URL,
        AlpacaMarketDataProvider,
    )
    from app.data_engine.providers.alpaca.config import get_alpaca_settings

    headers = AlpacaMarketDataProvider(get_alpaca_settings()).authentication_headers()
    symbols = sorted({s.strip().upper() for s in symbols if s.strip()})
    with httpx.Client(base_url=DATA_API_BASE_URL, headers=headers, timeout=60) as client:
        try:
            rows = parse(fetch_rows(client, symbols, *process_window(start, end), pages=pages))
            status, error = OK, None
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            rows, status, error = [], FAILED, f"{type(exc).__name__}"
    with factory() as session, session.begin():
        lo, hi = process_window(start, end)
        counts = store(
            session, symbols, start, end, rows, retrieved_at=datetime.now(UTC), status=status,
            error=error,
            details={"basis": EX_DATE_BASIS, "process_window": [lo.isoformat(), hi.isoformat()]},
        )  # fmt: skip
    counts["rows"] = len(rows)
    counts["status_ok"] = int(status == OK)
    return counts


def main(argv: Sequence[str] | None = None) -> None:
    from app.core.config import get_settings
    from app.database.sessions import build_engine, build_session_factory

    parser = argparse.ArgumentParser(
        prog="python -m app.backtesting.dividends",
        description="Retrieve historical cash dividends (GET-only provider requests).",
    )
    parser.add_argument("--symbols", required=True, help="comma-separated")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    factory = build_session_factory(build_engine(get_settings()))
    print(retrieve(factory, args.symbols.split(","), args.start, args.end), flush=True)


if __name__ == "__main__":
    main()


__all__ = [
    "DIVIDEND_COVERAGE",
    "EX_DATE_BASIS",
    "NOT_RETRIEVED",
    "POLICY_V1",
    "POLICY_V2",
    "PROVIDER_ROW_STATS",
    "RETRIEVED",
    "Dividend",
    "PointInTimeDividends",
    "fetch_rows",
    "parse",
    "process_window",
    "retrieve",
    "store",
]
DIVIDEND_COVERAGE = (RETRIEVED, NOT_RETRIEVED)
