"""Why mean_reversion rarely trades: a condition funnel over stored SIP daily bars.

APPROXIMATION, labelled: the conditions are re-implemented here with numpy from the
mean-reversion-1 documentation and the stored parameters (RSI14 <= 30 / >= 70, Bollinger
20/2, z-score 20 >= 2, >= 2 of 3 extremes with at least one price-deviation measure,
>= 1 ATR14 from the middle band, reversal close, SMA200 trend filter, 50-session slope
t-stat < 3 against the trade). The market-regime blocks (BEARISH/BULLISH market trend,
EXTREME volatility) are not re-implemented, so counts after step 5 are upper bounds. This
counts how often each condition holds; it measures no returns and changes nothing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import numpy as np
from analyze import reg_zone
from diagnose import load_closes

from app.core.config import get_settings
from app.database.sessions import build_engine, build_session_factory

UNIVERSE = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
START, END = date(2022, 1, 3), date(2026, 6, 26)


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    out = np.full_like(c, np.nan)
    au, ad = up[1 : n + 1].mean(), dn[1 : n + 1].mean()
    for i in range(n + 1, len(c)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
        out[i] = 100 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def rolling(x: np.ndarray, n: int, fn) -> np.ndarray:  # type: ignore[no-untyped-def]
    out = np.full_like(x, np.nan)
    for i in range(n - 1, len(x)):
        out[i] = fn(x[i - n + 1 : i + 1])
    return out


def slope_t(lc: np.ndarray, n: int = 50) -> np.ndarray:
    t = np.arange(n, dtype=float)
    out = np.full_like(lc, np.nan)
    for i in range(n - 1, len(lc)):
        y = lc[i - n + 1 : i + 1]
        b, a = np.polyfit(t, y, 1)
        resid = y - (a + b * t)
        se = np.sqrt((resid @ resid) / (n - 2) / ((t - t.mean()) @ (t - t.mean())))
        out[i] = b / se if se > 0 else np.nan
    return out


def main() -> None:
    factory = build_session_factory(build_engine(get_settings()))
    with factory() as session:
        bars = load_closes(session)
    _ = reg_zone
    funnel: dict[str, dict[str, int]] = {"LONG": defaultdict(int), "SHORT": defaultdict(int)}
    lagged = {"LONG": 0, "SHORT": 0}
    for sym in UNIVERSE:
        days, series = bars[sym]
        c = np.array([b["c"] for b in series])
        h = np.array([b["h"] for b in series])
        lo = np.array([b["l"] for b in series])
        tr = np.maximum(h - lo, np.maximum(abs(h - np.roll(c, 1)), abs(lo - np.roll(c, 1))))
        atr = rolling(tr, 14, np.mean)
        mid = rolling(c, 20, np.mean)
        sd = rolling(c, 20, np.std)
        z = (c - mid) / sd
        pb = (c - (mid - 2 * sd)) / (4 * sd)
        r = rsi(c)
        sma200 = rolling(c, 200, np.mean)
        st = slope_t(np.log(c))
        clv = (c - lo) / np.where(h > lo, h - lo, np.nan)
        for i in range(201, len(c)):
            if not START <= days[i] <= END:
                continue
            for side, s in (("LONG", 1), ("SHORT", -1)):
                f = funnel[side]
                f["0 sessions"] += 1
                rsi_x = r[i] <= 30 if s > 0 else r[i] >= 70
                band_x = pb[i] <= 0 if s > 0 else pb[i] >= 1
                z_x = z[i] <= -2 if s > 0 else z[i] >= 2
                stretch = (int(rsi_x) + int(band_x) + int(z_x)) >= 2 and (band_x or z_x)
                if not stretch:
                    continue
                f["1 stretch (2 of 3 extremes)"] += 1
                if not s * (mid[i] - c[i]) >= atr[i]:
                    continue
                f["2 >= 1 ATR from middle band"] += 1
                reversal = s * (c[i] - c[i - 1]) > 0 and (clv[i] >= 0.5 if s > 0 else clv[i] <= 0.5)
                if not reversal:
                    continue
                f["3 reversal close on the same bar"] += 1
                if not s * (c[i] - sma200[i]) > 0:
                    continue
                f["4 trend filter (SMA200 side)"] += 1
                if s * st[i] <= -3:
                    continue
                f["5 no opposing 50-session slope (t >= 3)"] += 1
            for side, s in (("LONG", 1), ("SHORT", -1)):
                if not START <= days[i] <= END:
                    continue
                stretched_prev = (
                    (pb[i - 1] <= 0 or z[i - 1] <= -2)
                    if s > 0
                    else (pb[i - 1] >= 1 or z[i - 1] >= 2)
                )
                rev = s * (c[i] - c[i - 1]) > 0
                trend_ok = s * (c[i] - sma200[i]) > 0
                if stretched_prev and rev and trend_ok:
                    lagged[side] += 1
    for side, f in funnel.items():
        print(side)
        for k in sorted(f):
            print(f"  {k:45} {f[k]:6d}")
    print("hypothetical count (stretch on prior session, reversal today, SMA200 side):", lagged)


if __name__ == "__main__":
    main()
