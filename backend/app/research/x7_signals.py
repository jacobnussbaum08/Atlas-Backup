# ruff: noqa: E501  (evidence tables read as one row each)
"""X7 (D-256): point-in-time strategy signals on the pinned X4.6.1 V2 / X5 data.

The **registered** strategy versions (``config/simulation/x7/registered_strategies_v2.json``, a
read-only capture of ``strategy_versions`` v2 from the main database, pinned by SHA-256) are
evaluated exactly as the live engine and the Phase 8A backtester evaluate them: the same
feature registry (``compute_bar_features``, ``compute_benchmark_feature``) and the same pure
``strategies.engine.decide``. Parameters are the stored ones, unchanged; nothing is tuned.

Inputs at a decision (the close of session *s*):

* the slot's executable bars up to and including *s* (its own, and an earlier formation's slot
  of the same security before its window: :meth:`x6_data.Market.history`), restated for splits
  and distribution share factors whose ex-date is on or before *s* (point-in-time split
  adjustment, as the backtester's ``POINT_IN_TIME_SPLIT``); a session without an executable
  bar is a missing period, never filled;
* the benchmark (SPY, a pinned fund member) under the same rules;
* Phase 3B regimes and Phase 3C news are not stored for 2017-2019: those features are
  UNAVAILABLE. Every strategy declares its regime features optional (recorded, not
  blocking); event-driven requires news, so it is never evaluated (a blocker, reported).

SHORT is never eligible: no borrow evidence exists (the asset is reported not shortable), so
every signal is LONG. Evaluations happen only where an entry could execute: the slot is
executable at *s* and a member at the next session.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from app.asset_discovery.sessions import ExchangeCalendar
from app.data_engine.calendar import trading_day
from app.data_engine.enums import PriceAdjustment
from app.database.models import AssetClass
from app.feature_engine.bar_features import BarHistory, compute_bar_features
from app.feature_engine.benchmark import Benchmark, compute_benchmark_feature
from app.feature_engine.features import Bar as FBar
from app.feature_engine.registry import (
    DAILY,
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ResolvedFeature,
    sha256_json,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.research import x6_data as xd
from app.research import x6_execution as ex
from app.strategies.base import AssetView, Eligibility, Strategy, StrategyParameters
from app.strategies.catalog import schema_sha256
from app.strategies.engine import EvaluationStageError, decide, ineligibility
from app.strategies.enums import EvaluationMode, TriggerKind
from app.strategies.inputs import BarSeries, FeatureInputs
from app.strategies.registry import StrategyRegistry, default_registry

DECISION = "D-256"
STRATEGIES_PATH = "config/simulation/x7/registered_strategies_v2.json"
STRATEGIES_SHA256 = "2331245f71a944c0fa49dd332d8cda2a42947b56205af1604b22831bc34894b7"
BENCHMARK_SLOT = "FUND|2016-12-30|SPY"
NOT_EVALUABLE = {
    "event_driven": "requires Phase 3C news (period_news_count); no news is stored for 2017-2019, so every evaluation would be skipped. Not evaluated."
}
NAMESPACE = uuid.UUID("6f6c2a8e-6f5e-4b7a-9d39-2b7c1f3a7e56")
UNAVAILABLE_REASON = (
    "Phase 3B regimes / Phase 3C news are not stored for 2017-2019 (X7): unavailable"
)


def xnys_calendar() -> ExchangeCalendar:
    from app.backtesting.data import CALENDAR

    return CALENDAR


# ---------------------------------------------------------------------- registered versions
@dataclass(frozen=True)
class Registered:
    strategy: Strategy
    params: StrategyParameters
    resolved: tuple[ResolvedFeature, ...]
    version: dict[str, Any]

    @property
    def strategy_id(self) -> str:
        return self.strategy.strategy_id


def load_registered(
    registry: StrategyRegistry | None = None, path: str = STRATEGIES_PATH
) -> dict[str, Registered]:
    """The registered v2 versions, pinned; each checked against the code (logic version,
    parameter hash, parameter schema hash). Raises on any difference."""
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != STRATEGIES_SHA256:
        raise xd.PinError(f"{path}: not the pinned registered-strategy capture")
    reg = registry or default_registry()
    out: dict[str, Registered] = {}
    for v in json.loads(raw)["versions"]:
        st = reg.get(v["strategy_id"])
        params = st.parse_parameters(v["parameters"])
        if st.logic_version != v["logic_version"]:
            raise xd.PinError(
                f"{st.strategy_id}: code logic {st.logic_version} != registered {v['logic_version']}"
            )
        if sha256_json(params.model_dump(mode="json")) != v["parameters_sha256"]:
            raise xd.PinError(f"{st.strategy_id}: parameters differ from the registered hash")
        if schema_sha256(st) != v["schema_sha256"]:
            raise xd.PinError(f"{st.strategy_id}: parameter schema differs from the registered one")
        resolved = tuple(reg.features.resolve(r) for r in st.required_features(params, DAILY))
        out[st.strategy_id] = Registered(st, params, resolved, v)
    return out


# ---------------------------------------------------------------------- point-in-time bars
@lru_cache(maxsize=1)
def _calendar_sessions() -> tuple[str, ...]:
    return tuple(xd._sessions("2015-06-01", "2020-01-31"))


@lru_cache(maxsize=1)
def _session_index() -> dict[str, int]:
    return {s: i for i, s in enumerate(_calendar_sessions())}


@cache
def _utc(session: str) -> tuple[datetime, datetime]:
    td = trading_day(date.fromisoformat(session))
    if td is None:
        raise ValueError(f"{session} is not a session")
    return td.regular_open, td.regular_close


def window_sessions(asof: str, count: int) -> list[str]:
    """The ``count`` XNYS sessions ending with ``asof`` (oldest first)."""
    i = _session_index()[asof]
    if i + 1 < count:
        raise ValueError(f"the calendar does not reach {count} sessions before {asof}")
    return list(_calendar_sessions()[i + 1 - count : i + 1])


class PitSeries:
    """One slot's executable bars (its own and an earlier formation's slot of the same
    security), served point in time: only bars on or before the as-of session, restated for
    the share ratios whose ex-date is on or before it."""

    def __init__(self, market: xd.Market, slot: str) -> None:
        sd = market.slots[slot]
        self.slot = slot
        self.bars = market.history(slot, sd.window[1])
        self.dates = [b.session for b in self.bars]
        self.ratios = sorted(xd.share_ratios(market, slot).items())
        self._cache: dict[tuple[str, ...], list[FBar]] = {}

    def _restated(self, asof: str) -> list[FBar]:
        applied = tuple(d for d, _ in self.ratios if d <= asof)
        if applied not in self._cache:
            out = []
            for b in self.bars:
                factor = Decimal(1)
                for d, r in self.ratios:
                    if b.session < d <= asof:
                        factor *= r
                opened, closed = _utc(b.session)
                out.append(FBar(event_time=opened, open=float(b.open / factor), high=float(b.high / factor),
                                low=float(b.low / factor), close=float(b.close / factor),
                                volume=float(b.volume * factor), available_at=closed))  # fmt: skip
            self._cache[applied] = out
        return self._cache[applied]

    def history(self, asof: str, count: int) -> BarHistory:
        """Bars of the ``count`` sessions ending at ``asof``; nothing after ``asof`` is read."""
        periods = window_sessions(asof, count)
        lo, hi = bisect_left(self.dates, periods[0]), bisect_right(self.dates, asof)
        restated = self._restated(asof)
        bars: dict[Hashable, FBar] = {
            date.fromisoformat(self.dates[i]): restated[i] for i in range(lo, hi)
        }
        return BarHistory(DAILY, [date.fromisoformat(p) for p in periods], bars)


def pit_history(market: xd.Market, slot: str, asof: str, count: int) -> BarHistory:
    return PitSeries(market, slot).history(asof, count)


def slot_uuid(slot: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, slot)


def unavailable(f: ResolvedFeature) -> FeatureValue:
    d = f.definition
    return FeatureValue(f.key, d.name, d.version, d.source, f.timeframe, f.param_map, None,
                        FeatureStatus.UNAVAILABLE, UNAVAILABLE_REASON, None)  # fmt: skip


ELIGIBILITY = Eligibility(
    known=True,
    provider="x4.5-member-data-v2",
    provider_status="ACTIVE",
    tradable=True,
    shortable=False,  # no historical borrow evidence: SHORT is never eligible
    easy_to_borrow=False,
)


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    slot: str
    session: str  # decision session (its close)
    entry: Decimal  # the newest close, restated as of the session
    stop: Decimal | None
    target: Decimal | None
    horizon: int
    strength: Decimal

    def as_json(self) -> dict[str, Any]:
        return {"strategy_id": self.strategy_id, "slot": self.slot, "session": self.session,
                "entry": str(self.entry), "stop": None if self.stop is None else str(self.stop),
                "target": None if self.target is None else str(self.target),
                "horizon": self.horizon, "strength": str(self.strength)}  # fmt: skip


def evaluate(
    reg: Registered, market: xd.Market, series: PitSeries, bench: PitSeries, session: str,
    registry: StrategyRegistry, calendar: ExchangeCalendar,
) -> tuple[str, list[Signal]]:  # fmt: skip
    """One evaluation at the close of ``session``: (state, LONG signals). The strategy sees
    exactly the window its own features declare, as ``compute_features`` loads it."""
    sd = market.slots[series.slot]
    view = AssetView(slot_uuid(sd.slot), sd.ticker, AssetClass.ETF if sd.pool == "FUND" else AssetClass.EQUITY,
                     None, ELIGIBILITY)  # fmt: skip
    if ineligibility(reg.strategy, reg.params, view) is not None:
        return "INELIGIBLE_ASSET", []
    bar_f = [f for f in reg.resolved if f.definition.source is FeatureSource.BARS]
    pair_f = [f for f in reg.resolved if f.definition.source is FeatureSource.BENCHMARK]
    other = [
        f
        for f in reg.resolved
        if f.definition.source not in (FeatureSource.BARS, FeatureSource.BENCHMARK)
    ]
    window = max((f.window for f in (*bar_f, *pair_f)), default=1)
    history = series.history(session, window)
    values: dict[str, FeatureValue] = dict(compute_bar_features(bar_f, history)) if bar_f else {}
    if pair_f:
        benchmark = Benchmark("SPY", slot_uuid(BENCHMARK_SLOT), bench.history(session, window))
        for f in pair_f:
            values[f.key] = compute_benchmark_feature(f, history, benchmark, view.asset_id)
    for f in other:
        values[f.key] = unavailable(f)
    _, cutoff = _utc(session)
    inputs = FeatureInputs(view.asset_id, DAILY, cutoff, cutoff, KnowledgeBasis.RECONSTRUCTED,
                           BarSeries("x4.5-member-data-v2", "sip", PriceAdjustment.SPLIT, calendar, timedelta(0)),
                           values, None)  # fmt: skip
    try:
        decision = decide(reg.strategy, reg.params, reg.resolved, view, inputs, timeframe=DAILY,
                          mode=EvaluationMode.HISTORICAL, basis=KnowledgeBasis.RECONSTRUCTED, data_cutoff=cutoff,
                          trigger=TriggerKind.BAR_CLOSE, trigger_ref=f"{DAILY}:{session}", registry=registry,
                          calendar=calendar, snapshot_ids=[])  # fmt: skip
    except EvaluationStageError as exc:
        return f"FAILED_{exc.stage.value}", []
    out = [
        Signal(
            reg.strategy_id,
            sd.slot,
            session,
            spec.reference_entry,
            spec.invalidation_level,
            spec.target_reference,
            spec.holding_horizon_periods,
            spec.signal_strength,
        )
        for spec in decision.specs
        if spec.direction.value == "LONG"
    ]
    return decision.state.value, out


def decision_sessions(sd: xd.SlotData) -> list[str]:
    """Sessions where an entry decided at the close could execute: executable now and a
    member at the next session (membership is known at the formation)."""
    if not sd.has_series or sd.usable_range is None:
        return []
    return [s for s in acc_sessions(sd.window[0], sd.window[1])
            if sd.executable(s) and sd.member_effective(ex.next_session(s))]  # fmt: skip


def acc_sessions(lo: str, hi: str) -> list[str]:
    return xd._sessions(lo, hi)


def slot_signals(
    regs: list[Registered], market: xd.Market, slot: str, registry: StrategyRegistry
) -> tuple[list[Signal], Counter[str]]:
    """Every registered strategy at every decision session of one slot."""
    calendar = xnys_calendar()
    signals: list[Signal] = []
    states: Counter[str] = Counter()
    series, bench = PitSeries(market, slot), PitSeries(market, BENCHMARK_SLOT)
    for s in decision_sessions(market.slots[slot]):
        for reg in regs:
            state, found = evaluate(reg, market, series, bench, s, registry, calendar)
            states[f"{reg.strategy_id}|{state}"] += 1
            signals += found
    return signals, states


# ---------------------------------------------------------------------- the full build
_WORKER: dict[str, Any] = {}


def _init_worker() -> None:
    docs = xd.load_documents()
    _WORKER["market"] = xd.Market.from_documents(docs)
    _WORKER["registry"] = default_registry()
    regs = load_registered(_WORKER["registry"])
    _WORKER["regs"] = [r for k, r in sorted(regs.items()) if k not in NOT_EVALUABLE]


def _run_slots(slots: list[str]) -> list[tuple[str, list[dict[str, Any]], dict[str, int]]]:
    out = []
    for slot in slots:
        sigs, states = slot_signals(_WORKER["regs"], _WORKER["market"], slot, _WORKER["registry"])
        out.append((slot, [s.as_json() for s in sigs], dict(states)))
    return out


def build_signals(market: xd.Market, workers: int = 6) -> dict[str, Any]:
    """Every registered, evaluable strategy at every decision session of every usable slot.
    Deterministic: results are merged by slot and sorted, whatever the worker order."""
    from concurrent.futures import ProcessPoolExecutor

    slots = sorted(s for s, sd in market.slots.items() if sd.has_series)
    chunks = [slots[i :: workers * 4] for i in range(workers * 4)]
    results: list[tuple[str, list[dict[str, Any]], dict[str, int]]] = []
    if workers <= 1:
        _init_worker()
        for c in chunks:
            results += _run_slots(c)
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
            for part in pool.map(_run_slots, chunks):
                results += part
    results.sort(key=lambda r: r[0])
    signals = sorted((s for _, sigs, _ in results for s in sigs),
                     key=lambda s: (s["session"], s["strategy_id"], s["slot"]))  # fmt: skip
    states: Counter[str] = Counter()
    per_slot: dict[str, dict[str, int]] = {}
    for slot, _, st in results:
        states.update(st)
        per_slot[slot] = dict(sorted(st.items()))
    return {"signals": signals, "states": dict(sorted(states.items())), "per_slot": per_slot}
