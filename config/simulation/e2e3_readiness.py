"""E2/E3 stage-A readiness checks (read only; computes no strategy result).

For every stage-A configuration (``a20*--*``) this resolves the request exactly as a run
would (nothing is created) and checks, without computing any return or trade:

* the engine and conventions a new run gets (8A.2, the configured conventions, dividends);
* bar coverage (SIP RAW and SPLIT) from the first lookback session to the window's end;
* measured close and open spread samples for every session of the window;
* dividend retrieval coverage for every traded symbol;
* that no stored run has used the stage-A period (the test data is unseen).

Writes runs/e2e3_readiness.json and exits non-zero when a check fails.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from analyze import MANIFEST, reg_zone
from sqlalchemy import select

from app.backtesting.config import BacktestRequest
from app.backtesting.data import sessions_back, sessions_between
from app.backtesting.service import LIQUIDITY_LOOKBACK, BacktestService, sessions_for
from app.core.clocks import SystemClock
from app.core.config import get_settings
from app.database.models import (
    AssetMetricValueRecord,
    BacktestRunRecord,
    CashDividendRetrievalRecord,
    MarketBarRecord,
)
from app.database.sessions import build_engine, build_session_factory

OUT = MANIFEST.parent / "e2e3_readiness.json"
STAGE_START, STAGE_END = date(2020, 6, 1), date(2021, 12, 31)
# Bars must cover at least this share of the lookback-plus-window sessions per asset that
# was listed (an asset listed later is checked from its first bar).
MIN_BAR_SHARE = 0.99
MIN_SPREAD_SHARE = 0.95
# Listed after the first lookback session (provider listing dates): a later first bar is
# expected for these only; for every other asset it means missing history.
LATER_LISTINGS = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection


def lookback_first(service: BacktestService, config: Any) -> date:
    """The first session the replay loads (as BacktestService.load computes it)."""
    windows = [1]
    for rs in config.strategies:
        strategy = service.registry.get(rs.strategy_id)
        params = strategy.parse_parameters(rs.parameters)
        for tf in rs.timeframes:
            resolved = service.registry.features.resolve_all(strategy.required_features(params, tf))
            windows.append(
                sessions_for(tf, max((f.window for f in resolved if f.window), default=1))
            )
    if config.pipeline is not None and config.pipeline.coordinator:
        from app.backtesting.pipeline import default_intelligence_config, sessions_needed

        windows.append(sessions_needed(default_intelligence_config()))
    return sessions_back(config.start_date, max(windows) + LIQUIDITY_LOOKBACK + 10)


def main() -> None:
    factory = build_session_factory(build_engine(get_settings()))
    service = BacktestService(factory, SystemClock(), version=lambda: "readiness-check")
    zone = reg_zone()
    paths = sorted(MANIFEST.parent.glob("a20*--*.json"))
    report: dict[str, Any] = {"configs": len(paths), "failures": [], "checks": {}}
    fail = report["failures"]
    firsts: dict[str, date] = {}
    with factory() as session:
        resolved = {}
        for p in paths:
            request = BacktestRequest.model_validate_json(p.read_text())
            config = service.resolve(session, request)
            resolved[p.stem] = config
            want = request.execution_conventions
            if config.engine_version != "8A.2" or config.conventions is not want:
                fail.append(f"{p.stem}: engine {config.engine_version} {config.conventions}")
            if config.dividend_policy != "cash-at-ex-date-v1":
                fail.append(f"{p.stem}: dividends not modelled")
            expected_model = 2 if want.value == "daily-v2" else 1
            if config.execution_model["version"] != expected_model:
                fail.append(f"{p.stem}: execution model v{config.execution_model['version']}")
            firsts[p.stem] = lookback_first(service, config)
        report["checks"]["engine"] = {
            "versions": sorted({c.engine_version for c in resolved.values()}),
            "conventions": sorted({c.conventions.value for c in resolved.values()}),
            "execution_model_sha256": sorted({c.execution_model_sha256 for c in resolved.values()}),
        }
        first = min(firsts.values())
        report["checks"]["lookback_first_session"] = first.isoformat()
        assets: dict[uuid.UUID, str] = {}
        traded: set[uuid.UUID] = set()
        for c in resolved.values():
            for a in (*c.assets, *c.reference_assets):
                assets[uuid.UUID(a.asset_id)] = a.symbol
            traded |= {uuid.UUID(a.asset_id) for a in c.assets}
        # Bars: sessions with a valid SIP bar per asset and adjustment.
        expected = [d.day for d in sessions_between(first, STAGE_END)]
        b = MarketBarRecord
        bars: dict[str, dict[str, Any]] = {}
        for adjustment in ("RAW", "SPLIT"):
            have: dict[uuid.UUID, set[date]] = defaultdict(set)
            for aid, at in session.execute(
                select(b.asset_id, b.event_time).where(
                    b.asset_id.in_(list(assets)),
                    b.feed == "sip",
                    b.timeframe == "1Day",
                    b.adjustment == adjustment,
                    b.quality_status == "VALID",
                    b.event_time >= datetime.combine(first, time(), tzinfo=UTC) - timedelta(days=1),
                )
            ):
                have[aid].add(at.astimezone(zone).date())
            for aid, sym in sorted(assets.items(), key=lambda kv: kv[1]):
                days = have.get(aid, set())
                listed = min((d for d in days), default=None)
                span = [d for d in expected if listed is not None and d >= listed]
                share = len([d for d in span if d in days]) / len(span) if span else 0.0
                entry = bars.setdefault(sym, {})
                entry[adjustment] = {
                    "first_bar": None if listed is None else listed.isoformat(),
                    "share": round(share, 4),
                }
                lookback_ok = listed is not None and listed <= first
                if share < MIN_BAR_SHARE and span:
                    fail.append(f"bars {sym} {adjustment}: {share:.3f} of sessions")
                if not lookback_ok:
                    listing = LATER_LISTINGS.get(sym)
                    if listing is not None and listed is not None and listed <= listing:
                        entry[adjustment]["note"] = f"listed {listing}, after the lookback start"
                    else:
                        fail.append(
                            f"bars {sym} {adjustment}: history starts {listed}, need {first}"
                        )
        report["checks"]["bars"] = bars
        # Spreads: measured close and open samples per session of the stage-A period.
        m = AssetMetricValueRecord
        stage_days = [d.day for d in sessions_between(STAGE_START, STAGE_END)]
        spreads: dict[str, dict[str, float]] = {}
        for version in ("spread-sample-v1", "spread-sample-open-v1"):
            have = defaultdict(set)
            for aid, at in session.execute(
                select(m.asset_id, m.as_of).where(
                    m.metric == "quoted_spread_bps",
                    m.feed == "sip",
                    m.feature_version == version,
                    m.status == "OK",
                    m.asset_id.in_(list(assets)),
                )
            ):
                d = at.astimezone(zone).date()
                if STAGE_START <= d <= STAGE_END:
                    have[aid].add(d)
            for aid, sym in assets.items():
                if aid not in traded:
                    continue  # reference assets (the benchmark) are never traded
                listed = bars.get(sym, {}).get("RAW", {}).get("first_bar")
                span = [d for d in stage_days if listed and d >= date.fromisoformat(listed)]
                share = len(have.get(aid, set()) & set(span)) / len(span) if span else 1.0
                spreads.setdefault(sym, {})[version] = round(share, 4)
                if span and share < MIN_SPREAD_SHARE:
                    fail.append(f"spreads {sym} {version}: {share:.3f} of sessions")
        report["checks"]["spreads"] = spreads
        # Dividends: a successful retrieval covering the stage-A period for every symbol.
        r = CashDividendRetrievalRecord
        covered = set(
            session.scalars(
                select(r.symbol).where(
                    r.status == "OK", r.range_start <= STAGE_START, r.range_end >= STAGE_END
                )
            )
        )
        missing = sorted(set(assets.values()) - covered)
        report["checks"]["dividends_not_retrieved"] = missing
        fail.extend(f"dividends {s}: no retrieval covers the period" for s in missing)
        # Unseen: no stored run (any status) overlaps the stage-A period.
        overlapping = session.scalars(
            select(BacktestRunRecord.name).where(
                BacktestRunRecord.start_date <= STAGE_END,
                BacktestRunRecord.end_date >= STAGE_START,
            )
        ).all()
        report["checks"]["stored_runs_overlapping_stage_a"] = list(overlapping)
        fail.extend(f"stored run overlaps stage A: {n}" for n in overlapping)
    report["ready"] = not fail
    OUT.write_text(json.dumps(report, indent=1))
    print(json.dumps({k: report[k] for k in ("configs", "ready")}, indent=1))
    print("failures:", len(fail))
    for f in fail[:60]:
        print(" -", f)
    sys.exit(0 if not fail else 1)


if __name__ == "__main__":
    main()
