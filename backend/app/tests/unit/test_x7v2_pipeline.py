# ruff: noqa: E501, PT018  (fixture rows on one line; paired assertions read as one fact)
"""X7 V2 (D-257): corrections C1-C12 on SYNTHETIC slots and signals (real XNYS calendar).

Every bar, signal, split, dividend and fund name below is test data, not provider data.
"""

from __future__ import annotations

from decimal import Decimal as D
from fractions import Fraction
from typing import Any

from app.research import x5_accounting as acc
from app.research import x7v2_gate as gate
from app.research import x7v2_pipeline as p2
from app.research import x7v2_spreads as sp2
from app.research.x6_data import Market

from .test_x6_execution import mk_slot

BASE = p2.SCENARIOS["baseline"]
PERIOD = ("2017-01-31", "2017-03-31")
AAA = "STOCK|2016-12-30|AAA"


def sig(slot: str, session: str, entry: str = "100.4", stop: str = "95", target: str | None = "110", horizon: int = 50,
        strength: str = "0.5", strategy: str = "momentum") -> dict[str, Any]:  # fmt: skip
    return {"strategy_id": strategy, "slot": slot, "session": session, "entry": entry, "stop": stop,
            "target": target, "horizon": horizon, "strength": strength}  # fmt: skip


def run(market: Market, signals: list[dict[str, Any]], set_name: str = "momentum", period: tuple[str, str] = PERIOD,
        classification: dict[str, Any] | None = None, issuers: dict[str, str] | None = None) -> p2.X7V2Portfolio:  # fmt: skip
    return p2.simulate(market, signals, set_name, period, BASE, spreads=None, classification=classification or {},
                       issuers=issuers or {s: f"SLOT:{s}" for s in market.slots})  # fmt: skip


def fills(pf: p2.X7V2Portfolio) -> list[dict[str, Any]]:
    return [f for f in pf.fills if f.get("status") in ("FILLED", "PARTIAL")]


def after(start: str, scale: str, end: str = "2017-03-31") -> dict[str, dict[str, Any]]:
    """Bars from ``start`` scaled (a split's price effect)."""
    k = D(scale)
    out = {}
    for i, s in enumerate(acc._sessions("2017-01-03", end)):
        if s >= start:
            c = (D(100) + D(i % 5) / 10) * k
            out[s] = {
                "open": c - D("0.05") * k,
                "high": c + D("0.5") * k,
                "low": c - D("0.5") * k,
                "close": c,
                "vwap": c,
            }
    return out


# ---------------------------------------------------------------------- C4
def test_c4_the_horizon_counts_from_the_entry_fill():
    pf = run(Market({AAA: mk_slot()}), [sig(AAA, "2017-02-01", target=None, horizon=3)])
    entry, exit_ = fills(pf)[0], next(f for f in fills(pf) if f["side"] == "SELL")
    assert entry["session"] == "2017-02-02"
    # three sessions after the fill (02-03, 02-06, 02-07) end at the 02-07 close; exit at the 02-08 open
    assert exit_["session"] == "2017-02-08" and exit_["intent_id"] == f"2017-02-07|horizon|{AAA}"


# ---------------------------------------------------------------------- C11
def test_c11_a_held_positions_target_is_restated_by_a_reverse_split_no_phantom_exit():
    rsplit = {"type": "REVERSE_SPLIT", "date": "2017-02-06", "ratio": 0.5}
    sd = mk_slot(events=[rsplit], overrides=after("2017-02-06", "2"))
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-01", target="110")])
    q = D(fills(pf)[0]["quantity"])
    restated = [r for r in pf.restatements if r["event"] == "SPLIT_RESTATED_TARGET"]
    assert restated and restated[0]["before"] == "110" and D(restated[0]["after"]) == D(220)
    pos = pf.positions[AAA]
    assert pos.quantity == q / 2 or pos.quantity == (q / 2).to_integral_value(
        rounding="ROUND_FLOOR"
    )
    assert pos.stop == D("190") and not [
        f for f in fills(pf) if f.get("reason") == "TARGET"
    ]  # unrestated 110 would exit at once
    assert pf.violations == []


def test_c11_a_pending_entry_on_a_split_ex_date_is_restated_and_not_stopped_out():
    split = {"type": "STOCK_SPLIT", "date": "2017-02-06", "ratio": 2.0}
    sd = mk_slot(events=[split], overrides=after("2017-02-06", "0.5"))
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-03", entry="100.3", stop="95", target="110")])
    ev_ = next(r for r in pf.restatements if r["event"] == "SPLIT_RESTATED_INTENT")
    q = D(ev_["before"]["quantity"])
    assert (
        D(ev_["after"]["quantity"]) == 2 * q
        and D(ev_["after"]["stop"]) == D("47.5")
        and D(ev_["after"]["target"]) == D(55)
    )
    f = fills(pf)[0]
    assert f["session"] == "2017-02-06" and D(f["quantity"]) == 2 * q
    assert pf.positions[AAA].stop == D("47.5") and not [
        x for x in fills(pf) if x.get("reason") == "STOP"
    ]
    assert pf.trades == [] and pf.violations == []


def test_c11_a_pending_entry_on_a_reverse_one_for_three_floors_the_quantity():
    rs = {"type": "REVERSE_SPLIT", "date": "2017-02-06", "ratio": float(Fraction(1, 3))}
    sd = mk_slot(events=[rs], overrides=after("2017-02-06", "3"))
    sd.by_date["2017-02-06"][0]["ratio"] = (
        "0.3333333333"  # not exact: the restatement uses the recorded ratio
    )
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-03", entry="100.3", stop="95", target=None)])
    ev_ = next(r for r in pf.restatements if r["event"] == "SPLIT_RESTATED_INTENT")
    q = D(ev_["before"]["quantity"])
    assert D(ev_["after"]["quantity"]) == (q * D("0.3333333333")).to_integral_value(
        rounding="ROUND_FLOOR"
    )


def test_c11_a_share_factor_with_a_distribution_restates_by_the_factor_only():
    spin = {"type": "SPIN_OFF", "date": "2017-02-06", "share_factor": "1/3", "distribution_value_per_share": 5.0,
            "components": [{"ticker": "KID", "ratio": "1/2", "close": 10.0, "session": "2017-02-06"}],
            "tr_treatment": "APPLIED", "basis": "DOCUMENTED"}  # fmt: skip
    sd = mk_slot(events=[spin], overrides=after("2017-02-06", "3"))
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-01", target="110")])
    q = D(fills(pf)[0]["quantity"])
    t = next(r for r in pf.restatements if r["event"] == "SPLIT_RESTATED_TARGET")
    assert D(t["after"]) == D(330) and pf.positions[AAA].stop == D(285)
    assert pf.positions[AAA].quantity == (q / 3).to_integral_value(rounding="ROUND_FLOOR")
    assert any(e["event"] == "DISTRIBUTION_SOLD" for e in pf.events) and pf.violations == []


def test_c11_no_ex_date_means_no_restatement():
    pf = run(Market({AAA: mk_slot()}), [sig(AAA, "2017-02-01")])
    assert pf.restatements == []


def test_c11_a_stop_hit_only_because_of_a_dividend_drop_is_reported_not_changed():
    div = {
        "type": "CASH_DIVIDEND",
        "date": "2017-02-06",
        "rate": 5.0,
        "status": "COUNTED",
        "provider_id": "fx",
    }
    sd = mk_slot(events=[div], overrides={s: v for s, v in after("2017-02-06", "1").items()
                                           if False} | {"2017-02-06": {"open": D("95.5"), "high": D("96"), "low": D("95.2"), "close": D("95.5"), "vwap": D("95.5")}})  # fmt: skip
    pf = run(Market({AAA: sd}), [sig(AAA, "2017-02-01", stop="95.5", target=None)])
    diag = pf.payout_stops
    assert (
        diag and diag[0]["session"] == "2017-02-06" and diag[0]["triggered_only_by_payout"] is True
    )
    assert any(f.get("reason") == "STOP" for f in fills(pf))  # the rule itself is unchanged


# ---------------------------------------------------------------------- C2, C3
def test_c2_a_leveraged_or_inverse_fund_is_never_entered():
    spxu, ivv = "FUND|2016-12-30|SPXU", "FUND|2016-12-30|IVV"
    m = Market(
        {
            spxu: mk_slot(spxu, window=("2017-01-03", "2019-12-31")),
            ivv: mk_slot(ivv, window=("2017-01-03", "2019-12-31")),
        }
    )
    cls = {spxu: {"plain": False}, ivv: {"plain": True}}
    pf = run(
        m, [sig(spxu, "2017-02-01", strength="0.9"), sig(ivv, "2017-02-01")], classification=cls
    )
    assert {f["slot"] for f in fills(pf)} == {ivv} and pf.skipped[
        "INSTRUMENT_NOT_LONG_UNLEVERED"
    ] == 1


def test_c3_one_position_per_issuer_across_share_classes_but_funds_of_one_trust_are_separate():
    g, gl = "STOCK|2016-12-30|GOOG", "STOCK|2016-12-30|GOOGL"
    m = Market({g: mk_slot(g), gl: mk_slot(gl)})
    issuers = {g: "ISSUER:CIK1", gl: "ISSUER:CIK1"}
    pf = run(
        m,
        [
            sig(g, "2017-02-01", strength="0.9"),
            sig(gl, "2017-02-01", strength="0.5"),
            sig(gl, "2017-02-03"),
        ],
        issuers=issuers,
    )
    assert {f["slot"] for f in fills(pf) if f["side"] == "BUY"} == {g} and pf.skipped[
        "ISSUER_ALREADY_HELD"
    ] == 2
    f1, f2 = "FUND|2016-12-30|F1", "FUND|2016-12-30|F2"
    m2 = Market(
        {
            f1: mk_slot(f1, window=("2017-01-03", "2019-12-31")),
            f2: mk_slot(f2, window=("2017-01-03", "2019-12-31")),
        }
    )
    pf2 = run(m2, [sig(f1, "2017-02-01"), sig(f2, "2017-02-01")], classification={f1: {"plain": True}, f2: {"plain": True}},
              issuers={f1: f"SLOT:{f1}", f2: f"SLOT:{f2}"})  # fmt: skip
    assert {f["slot"] for f in fills(pf2)} == {f1, f2}
    assert pf.issuer_peak > 0


# ---------------------------------------------------------------------- C8
def _families_market() -> tuple[Market, list[dict[str, Any]]]:
    slots, signals = {}, []
    for fam in p2.FAMILIES:
        for j in range(4):
            name = f"STOCK|2016-12-30|{fam[:3].upper()}{j}"
            slots[name] = mk_slot(name)
            signals.append(
                sig(name, "2017-02-01", stop="99.4", strategy=fam, strength=str(0.5 + j / 10))
            )
    return Market(slots), signals


def entered(pf: p2.X7V2Portfolio) -> list[str]:
    return sorted({f["slot"] for f in fills(pf) if f["side"] == "BUY"})


def test_c8_sleeves_give_each_family_one_position_and_share_three():
    m, signals = _families_market()
    pf = run(m, signals, set_name="combined_sleeves")
    by_family: dict[str, int] = {}
    for d in pf.decisions:
        if "|entry|" in d["intent_id"]:
            fam = d["intent_id"].split("|")[3]
            by_family[fam] = by_family.get(fam, 0) + 1
    assert (
        sum(by_family.values()) == 8
        and all(v >= 1 for v in by_family.values())
        and max(by_family.values()) <= 2
    )


def test_c8_allocation_does_not_compare_strength_across_families():
    m, signals = _families_market()
    scaled = [
        dict(s, strength=str(D(s["strength"]) * 10)) if s["strategy_id"] == "volatility" else s
        for s in signals
    ]
    for set_name in ("combined_sleeves", "combined_round_robin"):
        assert entered(run(m, signals, set_name=set_name)) == entered(
            run(m, scaled, set_name=set_name)
        )


def test_c8_round_robin_rotates_the_family_order_daily():
    assert (
        p2.rotation(0)[0] == p2.FAMILIES[0]
        and p2.rotation(1)[0] == p2.FAMILIES[1]
        and p2.rotation(5) == p2.rotation(0)
    )


def test_c8_sleeve_equity_starts_at_a_fifth_of_capital():
    pf = run(Market({AAA: mk_slot()}), [])
    assert p2.sleeve_equity(pf, "momentum", D(100000)) == D(20000)


# ---------------------------------------------------------------------- C1, C5, C9
def test_c1_exposure_matched_excess_and_c5_short_basis_rules():
    pf = run(Market({AAA: mk_slot()}), [sig(AAA, "2017-02-01", target=None, horizon=5)])
    prefix = pf.snapshots
    spy = {s["session"]: 1.001 for s in prefix}
    rec = p2.basis_record(pf, prefix, spy, {}, bootstrap=False)
    eq = [float(D(s["equity"])) for s in prefix]
    lm = [float(D(s["long_market_value"])) for s in prefix]
    x0 = eq[1] / eq[0] - 1 - (lm[0] / eq[0]) * 0.001
    assert (
        rec["sessions"] == len(prefix) - 1
        and abs(
            rec["excess_vs_exposure_matched_spy"]["mean_daily"]
            - sum(eq[t] / eq[t - 1] - 1 - lm[t - 1] / eq[t - 1] * 0.001 for t in range(1, len(eq)))
            / (len(eq) - 1)
        )
        < 1e-15
    )
    assert x0 == eq[1] / eq[0] - 1  # flat before the entry: zero exposure, zero benchmark
    assert (
        rec["headline"]["annualized_return"] is None and rec["headline"]["sharpe"] is None
    )  # < 126 sessions
    assert rec["excess_vs_exposure_matched_spy"]["annualized_arithmetic"] is None
    assert rec["exposure"]["active_sessions"] == sum(1 for v in lm[:-1] if v > 0)
    assert rec["breaker"]["drawdown_trip_session"] is None


def test_c5_an_incomplete_runs_basis_excludes_trades_after_it():
    sd = mk_slot(
        missing=("2017-03-06",),
        breaks={"2017-03-06": "MISSING_OR_EXCLUDED", "2017-03-07": "MISSING_OR_EXCLUDED"},
    )
    signals = [
        sig(AAA, "2017-02-01", target=None, horizon=3),
        sig(AAA, "2017-03-01", horizon=50, target=None),
    ]
    pf = run(Market({AAA: sd}), signals)
    prefix = [s for s in pf.snapshots if s["equity_complete"]]
    assert len(prefix) < len(pf.snapshots)
    rec = p2.basis_record(
        pf, prefix, {s["session"]: 1.0 for s in pf.snapshots}, {}, bootstrap=False
    )
    assert rec["end"] < "2017-03-06" and all(
        t["exit_session"] <= rec["end"] for t in pf.trades if t["exit_session"] <= rec["end"]
    )
    assert rec["trades"]["closed_trades"] == sum(
        1 for t in pf.trades if t["exit_session"] <= rec["end"]
    )


# ---------------------------------------------------------------------- C6
def test_c6_identity_check_excludes_mismatches_and_ambiguity_and_never_merges():
    m = Market({AAA: mk_slot()})
    row = [
        "AAA",
        "a1",
        sp2.OPEN_WINDOW,
        "v",
        "2017-02-01T14:31:00+00:00",
        "2017-02-01T14:32:00+00:00",
        "10",
        5,
        "100.1",
        "100.2",
        "x",
        5,
    ]
    wrong = [
        "AAA",
        "a2",
        sp2.OPEN_WINDOW,
        "v",
        "2017-02-02T14:31:00+00:00",
        "2017-02-02T14:32:00+00:00",
        "10",
        5,
        "23.0",
        "23.1",
        "x",
        5,
    ]
    amb1 = [
        "AAA",
        "a1",
        sp2.OPEN_WINDOW,
        "v",
        "2017-02-03T14:31:00+00:00",
        "2017-02-03T14:32:00+00:00",
        "10",
        5,
        "100.1",
        "100.2",
        "x",
        5,
    ]
    amb2 = [
        "AAA",
        "a3",
        sp2.OPEN_WINDOW,
        "v",
        "2017-02-03T14:31:00+00:00",
        "2017-02-03T14:32:00+00:00",
        "30",
        5,
        "100.1",
        "100.2",
        "x",
        5,
    ]
    dup = [
        "AAA",
        "a4",
        sp2.OPEN_WINDOW,
        "v",
        "2017-02-01T14:31:00+00:00",
        "2017-02-01T14:32:00+00:00",
        "10",
        5,
        "100.1",
        "100.2",
        "x",
        5,
    ]
    out = sp2.check([row, wrong, amb1, amb2, dup], m)
    assert len(out["kept"]) == 1 and out["kept"][0]["ledger_key"] == "KEY:AAA"
    assert out["excluded_by_reason"] == {
        "AMBIGUOUS_SYMBOL_DIFFERENT_VALUES": 2,
        "DUPLICATE_EQUAL_VALUE": 1,
        "MID_OUTSIDE_BAR_RANGE": 1,
    }
    spreads = sp2.MeasuredSpreadsV2(out["kept"], sp2.OPEN_WINDOW)
    assert spreads.at(("KEY:AAA", "AAA"), "2017-02-01") == (D("10"), "2017-02-01T14:32:00+00:00")
    assert (
        spreads.at(("KEY:OTHER", "AAA"), "2017-02-01") is None
    )  # another security with the symbol


def test_v1_is_untouched_by_v2():
    import hashlib
    from pathlib import Path

    from app.research import x7_pipeline as p1

    stored = __import__("app.research.x4_evidence", fromlist=["read_json"]).read_json(
        p1.RESULTS_PATH
    )
    for path, h in stored["inputs"]["sources_sha256"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == h, path


# ---------------------------------------------------------------------- D-258 pre-run integrity gate
CLS_REL = "config/simulation/x7/fund_classification_v1.json"
SPREADS_REL = "config/simulation/evidence/x7/sip_quoted_spreads_2017_2019_v2.json.gz"
REAL_PINS = dict(p2.PRE_RUN_INPUTS)  # taken at import, before any test monkeypatches it
PIPELINE_REL = "backend/app/research/x7v2_pipeline.py"
GATE_REL = "backend/app/research/x7v2_gate.py"


def _gate_copy(tmp_path, monkeypatch):
    """Copies of the pre-run inputs and of the real manifest, re-pointed at the copies.

    The real files are never written; the gate is aimed at the copies by monkeypatching.
    """
    import hashlib
    import json
    import shutil
    from pathlib import Path

    from app.research import x7v2_evaluator as ev

    real = json.loads(Path("config/simulation/x7/v2_freeze.json").read_bytes())
    copies: dict[str, Path] = {}
    files: dict[str, Any] = {}
    pins: dict[str, str | None] = {}
    for rel, entry in real["pre_run_inputs"]["files"].items():
        dst = tmp_path / Path(rel).name
        shutil.copyfile(rel, dst)
        assert hashlib.sha256(dst.read_bytes()).hexdigest() == entry["sha256"]
        copies[rel] = dst
        files[str(dst)] = dict(entry)
        pins[str(dst)] = REAL_PINS[rel]
    man = {**real, "pre_run_inputs": {**real["pre_run_inputs"], "files": files}}
    freeze = tmp_path / "v2_freeze.json"
    freeze.write_text(json.dumps(man))
    monkeypatch.setattr(ev, "FREEZE", freeze)
    monkeypatch.setattr(p2, "PRE_RUN_INPUTS", pins)
    monkeypatch.setattr(gate, "PIPELINE", str(copies[PIPELINE_REL]))  # D-259 anchor aimed at copies
    monkeypatch.setattr(gate, "SELF", str(copies[GATE_REL]))
    return copies, man, freeze


def test_the_real_manifest_records_the_reviewed_classification_and_spreads():
    import json
    from pathlib import Path

    from app.research import x7v2_classification as cls

    man = json.loads(Path("config/simulation/x7/v2_freeze.json").read_bytes())
    entries = man["pre_run_inputs"]["files"]
    assert (
        entries[CLS_REL]["sha256"]
        == "94fed7b977bb537ddc7a85bb86967fcd5334e0160904d42e1aae1f60964717ac"
    )
    assert (
        entries[SPREADS_REL]["sha256"]
        == "79f68b802d432c8fb79b8f45099edc58b9979b3ccf7f8df74c2ebb3e329ac2f2"
    )
    assert set(REAL_PINS) <= set(entries)
    assert entries[CLS_REL]["sha256"] == cls.SHA256 and entries[SPREADS_REL]["sha256"] == sp2.SHA256
    out = p2.check_pre_run_inputs()  # the real files pass
    assert all(v["found"] == v["manifest"] for v in out.values()) and len(out) == 6


def test_the_gate_passes_on_unmodified_copies(tmp_path, monkeypatch):
    _gate_copy(tmp_path, monkeypatch)
    assert len(p2.check_pre_run_inputs()) == 6
    assert gate.check_pipeline()["pipeline"] == gate.PIPELINE_SHA256


def test_a_modified_classification_fails_the_gate(tmp_path, monkeypatch):
    import json

    import pytest

    from app.research import x6_data as xd

    copies, _, _ = _gate_copy(tmp_path, monkeypatch)
    doc = json.loads(copies[CLS_REL].read_bytes())
    doc["funds"][next(k for k, v in doc["funds"].items() if v["ticker"] == "SPXU")]["plain"] = True
    copies[CLS_REL].write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    with pytest.raises(xd.PinError, match=r"fund_classification_v1\.json"):
        p2.check_pre_run_inputs()


def test_a_one_byte_change_to_any_pre_run_input_fails_the_gate(tmp_path, monkeypatch):
    import pytest

    from app.research import x6_data as xd

    rels = (CLS_REL, "backend/app/research/x7v2_classification.py", SPREADS_REL,
            "backend/app/research/x7v2_spreads.py", PIPELINE_REL, GATE_REL)  # fmt: skip
    for i, rel in enumerate(rels):
        sub = tmp_path / f"input_{i}"
        sub.mkdir()
        copies, _, _ = _gate_copy(sub, monkeypatch)
        data = bytearray(copies[rel].read_bytes())
        data[-1] ^= 1
        copies[rel].write_bytes(bytes(data))
        with pytest.raises(xd.PinError, match="do not match"):
            p2.check_pre_run_inputs()


def test_a_missing_manifest_entry_fails_the_gate(tmp_path, monkeypatch):
    import json

    import pytest

    from app.research import x6_data as xd

    copies, man, freeze = _gate_copy(tmp_path, monkeypatch)
    del man["pre_run_inputs"]["files"][str(copies[CLS_REL])]
    freeze.write_text(json.dumps(man))
    with pytest.raises(xd.PinError, match="not recorded in the freeze manifest"):
        p2.check_pre_run_inputs()
    del man["pre_run_inputs"]
    freeze.write_text(json.dumps(man))
    with pytest.raises(xd.PinError, match="not recorded in the freeze manifest"):
        p2.check_pre_run_inputs()


def test_the_file_and_manifest_changed_together_still_fail_on_the_embedded_pin(
    tmp_path, monkeypatch
):
    """Replacing the artifact and re-recording the manifest is caught by the module's own pin."""
    import hashlib
    import json

    import pytest

    from app.research import x6_data as xd

    copies, man, freeze = _gate_copy(tmp_path, monkeypatch)
    copies[CLS_REL].write_bytes(b"{}")
    man["pre_run_inputs"]["files"][str(copies[CLS_REL])]["sha256"] = hashlib.sha256(
        b"{}"
    ).hexdigest()
    freeze.write_text(json.dumps(man))
    with pytest.raises(xd.PinError, match="embedded_pin"):
        p2.check_pre_run_inputs()


def test_verify_inputs_runs_the_gate_before_anything_is_built(tmp_path, monkeypatch):
    import pytest

    from app.research import x6_data as xd
    from app.research import x7_pipeline as p1

    copies, _, _ = _gate_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        p1, "verify_inputs", lambda _sha=None: {}
    )  # the V1 pins are tested elsewhere
    copies[SPREADS_REL].write_bytes(copies[SPREADS_REL].read_bytes() + b"\0")
    with pytest.raises(xd.PinError, match="sip_quoted_spreads_2017_2019_v2"):
        p2.verify_inputs()

    def never(*_a, **_k):
        raise AssertionError("data was loaded before the gate refused")

    monkeypatch.setattr(xd, "load_documents", never)  # so the test can never start a run
    with pytest.raises(xd.PinError, match="do not match"):
        p2.build_results([("momentum", "year_2017", "baseline")])


# ---------------------------------------------------------------------- D-259 pipeline anchor
def test_the_real_pipeline_matches_its_anchor_and_the_manifest():
    import hashlib
    import json
    from pathlib import Path

    entries = json.loads(Path("config/simulation/x7/v2_freeze.json").read_bytes())[
        "pre_run_inputs"
    ]["files"]
    found = hashlib.sha256(Path(PIPELINE_REL).read_bytes()).hexdigest()
    assert found == gate.PIPELINE_SHA256 == entries[PIPELINE_REL]["sha256"]
    assert hashlib.sha256(Path(GATE_REL).read_bytes()).hexdigest() == entries[GATE_REL]["sha256"]
    assert REAL_PINS[PIPELINE_REL] == gate.PIPELINE_SHA256
    assert gate.check_pipeline() == {
        "pipeline": found,
        "anchor": found,
        "gate": entries[GATE_REL]["sha256"],
    }


def test_a_modified_pipeline_is_refused(tmp_path, monkeypatch):
    import pytest

    from app.research import x6_data as xd

    copies, _, _ = _gate_copy(tmp_path, monkeypatch)
    src = copies[PIPELINE_REL].read_text(encoding="utf-8")
    copies[PIPELINE_REL].write_text(
        src.replace('SLEEVE_SHARE = D("0.2")', 'SLEEVE_SHARE = D("0.3")'), encoding="utf-8"
    )
    with pytest.raises(gate.PipelineIntegrityError, match=r"x7v2_pipeline.py"):
        gate.check_pipeline()
    with pytest.raises(xd.PinError, match=r"x7v2_pipeline.py"):
        p2.check_pre_run_inputs()


def test_the_pipeline_and_its_manifest_entry_changed_together_are_still_refused(
    tmp_path, monkeypatch
):
    """The anchor is outside the pipeline and the manifest, so re-recording the manifest is not enough."""
    import hashlib
    import json

    import pytest

    from app.research import x6_data as xd

    copies, man, freeze = _gate_copy(tmp_path, monkeypatch)
    copies[PIPELINE_REL].write_bytes(copies[PIPELINE_REL].read_bytes() + b"# changed")
    new = hashlib.sha256(copies[PIPELINE_REL].read_bytes()).hexdigest()
    man["pre_run_inputs"]["files"][str(copies[PIPELINE_REL])]["sha256"] = new
    freeze.write_text(json.dumps(man))
    with pytest.raises(gate.PipelineIntegrityError, match="anchor"):
        gate.check_pipeline()
    with pytest.raises(xd.PinError, match="embedded_pin"):
        p2.check_pre_run_inputs()


def test_an_unrecorded_or_changed_gate_is_refused(tmp_path, monkeypatch):
    import json

    import pytest

    copies, man, freeze = _gate_copy(tmp_path, monkeypatch)
    copies[GATE_REL].write_bytes(copies[GATE_REL].read_bytes() + b"#")
    with pytest.raises(gate.PipelineIntegrityError, match=r"x7v2_gate.py"):
        gate.check_pipeline()
    copies[GATE_REL].write_bytes(copies[GATE_REL].read_bytes()[:-1])  # restored
    gate.check_pipeline()
    del man["pre_run_inputs"]["files"][str(copies[PIPELINE_REL])]
    freeze.write_text(json.dumps(man))
    with pytest.raises(gate.PipelineIntegrityError, match="not recorded"):
        gate.check_pipeline()


def test_verify_inputs_refuses_a_modified_pipeline_before_any_data_is_loaded(tmp_path, monkeypatch):
    import pytest

    from app.research import x6_data as xd
    from app.research import x7_pipeline as p1

    copies, _, _ = _gate_copy(tmp_path, monkeypatch)
    copies[PIPELINE_REL].write_bytes(copies[PIPELINE_REL].read_bytes() + b"#")

    def never(*_a, **_k):
        raise AssertionError("data was loaded or pins were read before the gate refused")

    monkeypatch.setattr(p1, "verify_inputs", never)
    monkeypatch.setattr(xd, "load_documents", never)  # so the test can never start a run
    with pytest.raises(gate.PipelineIntegrityError):
        p2.verify_inputs()
    with pytest.raises(gate.PipelineIntegrityError):
        p2.build_results([("momentum", "year_2017", "baseline")])


def test_the_cli_refuses_a_modified_pipeline_before_calling_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from app.research import cli

    copies, _, _ = _gate_copy(tmp_path, monkeypatch)
    copies[PIPELINE_REL].write_bytes(copies[PIPELINE_REL].read_bytes() + b"#")

    def never(*_a, **_k):
        raise AssertionError("the pipeline was called before the gate refused")

    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(postgres_db="atlas_sim_unit_test")
    )
    monkeypatch.setattr(cli, "build_engine", lambda _s: None)  # no database is touched
    monkeypatch.setattr(cli, "build_session_factory", lambda _e: None)
    for name in ("cli_backtest", "verify", "load_results", "build_results"):
        monkeypatch.setattr(p2, name, never)
    for command in ("x7v2-backtest", "x7v2-verify", "x7v2-evaluate"):
        with pytest.raises(gate.PipelineIntegrityError):
            cli.main([command])
