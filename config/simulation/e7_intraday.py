"""E7 intraday check: the daily-bar fill conventions against 1-minute SIP bars (read only).

For every fill of the standalone control runs (``y20*--live-sip-measured--<strategy>``),
the session's 1-minute SIP bars (RAW, regular session, fetched from Alpaca with GET-only
requests and cached locally; never written to the database) give an intraday reference
price for what a resting order would plausibly have received:

* limit (entry, take-profit) submitted before the session: marketable at the first
  minute's open when that open is better than the limit (plus the opening half spread and
  slippage), else the limit, from the first minute that traded strictly through it;
* stop: the first minute that reached it; that minute's open when it opened beyond the
  stop, else the stop (plus the half spread and slippage). A protective stop counts only
  from the minute its entry filled;
* market order submitted before the session: the first minute's open plus costs;
* a stop whose take-profit was reached in an earlier minute is resolved as the take-profit.

The same costs as the corrected model are applied, so the comparison isolates the timing
and price conventions. Minute bars are 1-minute OHLC, not trades: the reference is itself
an approximation (within one minute). Writes runs/e7_intraday.json.

    python config/simulation/e7_intraday.py [--cache DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from analyze import MANIFEST
from diagnose import run_id
from e7_audit import (
    ASSUMED_HALF,
    BPS,
    BUYS,
    SLIP,
    WINDOWS,
    adverse,
    load_bars,
    load_spreads,
)
from sqlalchemy import select

from app.backtesting.data import sessions_between
from app.core.config import get_settings
from app.database.models import (
    BacktestFillRecord,
    BacktestOrderRecord,
    BacktestTradeRecord,
)
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "e7_intraday.json"
STRATEGIES = (
    "breakout",
    "event_driven",
    "mean_reversion",
    "momentum",
    "trend_following",
    "volatility",
)

Minute = tuple[Decimal, Decimal, Decimal, Decimal]  # open, high, low, close


async def fetch_session(provider: Any, day: Any, symbols: list[str]) -> dict[str, list[list[str]]]:
    from app.data_engine.enums import PriceAdjustment
    from app.data_engine.providers.base import HistoricalBarsRequest
    from app.data_engine.timeframes import Timeframe

    request = HistoricalBarsRequest(
        provider_symbols=tuple(sorted(symbols)),
        timeframe=Timeframe.parse("1Min"),
        start=day.regular_open,
        end=day.regular_close - timedelta(minutes=1),
        adjustment=PriceAdjustment.RAW,
        page_size=10000,
    )
    out: dict[str, list[list[str]]] = {s: [] for s in symbols}
    async for page in provider.fetch_bars(request):
        for b in page.bars:
            if b.provider_symbol in out and day.regular_open <= b.event_time < day.regular_close:
                out[b.provider_symbol].append(
                    [b.event_time.isoformat(), str(b.open), str(b.high), str(b.low), str(b.close)]
                )
    return out


async def fetch_all(needed: dict[date, set[str]], cache: Path) -> None:
    from app.data_engine.providers.alpaca.adapter import AlpacaMarketDataProvider
    from app.data_engine.providers.alpaca.config import AlpacaFeed, get_alpaca_settings

    settings = get_alpaca_settings().model_copy(update={"data_feed": AlpacaFeed("sip")})
    provider = AlpacaMarketDataProvider(settings)
    days = {d.day: d for d in sessions_between(min(needed), max(needed))}
    todo = []
    for day, symbols in sorted(needed.items()):
        path = cache / f"{day.isoformat()}.json.gz"
        have: dict[str, Any] = {}
        if path.exists():
            have = json.loads(gzip.decompress(path.read_bytes()))
        missing = sorted(symbols - set(have))
        if missing:
            todo.append((day, missing, have, path))
    print(f"{len(todo)} session(s) to fetch", flush=True)
    sem = asyncio.Semaphore(3)

    async def one(item: tuple[date, list[str], dict[str, Any], Path]) -> None:
        day, missing, have, path = item
        async with sem:
            got = await fetch_session(provider, days[day], missing)
        have.update(got)
        path.write_bytes(gzip.compress(json.dumps(have).encode()))

    for i in range(0, len(todo), 30):
        await asyncio.gather(*(one(t) for t in todo[i : i + 30]))
        print(f"fetched {min(i + 30, len(todo))}/{len(todo)}", flush=True)


def minutes(cache: Path, day: date, symbol: str) -> list[Minute]:
    path = cache / f"{day.isoformat()}.json.gz"
    rows = json.loads(gzip.decompress(path.read_bytes())).get(symbol, [])
    return [(Decimal(o), Decimal(h), Decimal(lo), Decimal(c)) for _t, o, h, lo, c in rows]


def moved(base: Decimal, half: Decimal, buys: bool) -> Decimal:
    adj = (half + SLIP) / BPS
    return adverse(base * (1 + adj if buys else 1 - adj), buys)


def limit_ref(
    m: list[Minute], limit: Decimal, buys: bool, oh: Decimal
) -> tuple[Decimal, int] | None:
    if not m:
        return None
    first_open = m[0][0]
    if (first_open < limit) if buys else (first_open > limit):
        p = moved(first_open, oh, buys)
        return (min(p, limit) if buys else max(p, limit)), 0
    for i, (_o, h, lo, _c) in enumerate(m):
        if (lo < limit) if buys else (h > limit):
            return limit, i
    return None


def stop_ref(
    m: list[Minute], stop: Decimal, buys: bool, oh: Decimal, ch: Decimal, start: int
) -> tuple[Decimal, int] | None:
    for i in range(start, len(m)):
        o, h, lo, _c = m[i]
        if (h >= stop) if buys else (lo <= stop):
            beyond = (o >= stop) if buys else (o <= stop)
            if beyond:
                return moved(o, oh if i == 0 else ch, buys), i
            return moved(stop, ch, buys), i
    return None


def corrected_price(role: str, bar: dict, price: Decimal, o: Any, buys: bool, oh: Decimal,
                    ch: Decimal, exit_first: bool) -> Decimal:  # fmt: skip
    """The corrected daily convention, as in e7_audit.py."""

    def at_open() -> Decimal:
        return min(max(moved(bar["o"], oh, buys), bar["l"]), bar["h"])

    limit, stop = o.details.get("limit_price"), o.details.get("stop_price")
    if role in ("ENTRY", "TAKE_PROFIT") and limit is not None:
        lim = Decimal(limit)
        if (bar["o"] < lim) if buys else (bar["o"] > lim):
            p = at_open()
            return min(p, lim) if buys else max(p, lim)
        return price
    if role == "STOP" and stop is not None:
        s = Decimal(stop)
        if ((bar["o"] >= s) if buys else (bar["o"] <= s)) or exit_first:
            return at_open()
        return min(max(moved(s, ch, buys), bar["l"]), bar["h"])
    if role == "EXIT":
        return at_open()
    return price


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", default=os.environ.get("E7_MINUTE_CACHE", "minute_cache"))
    # Whose fills to check: the control (daily-v1) or e.g. v2-default (engine 8A.2).
    parser.add_argument("--variant", default="live-sip-measured")
    # Stage A (E2/E3): other windows and families, e.g. --windows a2020h2,a2021h1,a2021h2.
    parser.add_argument("--windows", default=",".join(WINDOWS))
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    # Appended to the output name, so a later check never overwrites an earlier artifact.
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text())
    factory = build_session_factory(build_engine(get_settings()))
    with factory() as session:
        bars = load_bars(session)
        open_half = load_spreads(session, "spread-sample-open-v1")
        close_half = load_spreads(session, "spread-sample-v1")
        runs = []
        for window in args.windows.split(","):
            for strategy in args.strategies.split(","):
                rid = run_id(session, manifest, f"{window}--{args.variant}--{strategy}")
                orders = {
                    o.seq: o
                    for o in session.scalars(
                        select(BacktestOrderRecord).where(BacktestOrderRecord.run_id == rid)
                    )
                }
                trades = {
                    t.seq: t
                    for t in session.scalars(
                        select(BacktestTradeRecord).where(BacktestTradeRecord.run_id == rid)
                    )
                }
                fills = list(
                    session.scalars(
                        select(BacktestFillRecord)
                        .where(BacktestFillRecord.run_id == rid)
                        .order_by(BacktestFillRecord.seq)
                    )
                )
                runs.append((strategy, orders, trades, fills))
    needed: dict[date, set[str]] = defaultdict(set)
    for _s, _o, _t, fills in runs:
        for f in fills:
            needed[f.session].add(f.symbol)
    asyncio.run(fetch_all(needed, cache))

    cats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "fills": 0,
            "baseline_err": Decimal(0),
            "corrected_err": Decimal(0),
            "baseline_abs_bps": Decimal(0),
            "corrected_abs_bps": Decimal(0),
        }
    )
    notes: dict[str, int] = defaultdict(int)
    by_strategy: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"baseline_err": Decimal(0), "corrected_err": Decimal(0)}
    )
    for strategy, orders, _trades, fills in runs:
        entry_minute: dict[int, tuple[date, int]] = {}
        by_trade: dict[int, list[Any]] = defaultdict(list)
        for o in orders.values():
            if o.trade_seq:
                by_trade[o.trade_seq].append(o)
        for f in fills:
            o = orders[f.order_seq]
            tseq = f.trade_seq or o.trade_seq
            bar = bars.get((f.asset_id, f.session))
            m = minutes(cache, f.session, f.symbol)
            if bar is None or not m:
                notes["no_minute_or_daily_bar"] += 1
                continue
            buys = f.side in BUYS
            oh = open_half.get((f.asset_id, f.session), ASSUMED_HALF)
            ch = close_half.get((f.asset_id, f.session), ASSUMED_HALF)
            price, q = Decimal(f.price), Decimal(f.quantity)
            ref: tuple[Decimal, int] | None = None
            cat = f.role
            exit_first = False
            if f.role in ("ENTRY", "TAKE_PROFIT"):
                limit = o.details.get("limit_price")
                if limit is None:
                    continue
                ref = limit_ref(m, Decimal(limit), buys, oh)
                if ref is not None and f.role == "ENTRY" and tseq:
                    entry_minute[tseq] = (f.session, ref[1])
                cat += (
                    ":opened_through"
                    if ref is not None and ref[1] == 0 and ref[0] != Decimal(limit)
                    else ":resting"
                )
            elif f.role == "STOP":
                stop = o.details.get("stop_price")
                if stop is None:
                    continue
                start = 0
                if tseq in entry_minute and entry_minute[tseq][0] == f.session:
                    start = entry_minute[tseq][1]
                    cat += ":entry_session"
                exit_first = any(
                    x.role == "EXIT" and x.arrival_at <= f.filled_at and x.status != "FILLED"
                    for x in by_trade.get(tseq, [])
                )
                if exit_first:
                    ref = (moved(m[0][0], oh, buys), 0)
                    cat += ":horizon_exit_first"
                else:
                    ref = stop_ref(m, Decimal(stop), buys, oh, ch, start)
                    tp = next((x for x in by_trade.get(tseq, []) if x.role == "TAKE_PROFIT"), None)
                    if ref is not None and tp is not None and tp.details.get("limit_price"):
                        target = Decimal(tp.details["limit_price"])
                        tp_ref = limit_ref(m[start:], target, buys, oh)
                        if tp_ref is not None and tp_ref[1] < ref[1]:
                            ref = (tp_ref[0], tp_ref[1])
                            cat += ":take_profit_first"
                    if ref is not None:
                        gap = (
                            (m[ref[1]][0] >= Decimal(stop))
                            if buys
                            else (m[ref[1]][0] <= Decimal(stop))
                        )
                        cat += ":gap" if gap else ":at_level"
            elif f.role == "EXIT":
                ref = (moved(m[0][0], oh, buys), 0)
            if ref is None:
                notes[f"{f.role}:not_reached_in_minute_bars"] += 1
                continue
            corr = corrected_price(f.role, bar, price, o, buys, oh, ch, exit_first)
            r = ref[0]
            # Positive error: the model priced the fill better for Atlas than the minutes do.
            b_err = (r - price) * q if buys else (price - r) * q
            c_err = (r - corr) * q if buys else (corr - r) * q
            c = cats[cat]
            c["fills"] += 1
            c["baseline_err"] += b_err
            c["corrected_err"] += c_err
            c["baseline_abs_bps"] += abs(price - r) / r * BPS
            c["corrected_abs_bps"] += abs(corr - r) / r * BPS
            by_strategy[strategy]["baseline_err"] += b_err
            by_strategy[strategy]["corrected_err"] += c_err
    out = {
        "categories": {
            k: {
                "fills": v["fills"],
                "baseline_error_usd": round(float(v["baseline_err"]), 2),
                "corrected_error_usd": round(float(v["corrected_err"]), 2),
                "baseline_mean_abs_bps": round(float(v["baseline_abs_bps"] / v["fills"]), 2),
                "corrected_mean_abs_bps": round(float(v["corrected_abs_bps"] / v["fills"]), 2),
            }
            for k, v in sorted(cats.items())
        },
        "by_strategy": {
            k: {kk: round(float(vv), 2) for kk, vv in v.items()}
            for k, v in sorted(by_strategy.items())
        },
        "totals": {
            "baseline_error_usd": round(float(sum(v["baseline_err"] for v in cats.values())), 2),
            "corrected_error_usd": round(float(sum(v["corrected_err"] for v in cats.values())), 2),
        },
        "unresolved": dict(notes),
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    out["variant"] = args.variant
    target = (
        OUT
        if args.variant == "live-sip-measured"
        else OUT.with_name(f"e7_intraday_{args.variant}{args.tag}.json")
    )
    target.write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
