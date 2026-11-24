"""Write the profitability-simulation run configurations (D-234).

Every run uses the stored strategy parameters (nothing is fitted here), engine-default
risk, and the evidence universe (21 symbols chosen in Phase 8A; survivorship bias is
present and unmeasured). Run with: python config/simulation/generate.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
UNIVERSE = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
STRATEGIES = [
    "breakout", "event_driven", "mean_reversion", "momentum", "trend_following", "volatility",
]  # fmt: skip
PERIODS = {
    # REDACTED_FOR_GITHUB
    "main": ("2022-01-03", "2026-06-26"),
    # REDACTED_FOR_GITHUB
    # REDACTED_FOR_GITHUB
    "recent": ("2026-06-29", "2026-11-10"),
}
# Independent calendar-year windows: each is a fresh account under the same, unchanged
# risk limits. A run whose 10% drawdown breaker trips stays halted (the backtest never
# performs the manual recovery live trading requires), so the main period alone leaves
# 2023-2026 untested once it trips. A yearly restart is the equivalent of an operator
# recovering the breaker and resetting at the start of each year.
WINDOWS = {
    "y2022": ("2022-01-03", "2022-12-30"),
    "y2023": ("2023-01-03", "2023-12-29"),
    "y2024": ("2024-01-02", "2024-12-31"),
    "y2025": ("2025-01-02", "2025-12-31"),
    "y2026h1": ("2026-01-02", "2026-06-26"),
}
LIVE = {"coordinator": True, "live_entry_expiry": True}
VARIANTS: dict[str, dict] = {
    # The Phase 8A replay as 11A ran it (no coordinator, assumed spreads).
    "8a-iex": {"data": {"feed": "iex"}},
    "8a-sip": {"data": {"feed": "sip"}},
    # The live decision pipeline.
    "live-iex-assumed": {"data": {"feed": "iex"}, "pipeline": LIVE},
    "live-iex-measured": {
        "data": {"feed": "iex"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "iex"}},
    },
    "live-sip-measured": {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip"}},
    },
}
SENSITIVITY: dict[str, dict] = {
    "live-sip-spread2x": {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip", "multiplier": "2"}},
    },
    "live-sip-spread4x": {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip", "multiplier": "4"}},
    },
    "live-sip-slip10": {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip"}},
        "execution": {"equity_overrides": {"slippage_bps": "10"}},
    },
    "live-sip-slip25": {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip"}},
        "execution": {"equity_overrides": {"slippage_bps": "25"}},
    },
}


def _e1(opening: dict) -> dict:
    return {
        "data": {"feed": "sip"},
        "pipeline": {**LIVE, "measured_spreads": {"feed": "sip"}, "opening_entry": opening},
    }


E1_VARIANTS: dict[str, dict] = {
    # Primary: marketable at the open, worst price 1.5% beyond the signal close.
    "e1-t2": _e1({"tolerance_pct": "0.015"}),
    # Secondary: worst price 0.25 x the strategy's ATR beyond the signal close.
    "e1-t1": _e1({"tolerance_atr": "0.25"}),
    # REDACTED_FOR_GITHUB
    "e1-t0": _e1({"tolerance_pct": "0"}),
}

_E7_OFF = {"limit_marketable_at_open": False, "stop_fill_at_trigger": False,
           "market_orders_at_open": False}  # fmt: skip


def _e7(**on: bool) -> dict:
    return {
        "data": {"feed": "sip"},
        "pipeline": {
            **LIVE,
            "measured_spreads": {"feed": "sip"},
            "execution_fidelity": {**_E7_OFF, **on},
        },
    }


# E7 execution-model audit (D-236): the control (live-sip-measured) with corrected daily-bar
# conventions, all together and one at a time (attribution). Strategy, risk and entry
# order are the control's.
E7_VARIANTS: dict[str, dict] = {
    "e7-corrected": _e7(
        limit_marketable_at_open=True, stop_fill_at_trigger=True, market_orders_at_open=True
    ),
    "e7-limit": _e7(limit_marketable_at_open=True),
    "e7-stop": _e7(stop_fill_at_trigger=True),
    "e7-market": _e7(market_orders_at_open=True),
}


# E2/E3 stage A (pre-registered: docs/phase-reports/e2-e3-preregistration.md). Unseen period
# 2020-06..2021-12 on the evidence universe: falsification only (the universe was chosen in
# 2026, so survivorship bias is present and a pass is not evidence of an edge).
STAGE_A_WINDOWS = {
    "a2020h2": ("2020-06-01", "2020-12-31"),
    "a2021h1": ("2021-01-04", "2021-06-30"),
    "a2021h2": ("2021-07-01", "2021-12-31"),
}
# Design horizons from the families' code defaults (fixed in the pre-registration).
E2_HORIZONS = {"momentum": 10, "breakout": 10, "trend_following": 20}
_STAGE_DATA = {"data": {"feed": "sip"}, "dividends": True}


def _stage(conventions: str, multiplier: str = "1") -> dict:
    spreads: dict = {"feed": "sip"}
    if multiplier != "1":
        spreads["multiplier"] = multiplier
    return {
        **_STAGE_DATA,
        "execution_conventions": conventions,
        "pipeline": {**LIVE, "measured_spreads": spreads},
    }


E2_E3_VARIANTS: dict[str, dict] = {
    # Primary: corrected conventions (engine 8A.2 daily-v2), measured SIP spreads.
    "primary": _stage("daily-v2"),
    # Pessimistic comparison: the old (daily-v1) conventions, reported alongside.
    "old-conventions": _stage("daily-v1"),
    # Cost stress: twice the measured spreads.
    "spread2x": _stage("daily-v2", "2"),
}


def e2_e3_config(name: str, window: str, experiment: str, strategy: str, variant: dict) -> dict:
    start, end = STAGE_A_WINDOWS[window]
    selection: dict = {"strategy_id": strategy}
    body: dict = {
        "name": name,
        "start_date": start,
        "end_date": end,
        "universe": {"mode": "EXPLICIT", "symbols": UNIVERSE},
        "timeframes": ["1Day"],
        "benchmark_symbol": "SPY",
        "seed": 0,
        **variant,
    }
    if experiment in ("e2", "e3"):
        selection["parameters"] = {"holding_horizon_periods": E2_HORIZONS[strategy]}
    if experiment == "e2" and strategy == "trend_following":
        # Trend: no fixed target; its 3-ATR initial invalidation stays the (fixed) stop.
        body["execution"] = {"use_take_profit": False}
    if experiment == "e3":
        body["directions"] = ["LONG"]
    body["strategies"] = [selection]
    return body


def config(name: str, period: str, strategies: list[str], variant: dict) -> dict:
    start, end = {**PERIODS, **WINDOWS}[period]
    return {
        "name": name,
        "start_date": start,
        "end_date": end,
        "universe": {"mode": "EXPLICIT", "symbols": UNIVERSE},
        "strategies": [{"strategy_id": s} for s in strategies],
        "timeframes": ["1Day"],
        "benchmark_symbol": "SPY",
        "seed": 0,
        **variant,
    }


def main() -> None:
    out = HERE / "runs"
    out.mkdir(exist_ok=True)
    written = 0
    for period in PERIODS:
        for vname, variant in {**VARIANTS, **SENSITIVITY}.items():
            scopes = [("all6", STRATEGIES)]
            if vname in VARIANTS:
                scopes += [(s, [s]) for s in STRATEGIES]
            for scope, strategies in scopes:
                key = f"{period}--{vname}--{scope}"
                path = out / f"{key}.json"
                body = config(f"sim {key}", period, strategies, variant)
                path.write_text(json.dumps(body, indent=2))
                written += 1
    # Yearly windows: the full live pipeline on consolidated (SIP) data and quotes, and the
    # E1 entry-order treatments (pre-registered: docs/phase-reports/
    # e1-entry-order-preregistration.md). The control is the live-sip-measured variant.
    yearly = {"live-sip-measured": VARIANTS["live-sip-measured"], **E1_VARIANTS, **E7_VARIANTS}
    # Engine 8A.2 defaults (D-237: daily-v2 conventions and dividends) on the control: the
    # validation of the new default engine against 1-minute SIP bars (seen data only).
    yearly["v2-default"] = {
        **VARIANTS["live-sip-measured"],
        "execution_conventions": "daily-v2",
        "dividends": True,
    }
    for window in WINDOWS:
        for vname, variant in yearly.items():
            for scope, strategies in [("all6", STRATEGIES)] + [(s, [s]) for s in STRATEGIES]:
                key = f"{window}--{vname}--{scope}"
                body = config(f"sim {key}", window, strategies, variant)
                (out / f"{key}.json").write_text(json.dumps(body, indent=2))
                written += 1
    # E2/E3 stage A: written now, run only after the pre-registration is final and every
    # readiness check passes. ``control`` (stored parameters) is reporting only.
    plan = [("e2", s) for s in E2_HORIZONS] + [("e3", "momentum")]
    plan += [("control", s) for s in E2_HORIZONS]
    for window in STAGE_A_WINDOWS:
        for experiment, strategy in plan:
            for vname, variant in E2_E3_VARIANTS.items():
                if experiment == "control" and vname != "primary":
                    continue
                key = f"{window}--{experiment}-{vname}--{strategy}"
                body = e2_e3_config(f"sim {key}", window, experiment, strategy, variant)
                (out / f"{key}.json").write_text(json.dumps(body, indent=2))
                written += 1
    print(f"wrote {written} configurations to {out}")


if __name__ == "__main__":
    main()
