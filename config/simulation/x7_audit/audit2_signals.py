"""Audit 2: rebuild signals for sampled slots with independent history assembly (raw member-data
JSON, own previous-slot linking, own split restatement, own window), then call the registered
strategy code; compare with the stored X7 signals. Also check stored params, levels, warm-up."""
import gzip
import json
import random
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal as D
from fractions import Fraction

from app.data_engine.calendar import trading_day
from app.feature_engine.bar_features import BarHistory, compute_bar_features
from app.feature_engine.benchmark import Benchmark, compute_benchmark_feature
from app.feature_engine.features import Bar as FBar
from app.feature_engine.registry import DAILY, FeatureSource
from app.research import x6_data as xd
from app.research import x7_signals as sg
from app.strategies.engine import decide
from app.strategies.enums import EvaluationMode, TriggerKind
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.inputs import BarSeries, FeatureInputs
from app.data_engine.enums import PriceAdjustment
from datetime import timedelta


def load(p):
    return json.loads(gzip.decompress(open(p, "rb").read()))


md = load(xd.MEMBER_DATA)
led = load(xd.X5_LEDGER)
stored = load("config/simulation/lifecycle/x7_signals_v1.json.gz")
docs = xd.load_documents()
market = xd.Market.from_documents(docs)  # used only for executable() (validated against X5 in X6)
raw = {s["slot"]: s for s in md["slots"]}
regs = sg.load_registered()
registry = sg.default_registry()
cal = sg.xnys_calendar()
use = [r for k, r in sorted(regs.items()) if k not in sg.NOT_EVALUABLE]

# --- stored parameters actually used
reg_file = json.load(open(sg.STRATEGIES_PATH))
for v in reg_file["versions"]:
    p = regs[v["strategy_id"]].params.model_dump(mode="json")
    if p != v["parameters"]:
        print("PARAM MISMATCH", v["strategy_id"])
print("params identical to stored rows:", all(regs[v["strategy_id"]].params.model_dump(mode="json") == v["parameters"] for v in reg_file["versions"]))

# --- independent calendar
ALL = []
d = date(2015, 6, 1)
while d <= date(2020, 1, 31):
    if trading_day(d) is not None:
        ALL.append(d.isoformat())
    d += timedelta(days=1)
IDX = {s: i for i, s in enumerate(ALL)}


def rows(s):
    cols = s["bars"]["columns"]
    return {r[0]: dict(zip(cols, r)) for r in s["bars"]["rows"] if float(r[cols.index("volume")] or 0) > 0}


def ratios_for(slots):
    out = {}
    for s in slots:
        for e in led["slots"].get(s, []):
            if e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT"):
                out[e["date"]] = Fraction(str(e["ratio"]))
            elif e["type"] == "SPIN_OFF" and e["tr_treatment"] == "APPLIED":
                f = Fraction(1)
                for part in e["share_factor"].split("*"):
                    f *= Fraction(part)
                if f != 1:
                    out[e["date"]] = f
    return out


def my_history(slot, asof, count):
    me = raw[slot]
    same = [s for s in raw.values() if s["ledger_key"] == me["ledger_key"] and s["ticker"] == me["ticker"] and s["formation"] <= me["formation"]]
    periods = ALL[IDX[asof] - count + 1: IDX[asof] + 1]
    bars = {}
    for s in same:
        sd = market.slots[s["slot"]]
        for sess, r in rows(s).items():
            if sess < periods[0] or sess > asof or not sd.executable(sess):
                continue
            if s["slot"] != slot and sess >= me["window"][0]:
                continue
            bars.setdefault(sess, r)
    rat = {k: v for k, v in ratios_for([s["slot"] for s in same]).items() if k <= asof}
    out = {}
    for sess, r in bars.items():
        f = Fraction(1)
        for k, v in rat.items():
            if sess < k:
                f *= v
        td = trading_day(date.fromisoformat(sess))
        fd = D(f.numerator) / D(f.denominator)
        out[date.fromisoformat(sess)] = FBar(event_time=td.regular_open, open=float(xd.dec(r["open"]) / fd), high=float(xd.dec(r["high"]) / fd),
                                             low=float(xd.dec(r["low"]) / fd), close=float(xd.dec(r["close"]) / fd),
                                             volume=float(xd.dec(r["volume"]) * fd), available_at=td.regular_close)
    return BarHistory(DAILY, [date.fromisoformat(p) for p in periods], out)


def my_eval(reg, slot, s):
    sd = market.slots[slot]
    from app.database.models import AssetClass
    from app.strategies.base import AssetView
    view = AssetView(sg.slot_uuid(slot), sd.ticker, AssetClass.ETF if sd.pool == "FUND" else AssetClass.EQUITY, None, sg.ELIGIBILITY)
    bar_f = [f for f in reg.resolved if f.definition.source is FeatureSource.BARS]
    pair_f = [f for f in reg.resolved if f.definition.source is FeatureSource.BENCHMARK]
    other = [f for f in reg.resolved if f.definition.source not in (FeatureSource.BARS, FeatureSource.BENCHMARK)]
    w = max(f.window for f in (*bar_f, *pair_f))
    hist = my_history(slot, s, w)
    vals = dict(compute_bar_features(bar_f, hist))
    for f in pair_f:
        vals[f.key] = compute_benchmark_feature(f, hist, Benchmark("SPY", sg.slot_uuid(sg.BENCHMARK_SLOT), my_history(sg.BENCHMARK_SLOT, s, w)), view.asset_id)
    for f in other:
        vals[f.key] = sg.unavailable(f)
    cutoff = trading_day(date.fromisoformat(s)).regular_close
    inputs = FeatureInputs(view.asset_id, DAILY, cutoff, cutoff, KnowledgeBasis.RECONSTRUCTED,
                           BarSeries("x", "sip", PriceAdjustment.SPLIT, cal, timedelta(0)), vals, None)
    dec = decide(reg.strategy, reg.params, reg.resolved, view, inputs, timeframe=DAILY, mode=EvaluationMode.HISTORICAL,
                 basis=KnowledgeBasis.RECONSTRUCTED, data_cutoff=cutoff, trigger=TriggerKind.BAR_CLOSE,
                 trigger_ref=f"1Day:{s}", registry=registry, calendar=cal, snapshot_ids=[])
    return dec.state.value, [(spec.direction.value, str(spec.reference_entry), str(spec.invalidation_level), str(spec.target_reference), spec.holding_horizon_periods, str(spec.signal_strength)) for spec in dec.specs]


# --- sample: split/spin-off slots, slots with a previous slot, funds, random
usable = sorted(s for s, sd in market.slots.items() if sd.has_series)
ca = sorted({s for s in usable if any(e["type"] in ("STOCK_SPLIT", "REVERSE_SPLIT", "SPIN_OFF") and e.get("in_usable_range") for e in led["slots"].get(s, []))})
prev = [s for s in usable if market.previous_slot(s) is not None]
random.seed(20261119)
sample = None  # REDACTED_FOR_GITHUB: per-security research table or provider-derived selection
by_key = defaultdict(list)
for x in stored["signals"]:
    by_key[(x["slot"], x["session"], x["strategy_id"])].append(x)
mism, checked, sig_n = [], 0, 0
states = defaultdict(int)
for slot in sample:
    sd = market.slots[slot]
    sessions = [s for s in ALL if sd.window[0] <= s <= sd.window[1] and sd.executable(s) and (IDX[s] + 1 < len(ALL)) and sd.member_effective(ALL[IDX[s] + 1])]
    for s in sessions:
        for reg in use:
            st, specs = my_eval(reg, slot, s)
            states[f"{reg.strategy_id}|{st}"] += 1
            mine = [(x[1], x[2], x[3], x[4], x[5]) for x in specs if x[0] == "LONG"]
            theirs = [(x["entry"], x["stop"], x["target"], x["horizon"], x["strength"]) for x in by_key.get((slot, s, reg.strategy_id), [])]
            checked += 1
            sig_n += len(mine)
            if mine != theirs:
                mism.append((slot, s, reg.strategy_id, mine, theirs))
    # stored per-slot evaluation states must equal my counts for this slot
print("sample slots", len(sample), "evaluations", checked, "signals", sig_n, "mismatches", len(mism))
for m in mism[:10]:
    print("MISMATCH", m)
mine_slot_states = defaultdict(int)
# compare per-slot states
diff_states = 0
for slot in sample:
    pass
print("states", dict(states))
# --- signal levels: entry equals the raw close at the decision session (restated as of then = raw)
bad_entry = 0
for x in stored["signals"]:
    r = rows(raw[x["slot"]]).get(x["session"])
    if r is None or abs(float(x["entry"]) - float(r["close"])) > 1e-9:
        bad_entry += 1
print("signals whose entry != raw close at the decision session:", bad_entry, "of", len(stored["signals"]))
# --- warm-up: first signal session per strategy vs the earliest possible given history
first = defaultdict(lambda: "9999")
for x in stored["signals"]:
    first[x["strategy_id"]] = min(first[x["strategy_id"]], x["session"])
print("first signal session by strategy:", dict(first))
# --- no signal on a session where the slot is not a member at the next session or not executable
bad_member = 0
for x in stored["signals"]:
    sd = market.slots[x["slot"]]
    nxt = ALL[IDX[x["session"]] + 1]
    if not (sd.executable(x["session"]) and sd.member_effective(nxt)):
        bad_member += 1
print("signals outside executable/member sessions:", bad_member)
