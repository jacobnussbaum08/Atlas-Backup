"""The pure assessment with active reservations (Phase 6C). Hand-worked vectors.

ZERO_COSTS fixture: entry 100, stop 95 (5.00 a share at risk), equity 100,000, budget
500.00 (0.5%), loss allowance 2,000.00 (2%), every exposure at 100 a share. Without
reservations the permitted quantity is 100 shares (binding: risk budget).
"""

from __future__ import annotations

import random
import uuid
from decimal import Decimal as D

import pytest

from app.risk.assessment import assess
from app.risk.enums import AssessmentResult, CheckOutcome, TradeDirection
from app.risk.models import ReservedExposure
from app.tests.fixtures.risk_inputs import (
    ASSET,
    config,
    inputs,
    instrument,
    market,
    portfolio,
    proposal,
)

LONG, SHORT = TradeDirection.LONG, TradeDirection.SHORT
PASS = AssessmentResult.PASS_FOR_RESERVATION
OTHER = uuid.UUID("00000000-0000-0000-0000-00000000b002")


def reserved(
    exposure: str = "0",
    *,
    risk: str = "0",
    capital: str | None = None,
    margin: str = "0",
    asset_id: uuid.UUID = OTHER,
    direction: TradeDirection = LONG,
    sector: str | None = None,
    cluster: str | None = None,
    asset_class: str = "EQUITY",
) -> ReservedExposure:
    return ReservedExposure(
        reservation_id=uuid.uuid4(),
        asset_id=asset_id,
        symbol="RSV",
        asset_class=asset_class,
        direction=direction,
        sector=sector,
        cluster=cluster,
        remaining_quantity=D(1),
        exposure=D(exposure),
        capital=D(capital if capital is not None else exposure),
        margin=D(margin),
        risk=D(risk),
    )


def run(*res: ReservedExposure, prop=None, ins=None, mkt=None, cfg=None, **port):
    return assess(
        inputs(
            prop=prop,
            ins=ins,
            mkt=mkt,
            cfg=cfg,
            port=portfolio(reservations=list(res), **port),
        )
    )


def check(out, code):
    return next(c for c in out.checks if c.code == code)


def codes(out) -> set[str]:
    return {c.reason_code.value for c in out.reasons if c.reason_code is not None}


def test_without_reservations_nothing_changes():
    out = run()
    assert out.result is PASS
    assert out.permitted_quantity == D(100)
    assert "no active risk reservation" in check(out, "pending_exposure").message
    assert not [c for c in out.checks if c.code == "reserved_capacity"]
    assert out.calculations["reservation"] == {
        "quantity": "100",
        "risk": "500.00",
        "exposure": "10000.00",
        "capital": "10000.00",
        "margin": "0",
        "worst_entry_price": "100",
    }


def test_reserved_risk_reduces_the_loss_allowance():
    out = run(reserved(risk="1800"))
    # 2,000 - 1,800 = 200 left: 40 shares at 5.00.
    assert out.permitted_quantity == D(40)
    budget = check(out, "risk_budget")
    assert budget.outcome is CheckOutcome.RESTRICT
    assert "1800 reserved" in budget.message
    assert check(out, "reserved_capacity").outcome is CheckOutcome.INFO


def test_a_fully_reserved_allowance_rejects():
    out = run(reserved(risk="1000"), reserved(risk="1000"))
    assert out.result is AssessmentResult.REJECTED
    assert "RISK_BUDGET_EXHAUSTED" in codes(out)


@pytest.mark.parametrize(
    ("res", "binding", "quantity"),
    [
        (reserved("99000", asset_class="ETF", capital="0"), "gross_exposure", 10),
        (reserved("1000", capital="99500"), "available_capital", 5),
        (reserved("19000", asset_id=ASSET), "single_asset_exposure", 10),
        (reserved("29500", sector="TECH"), "sector_exposure", 5),
        (reserved("39000", cluster="CHIPS", sector="OTHER"), "correlated_exposure", 10),
    ],
    ids=["gross", "capital", "single-asset", "sector", "cluster"],
)
def test_each_capacity_counts_reservations(res, binding, quantity):
    ins = instrument(sector="TECH", cluster="CHIPS")
    # Correlation measured as absent counts as uncorrelated here, so each limit is isolated
    # (the cluster tag still links the cluster case).
    cfg = config({"missing_correlation_policy": "ASSUME_UNCORRELATED"})
    out = run(res, ins=ins, cfg=cfg)
    assert out.result is PASS
    assert out.binding_constraint == binding
    assert out.permitted_quantity == D(quantity)
    assert check(out, binding).details["reserved"] is not None


def test_a_reservation_counts_like_the_same_position():
    """The same exposure held or reserved leaves the same capacity."""
    from app.tests.fixtures.risk_inputs import held

    ins = instrument(sector="TECH")
    as_res = run(reserved("25000", sector="TECH", capital="25000"), ins=ins)
    position = held("RSV", "25000", sector="TECH", asset_id=OTHER)
    as_held = assess(inputs(ins=ins, port=portfolio(positions=[position])))
    assert as_res.permitted_quantity == as_held.permitted_quantity == D(50)
    assert as_res.binding_constraint == as_held.binding_constraint == "sector_exposure"


def test_liquidity_is_shared_with_reservations_of_the_same_asset():
    mkt = market(adv_quote=D("1000000"))  # 1% participation = 10,000
    out = run(reserved("9500", asset_id=ASSET), mkt=mkt)
    assert out.binding_constraint == "liquidity"
    assert out.permitted_quantity == D(5)


def test_net_exposure_counts_only_reservations_that_make_it_worse():
    long_res = reserved("40000", direction=LONG)
    short_res = reserved("40000", direction=SHORT, margin="20000")
    long_out = run(long_res, short_res)
    assert check(long_out, "net_exposure").measured == D("40000")  # the long one counts
    short_out = run(long_res, short_res, prop=proposal(SHORT, "100", "105"))
    assert check(short_out, "net_exposure").measured == D("-40000")  # the short one counts


def test_short_exposure_and_margin_count_reservations():
    short = proposal(SHORT, "100", "105")
    out = run(reserved("29500", direction=SHORT, margin="0", sector="OTHER"), prop=short)
    assert out.binding_constraint == "short_exposure"
    assert out.permitted_quantity == D(5)
    # Margin: 50% limit = 50,000; 49,800 reserved; the fixture's provider rate is 100%.
    out = run(reserved("1000", direction=SHORT, margin="49800"), prop=short)
    assert out.binding_constraint == "margin_usage"
    assert out.permitted_quantity == D(2)
    full = run(prop=short)
    rsv = full.calculations["reservation"]
    assert D(rsv["margin"]) == D(rsv["exposure"])  # margin rate max(0.5, provider 1.0)


def test_reserved_assets_count_as_open_positions():
    eight = [reserved("100", asset_id=uuid.uuid4()) for _ in range(8)]
    out = run(*eight)
    assert "MAX_OPEN_POSITIONS" in codes(out)
    assert "8 reserved" in check(out, "open_positions").message
    same = run(*eight[:7], reserved("100", asset_id=ASSET))
    assert same.result is PASS  # adding to an already reserved asset opens nothing


def test_an_opposite_reservation_is_refused():
    out = run(reserved("1000", asset_id=ASSET, direction=SHORT))
    assert "OPPOSITE_RESERVATION_ACTIVE" in codes(out)


def test_reservations_that_cannot_be_valued_block_approval():
    out = run(reservation_issues=[{"kind": "RATE_STALE", "currency": "EUR"}])
    assert out.result is AssessmentResult.INSUFFICIENT_DATA
    assert "RESERVATION_VALUATION_UNKNOWN" in codes(out)


def test_exposure_after_includes_reservations():
    out = run(
        reserved("20000", direction=LONG, sector="A"),
        reserved("5000", direction=SHORT, sector="B"),
        reserved("10000", direction=LONG, sector="C"),
    )
    after = out.exposure_after
    # The candidate is cut to 50 shares by the 40% correlated limit (35,000 reserved, all
    # ASSUMED correlated): 30,000 + 5,000 long after.
    assert out.permitted_quantity == D(50)
    assert D(after["long_exposure"]) == D("35000.00")
    assert D(after["short_exposure"]) == D("5000")
    assert after["open_positions"] == 2
    assert after["reserved"]["count"] == 3


def test_assessment_with_reservations_is_deterministic():
    res = [reserved("12345.67", risk="321.09", sector="TECH")]
    assert run(*res).model_dump() == run(*res).model_dump()


@pytest.mark.parametrize("seed", range(6))
def test_random_reservations_never_push_a_limit_over(seed):
    """Seeded random sets of reservations: an approval never exceeds the loss allowance left,
    the gross room left or the free capital, and its own amounts are rounded against Atlas."""
    rng = random.Random(seed)  # noqa: S311 - deterministic test vectors, not security
    cfg = config(
        prefs={
            "commission_per_unit": "0.005",
            "min_commission": "1",
            "fee_bps": "1",
            "slippage_bps": "5",
        }
    )
    for _ in range(120):
        res = [
            reserved(
                str(D(rng.randint(0, 25_000_00)) / 100),
                risk=str(D(rng.randint(0, 600_00)) / 100),
                capital=str(D(rng.randint(0, 30_000_00)) / 100),
                asset_id=uuid.uuid4(),
                direction=rng.choice([LONG, SHORT]),
                sector=rng.choice([None, "TECH", "ENERGY"]),
            )
            for _ in range(rng.randint(0, 5))
        ]
        entry = D(rng.randint(10_00, 300_00)) / 100
        stop = entry - D(rng.randint(20, 2000)) / 100
        if stop <= 0:
            continue
        out = run(
            *res,
            prop=proposal(LONG, str(entry), str(stop)),
            mkt=market(str(entry)),
            cfg=cfg,
        )
        if out.result is not PASS:
            continue
        rsv = out.calculations["reservation"]
        r_risk = sum((r.risk for r in res), D(0))
        r_gross = sum((r.exposure for r in res), D(0))
        r_capital = sum((r.capital for r in res), D(0))
        assert D(rsv["risk"]) <= D("2000") - r_risk
        assert D(rsv["risk"]) <= D("500")
        assert D(rsv["exposure"]) <= D("100000") - r_gross + D("0.01")  # rounded up to a cent
        assert D(rsv["capital"]) <= D("100000") - r_capital + D("0.01")
        assert len({r.asset_id for r in res}) + 1 <= 8
