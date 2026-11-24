"""Phase 6B risk assessment checks: limits, boundaries, data, shorts (TEST FIXTURES)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.risk.assessment import NOT_AN_APPROVAL, assess
from app.risk.enums import AssessmentResult, BreakerType, CheckOutcome, MarkStatus, TradeDirection
from app.risk.models import AssessmentInputs, Correlation, Proposal
from app.tests.fixtures.risk_inputs import (
    AT,
    config,
    held,
    inputs,
    instrument,
    market,
    portfolio,
    proposal,
)

LONG, SHORT = TradeDirection.LONG, TradeDirection.SHORT
PASS = AssessmentResult.PASS_FOR_RESERVATION
REJECTED = AssessmentResult.REJECTED
INSUFFICIENT = AssessmentResult.INSUFFICIENT_DATA
HALTED = AssessmentResult.RISK_HALTED


def codes(out) -> set[str]:
    return {c.reason_code.value for c in out.reasons if c.reason_code is not None}


def check(out, code):
    return next(c for c in out.checks if c.code == code)


# ------------------------------------------------------------------ result semantics
def test_every_check_is_recorded_and_a_pass_is_not_an_approval():
    out = assess(inputs())
    assert out.result is PASS
    assert {c.code for c in out.checks} >= {
        "circuit_breakers",
        "portfolio_marked",
        "reconciliation",
        "stop",
        "tradable",
        "lot_size",
        "price_freshness",
        "spread",
        "open_positions",
        "risk_budget",
        "available_capital",
        "gross_exposure",
        "leverage",
        "net_exposure",
        "single_asset_exposure",
        "asset_class_exposure",
        "sector_exposure",
        "correlated_exposure",
        "liquidity",
        "potential_loss",
    }
    assert out.calculations["note"] == NOT_AN_APPROVAL


def test_assessment_is_deterministic_and_replayable_from_json():
    original = inputs(port=portfolio(positions=[held("AAA", "10000", sector="TECH")]))
    first = assess(original)
    replayed = assess(AssessmentInputs.model_validate(original.model_dump(mode="json")))
    assert replayed == first
    assert assess(original) == first


def test_proposals_carry_no_scores():
    # Strategy strength, model probabilities and LLM text cannot reach the risk engine.
    assert set(Proposal.model_fields) == {
        "source",
        "asset_id",
        "direction",
        "entry",
        "stop",
        "target",
        "requested_quantity",
        "expected_holding_days",
        "expires_at",
        "strategy_proposal_id",
        "ai_decision_id",
    }
    with pytest.raises(ValueError, match="Extra inputs"):
        Proposal.model_validate(
            proposal().model_dump() | {"signal_strength": "0.99", "approved": True}
        )


def test_active_breaker_halts_even_an_invalid_proposal():
    out = assess(inputs(prop=proposal(stop="105"), breakers=[BreakerType.KILL_SWITCH]))
    assert out.result is HALTED
    assert out.permitted_quantity is None
    assert "BREAKER_ACTIVE" in codes(out)
    assert "STOP_WRONG_SIDE" in codes(out)  # still recorded


@pytest.mark.parametrize("breaker", list(BreakerType))
def test_every_breaker_type_halts(breaker):
    out = assess(inputs(breakers=[breaker]))
    assert out.result is HALTED
    assert check(out, "circuit_breakers").details["breaker"] == breaker.value


def test_rejection_takes_precedence_over_missing_data():
    out = assess(inputs(prop=proposal(stop=None), mkt=market(price_status="MISSING", price=None)))
    assert out.result is REJECTED
    assert {"MISSING_STOP", "PRICE_MISSING"} <= codes(out)


def test_no_configuration_is_insufficient_data():
    out = assess(inputs(no_config=True))
    assert out.result is INSUFFICIENT
    assert codes(out) == {"BREAKER_ACTIVE"} - {"BREAKER_ACTIVE"} | {"NO_CONFIGURATION"}


# ------------------------------------------------------------------ stops
def test_missing_stop_is_rejected_when_required():
    out = assess(inputs(prop=proposal(stop=None)))
    assert out.result is REJECTED
    assert codes(out) == {"MISSING_STOP"}


def test_missing_stop_uses_a_labelled_assumption_when_not_required():
    out = assess(inputs(prop=proposal(stop=None), cfg=config({"require_stop": False})))
    assert out.result is PASS
    assert check(out, "stop").reason_code.value == "STOP_ASSUMED"
    # Assumed 10% stop: 500 / 10 = 50 shares.
    assert out.permitted_quantity == D("50")


@pytest.mark.parametrize(
    ("direction", "entry", "stop"),
    [
        (LONG, "100", "105"),
        (LONG, "100", "100"),
        (SHORT, "50", "48"),
        (SHORT, "50", "50"),
        (LONG, "100", "-1"),
    ],
)
def test_stop_on_the_wrong_side_is_rejected(direction, entry, stop):
    out = assess(inputs(prop=proposal(direction, entry=entry, stop=stop), mkt=market(entry)))
    assert out.result is REJECTED
    assert "STOP_WRONG_SIDE" in codes(out)


def test_stop_distance_boundary():
    # Minimum 10 bps: 99.90 is exactly 10 bps below 100 (allowed), 99.91 is 9 bps (refused).
    at_limit = assess(inputs(prop=proposal(entry="100", stop="99.90")))
    assert at_limit.result is PASS
    too_close = assess(inputs(prop=proposal(entry="100", stop="99.91")))
    assert too_close.result is REJECTED
    assert codes(too_close) == {"STOP_TOO_CLOSE"}
    assert check(too_close, "stop_distance").measured == D("9")


def test_expired_proposal_is_rejected():
    out = assess(inputs(prop=proposal(expires_at=AT)))
    assert out.result is REJECTED
    assert codes(out) == {"PROPOSAL_EXPIRED"}
    assert assess(inputs(prop=proposal(expires_at=AT + timedelta(seconds=1)))).result is PASS


def test_requested_quantity_below_and_above_the_permitted_size():
    smaller = assess(inputs(prop=proposal(requested_quantity=D("40"))))
    assert smaller.result is PASS
    assert smaller.permitted_quantity == D("40")
    assert smaller.binding_constraint == "requested_quantity"
    larger = assess(inputs(prop=proposal(requested_quantity=D("150"))))
    assert larger.result is PASS
    assert larger.permitted_quantity == D("100")
    assert check(larger, "requested_quantity").outcome is CheckOutcome.RESTRICT
    invalid = assess(inputs(prop=proposal(requested_quantity=D("0"))))
    assert invalid.result is REJECTED
    below_minimum = assess(
        inputs(
            prop=proposal(requested_quantity=D("4")),
            ins=instrument(quantity_increment=D(1), min_quantity=D(5)),
        )
    )
    assert codes(below_minimum) == {"INVALID_REQUESTED_QUANTITY", "BELOW_MINIMUM_QUANTITY"}


# ------------------------------------------------------------------ market data
def test_missing_and_stale_prices_are_insufficient_data():
    missing = assess(inputs(mkt=market(price=None, price_status="MISSING")))
    assert missing.result is INSUFFICIENT
    assert codes(missing) == {"PRICE_MISSING"}
    stale = assess(inputs(mkt=market(price_status="STALE", price_reason="older than 30 min")))
    assert stale.result is INSUFFICIENT
    assert codes(stale) == {"PRICE_STALE"}


def test_entry_deviation_boundary():
    # 2% maximum: 102 vs an entry of 100 is exactly 1.96%; 97.9 is 2.15%.
    assert assess(inputs(mkt=market("102"))).result is PASS
    exact = assess(inputs(prop=proposal(entry="102", stop="97"), mkt=market("100")))
    assert check(exact, "entry_vs_market").measured == D("0.02")
    assert exact.result is PASS
    far = assess(inputs(mkt=market("97.9")))
    assert far.result is REJECTED
    assert "ENTRY_DEVIATES_FROM_MARKET" in codes(far)


def test_stop_already_breached_is_rejected():
    out = assess(inputs(prop=proposal(entry="100", stop="99.5"), mkt=market("99.4")))
    assert out.result is REJECTED
    assert "STOP_ALREADY_BREACHED" in codes(out)
    short = assess(inputs(prop=proposal(SHORT, entry="50", stop="50.5"), mkt=market("50.6")))
    assert "STOP_ALREADY_BREACHED" in codes(short)


def test_wide_spread_is_rejected_and_a_missing_quote_uses_an_assumption():
    wide = assess(inputs(mkt=market("100", bid=D("99"), ask=D("101"))))
    assert wide.result is REJECTED
    assert codes(wide) == {"SPREAD_TOO_WIDE"}
    assert check(wide, "spread").measured == D("200")
    assumed = assess(
        inputs(
            mkt=market("100", quote_fresh=False), cfg=config(prefs={"fallback_spread_bps": "25"})
        )
    )
    assert assumed.result is PASS
    assert check(assumed, "spread_source").reason_code.value == "SPREAD_ASSUMED"


def test_missing_liquidity_is_insufficient_data():
    out = assess(inputs(mkt=market(adv_quote=None)))
    assert out.result is INSUFFICIENT
    assert codes(out) == {"LIQUIDITY_DATA_MISSING"}


def test_liquidity_limits_the_size():
    # 1% of 100,000 daily dollar volume = 1,000 -> 10 shares at 100.
    out = assess(inputs(mkt=market(adv_quote=D("100000"))))
    assert out.permitted_quantity == D("10")
    assert out.binding_constraint == "liquidity"


# ------------------------------------------------------------------ currencies
def test_foreign_instrument_needs_a_fresh_rate():
    eur = instrument(quote_currency="EUR")
    missing = assess(inputs(ins=eur, mkt=market(fx_rate=None, fx_status="MISSING")))
    assert missing.result is INSUFFICIENT
    assert codes(missing) == {"FX_RATE_MISSING"}
    stale = assess(inputs(ins=eur, mkt=market(fx_rate=D("1.08"), fx_status="STALE")))
    assert codes(stale) == {"FX_RATE_STALE"}


def test_foreign_instrument_is_sized_in_functional_terms_with_conversion_cost():
    # 1.25 USD per GBP; 500 USD budget / (5 GBP x 1.25) = 80 shares without costs.
    gbp = instrument(quote_currency="GBP")
    out = assess(inputs(ins=gbp, mkt=market(fx_rate=D("1.25"))))
    assert out.permitted_quantity == D("80")
    assert out.notional == D("10000.00")
    with_cost = assess(
        inputs(
            ins=gbp,
            mkt=market(fx_rate=D("1.25")),
            cfg=config(prefs={"fx_conversion_cost_bps": "10"}),
        )
    )
    # 10 bps on both fills: 0.001 x (100 + 95) = 0.195 GBP more per share; 500 / (5.195 x
    # 1.25) = 76.99 -> 76.
    assert with_cost.permitted_quantity == D("76")


def test_unsupported_currency_is_rejected():
    out = assess(inputs(ins=instrument(quote_currency="BTC", currency_supported=False)))
    assert out.result is REJECTED
    assert "UNSUPPORTED_CURRENCY" in codes(out)


# ------------------------------------------------------------------ instruments
def test_unknown_lot_size_is_insufficient_data():
    out = assess(inputs(ins=instrument(quantity_increment=None, min_quantity=None)))
    assert out.result is INSUFFICIENT
    assert codes(out) == {"LOT_SIZE_MISSING"}


def test_below_minimum_quantity_names_the_binding_constraint():
    out = assess(inputs(ins=instrument(quantity_increment=D(1), min_quantity=D(150))))
    assert out.result is REJECTED
    assert codes(out) == {"RISK_BUDGET", "BELOW_MINIMUM_QUANTITY"}


def test_tradability_rules():
    assert assess(inputs(ins=instrument(asset_status="INACTIVE"))).result is REJECTED
    assert assess(inputs(ins=instrument(tradable=False))).result is REJECTED
    unknown = assess(inputs(ins=instrument(reference_known=False, tradable=None)))
    assert unknown.result is INSUFFICIENT
    assert codes(unknown) == {"TRADABILITY_UNKNOWN"}


def test_disabled_asset_classes_and_forex():
    crypto = assess(inputs(ins=instrument(asset_class="CRYPTO")))
    assert crypto.result is REJECTED
    assert codes(crypto) == {"ASSET_CLASS_NOT_ENABLED"}
    forex = assess(inputs(ins=instrument(asset_class="FOREX")))
    assert codes(forex) == {"INSTRUMENT_NOT_SUPPORTED"}


def test_enabled_crypto_needs_a_reported_tick_and_cannot_be_shorted():
    cfg = config({"asset_class_limits_pct": {"EQUITY": "1", "ETF": "1", "CRYPTO": "0.1"}})
    ins = instrument(asset_class="CRYPTO", quantity_increment=D("0.0001"), min_quantity=D("0.0001"))
    no_tick = assess(inputs(ins=ins, cfg=cfg))
    assert no_tick.result is INSUFFICIENT
    assert codes(no_tick) == {"TICK_SIZE_MISSING"}
    with_tick = assess(
        inputs(ins=ins.model_copy(update={"provider_tick_size": D("0.01")}), cfg=cfg)
    )
    assert with_tick.result is PASS
    assert with_tick.permitted_quantity == D("100.0000")
    short = assess(inputs(ins=ins, cfg=cfg, prop=proposal(SHORT, entry="100", stop="105")))
    assert "SHORT_NOT_SUPPORTED" in codes(short)


# ------------------------------------------------------------------ shorts
def test_short_eligibility():
    def short(**ins):
        return assess(
            inputs(
                prop=proposal(SHORT, entry="50", stop="52"), mkt=market("50"), ins=instrument(**ins)
            )
        )

    assert short().result is PASS
    assert codes(short(shortable=None)) == {"SHORTABILITY_UNKNOWN"}
    assert codes(short(shortable=False)) == {"NOT_SHORTABLE"}
    assert codes(short(easy_to_borrow=False)) == {"BORROW_UNAVAILABLE"}
    assert codes(short(easy_to_borrow=None)) == {"BORROW_UNAVAILABLE"}
    assert codes(
        short(reference_known=False, shortable=None, easy_to_borrow=None, tradable=None)
    ) >= {
        "SHORTABILITY_UNKNOWN",
        "BORROW_UNAVAILABLE",
    }
    assert codes(short(margin_requirement_short=None)) == {"MARGIN_DATA_MISSING"}


def test_missing_margin_can_be_an_explicitly_restricted_assessment_by_policy():
    out = assess(
        inputs(
            prop=proposal(SHORT, entry="50", stop="52"),
            mkt=market("50"),
            ins=instrument(margin_requirement_short=None),
            cfg=config({"missing_margin_policy": "USE_CONFIGURED_MINIMUM"}),
        )
    )
    assert out.result is PASS
    assert check(out, "short_margin").reason_code.value == "MARGIN_RATE_ASSUMED"


def test_shorts_disabled():
    out = assess(
        inputs(
            prop=proposal(SHORT, entry="50", stop="52"),
            mkt=market("50"),
            cfg=config({"shorts_enabled": False}),
        )
    )
    assert codes(out) == {"SHORTS_DISABLED"}


def test_short_exposure_and_margin_limits():
    prop = proposal(SHORT, entry="50", stop="52")
    # Short exposure limit 30% = 30,000; 29,000 held short -> 1,000 left -> 20 shares at 50.
    out = assess(
        inputs(
            prop=prop,
            mkt=market("50"),
            port=portfolio(positions=[held("S1", "29000", direction=SHORT, sector="S1")]),
            correlations=[Correlation(asset_id=held("S1", "1").asset_id, value=D("0"))],
        )
    )
    assert out.permitted_quantity == D("20")
    assert out.binding_constraint == "short_exposure"
    # Margin limit 50% = 50,000; 49,000 used; rate 1.0 -> 1,000 / 50 = 20 shares.
    margin = assess(
        inputs(
            prop=prop,
            mkt=market("50"),
            port=portfolio(positions=[held("L1", "1000", margin="49000", sector="L1")]),
        )
    )
    assert margin.permitted_quantity == D("20")
    assert margin.binding_constraint == "margin_usage"


def test_short_collateral_uses_the_higher_of_provider_and_minimum_margin():
    out = assess(
        inputs(
            prop=proposal(SHORT, entry="50", stop="52"),
            mkt=market("50"),
            ins=instrument(margin_requirement_short=D("0.25")),
        )
    )
    assert check(out, "short_margin").measured == D("0.50")


# ------------------------------------------------------------------ exposure limits
def test_gross_exposure_boundary():
    # 99,900 held in an unrelated sector: 100 of room = exactly one share at 100 (no costs).
    def held_gross(amount: str):
        a = held("A", amount, sector="OTHER", asset_class="ETF")
        return inputs(
            port=portfolio(positions=[a], available_capital=D("100000")),
            correlations=[Correlation(asset_id=a.asset_id, value=D("0"))],
            cfg=config({"max_sector_pct": "1", "max_correlated_exposure_pct": "1"}),
        )

    one = assess(held_gross("99900"))
    assert one.result is PASS
    assert one.permitted_quantity == D("1")
    assert one.binding_constraint == "gross_exposure"
    none = assess(held_gross("99950"))
    assert none.result is REJECTED
    assert codes(none) == {"GROSS_EXPOSURE", "BELOW_MINIMUM_QUANTITY"}


def test_available_capital_limits_a_long():
    out = assess(inputs(port=portfolio(available_capital=D("1000"))))
    assert out.permitted_quantity == D("10")
    assert out.binding_constraint == "available_capital"


def test_single_asset_limit():
    out = assess(inputs(prop=proposal(entry="100", stop="99.5")))
    # Risk allows 1,000 shares; 20% of equity = 20,000 -> 200 shares.
    assert out.permitted_quantity == D("200")
    assert out.binding_constraint == "single_asset_exposure"


def test_net_exposure_limit_applies_by_direction():
    cfg = config({"max_net_exposure_pct": "0.5"})
    a = held("A", "45000", sector="X1")
    port = portfolio(positions=[a])
    corr = [Correlation(asset_id=a.asset_id, value=D("0"))]
    long_ = assess(inputs(port=port, cfg=cfg, correlations=corr))
    assert long_.permitted_quantity == D("50")
    assert long_.binding_constraint == "net_exposure"
    short = assess(
        inputs(
            port=port,
            cfg=cfg,
            correlations=corr,
            prop=proposal(SHORT, entry="100", stop="102"),
        )
    )
    assert short.binding_constraint != "net_exposure"


def test_sector_limit_and_unclassified_pool():
    port = portfolio(positions=[held("A", "25000", sector="SEMICONDUCTORS")])
    out = assess(inputs(port=port, ins=instrument(sector="SEMICONDUCTORS")))
    assert out.permitted_quantity == D("50")
    assert out.binding_constraint == "sector_exposure"
    other = assess(inputs(port=port, ins=instrument(sector="ENERGY")))
    assert other.binding_constraint == "risk_budget"
    # Unclassified assets share one bucket under the sector limit.
    pooled = assess(inputs(port=portfolio(positions=[held("B", "25000")])))
    assert pooled.permitted_quantity == D("50")
    rejected = assess(inputs(cfg=config({"unclassified_sector_policy": "REJECT"})))
    assert codes(rejected) == {"SECTOR_UNCLASSIFIED"}


def test_asset_class_limit():
    cfg = config({"asset_class_limits_pct": {"EQUITY": "0.3", "ETF": "1"}})
    out = assess(inputs(cfg=cfg, port=portfolio(positions=[held("A", "25000", sector="X")])))
    assert out.permitted_quantity == D("50")
    assert out.binding_constraint == "asset_class_exposure"


def test_correlated_exposure_counts_long_and_short_without_netting():
    a = held("A", "20000", sector="S1")
    b = held("B", "15000", sector="S2", direction=SHORT)
    corr = [
        Correlation(asset_id=a.asset_id, value=D("0.8")),
        Correlation(asset_id=b.asset_id, value=D("0.9")),
    ]
    out = assess(
        inputs(port=portfolio(positions=[a, b]), correlations=corr, ins=instrument(sector="S3"))
    )
    # 40,000 - (20,000 + 15,000) = 5,000 -> 50 shares: the short does not offset the long.
    assert out.permitted_quantity == D("50")
    assert out.binding_constraint == "correlated_exposure"
    members = check(out, "correlated_exposure").details["members"]
    assert {m["symbol"] for m in members} == {"A", "B"}


def test_low_correlation_is_not_counted_and_missing_correlation_is_assumed():
    a = held("A", "35000", sector="S1")
    low = assess(
        inputs(
            port=portfolio(positions=[a]),
            correlations=[Correlation(asset_id=a.asset_id, value=D("0.69"))],
            ins=instrument(sector="S2"),
        )
    )
    assert low.binding_constraint == "risk_budget"
    at_threshold = assess(
        inputs(
            port=portfolio(positions=[a]),
            correlations=[Correlation(asset_id=a.asset_id, value=D("0.70"))],
            ins=instrument(sector="S2"),
        )
    )
    assert at_threshold.binding_constraint == "correlated_exposure"
    unmeasured = assess(inputs(port=portfolio(positions=[a]), ins=instrument(sector="S2")))
    assert unmeasured.binding_constraint == "correlated_exposure"
    ignored = assess(
        inputs(
            port=portfolio(positions=[a]),
            ins=instrument(sector="S2"),
            cfg=config({"missing_correlation_policy": "ASSUME_UNCORRELATED"}),
        )
    )
    assert ignored.binding_constraint == "risk_budget"


def test_cluster_tag_links_an_etf_and_its_holdings():
    stocks = [held(f"SEMI{i}", "12000", sector="TECH", cluster="SEMICONDUCTORS") for i in range(3)]
    corr = [Correlation(asset_id=s.asset_id, value=D("0.1")) for s in stocks]
    etf = instrument(asset_class="ETF", sector="ETF_SECTOR", cluster="SEMICONDUCTORS")
    out = assess(inputs(port=portfolio(positions=stocks), ins=etf, correlations=corr))
    # 40,000 - 36,000 = 4,000 -> 40 shares, although each measured correlation is low.
    assert out.permitted_quantity == D("40")
    assert out.binding_constraint == "correlated_exposure"


# ------------------------------------------------------------------ positions and state
def test_position_count_boundary():
    seven = [held(f"P{i}", "1000", sector=f"S{i}") for i in range(7)]
    corr = [Correlation(asset_id=p.asset_id, value=D("0")) for p in seven]
    assert assess(inputs(port=portfolio(positions=seven), correlations=corr)).result is PASS
    eight = [*seven, held("P7", "1000", sector="S7")]
    corr8 = [*corr, Correlation(asset_id=eight[-1].asset_id, value=D("0"))]
    out = assess(inputs(port=portfolio(positions=eight), correlations=corr8))
    assert out.result is REJECTED
    assert codes(out) == {"MAX_OPEN_POSITIONS"}
    # Adding to one of the eight held assets keeps the count at eight.
    adding = proposal(asset_id=eight[0].asset_id)
    ins = instrument(asset_id=eight[0].asset_id)
    assert (
        assess(
            inputs(port=portfolio(positions=eight), correlations=corr8, prop=adding, ins=ins)
        ).result
        is PASS
    )


def test_opposite_position_is_rejected():
    port = portfolio(
        positions=[held("FIXT", "5000", direction=SHORT, asset_id=instrument().asset_id)]
    )
    out = assess(inputs(port=port))
    assert codes(out) == {"OPPOSITE_POSITION_HELD"}


def test_unmarked_portfolio_and_reconciliation_state():
    stale = assess(inputs(port=portfolio(mark_status=MarkStatus.STALE)))
    assert stale.result is INSUFFICIENT
    assert codes(stale) == {"PORTFOLIO_NOT_MARKED"}
    behind = assess(inputs(port=portfolio(reconciliation_current=False)))
    assert codes(behind) == {"RECONCILIATION_NOT_CURRENT"}
    allowed = assess(
        inputs(
            port=portfolio(reconciliation_current=False),
            cfg=config({"require_current_reconciliation": False}),
        )
    )
    assert allowed.result is PASS
    none = assess(inputs(port=portfolio(reconciliation_status=None)))
    assert codes(none) == {"RECONCILIATION_MISSING"}
    unknown = assess(inputs(port=portfolio(daily_loss=None)))
    assert codes(unknown) == {"LOSS_STATE_UNKNOWN"}


def test_equity_and_loss_allowance():
    broke = assess(inputs(port=portfolio(equity="0")))
    assert broke.result is REJECTED
    assert "EQUITY_NOT_POSITIVE" in codes(broke)
    exhausted = assess(inputs(port=portfolio(available_risk_budget=D("0"))))
    assert codes(exhausted) == {"RISK_BUDGET_EXHAUSTED"}
    capped = assess(inputs(port=portfolio(available_risk_budget=D("300"))))
    assert capped.risk_budget == D("300.00")
    assert capped.permitted_quantity == D("60")
    assert check(capped, "risk_budget").outcome is CheckOutcome.RESTRICT


def test_pending_exposure_is_zero_and_says_why():
    out = assess(inputs())
    pending = check(out, "pending_exposure")
    assert "Phase 6C" in pending.message


def test_exposure_after_is_reported():
    out = assess(inputs(port=portfolio(positions=[held("A", "10000", sector="X")])))
    after = out.exposure_after
    assert D(after["gross_exposure"]) == D("20000.00")
    assert after["open_positions"] == 2
    assert D(after["of_equity"]["gross_exposure"]) == D("0.2")


def test_unknown_asset_is_rejected():
    out = assess(inputs().model_copy(update={"instrument": None}))
    assert out.result is REJECTED
    assert "INSTRUMENT_NOT_SUPPORTED" in codes(out)


def test_correlation_for_unrelated_asset_ids_is_ignored():
    stray = Correlation(asset_id=uuid.uuid4(), value=D("1"))
    assert assess(inputs(correlations=[stray])).result is PASS
