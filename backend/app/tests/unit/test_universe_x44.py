"""X4.4 (D-250): the historical universe, on FIXTURE data.

Listings, metrics, events and histories here are hand-written test data, not evidence.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.research import graph_v2 as g2
from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import universe_x44 as u
from app.research import x4_evidence as x4
from app.research import x432_evidence as x432
from app.tests.unit.test_entity_resolution import stores  # noqa: F401 (pytest fixture)

F = date(2016, 12, 30)
RULE = u.RULE["equity"]


def row(
    ticker: str,
    day: date,
    entity: str | None,
    security: str | None,
    name: str,
    cat: str = "COMMON",
    etf: bool | None = False,
) -> rs.Obs:
    return rs.Obs(
        ticker=ticker,
        name=name,
        venue="NYSE",
        family="symdir_otherlisted",
        day=day,
        source="symdir",
        ref={"artifact": "fixture", "sha256": "0", "locator": ticker},
        etf=etf,
        issuer_text=name,
        class_text=None,
        cat=cat,
        entity=entity,
        security=security,
        status="CONFIRMED" if entity else "UNRESOLVED",
        confidence="HIGH" if entity else "LOW",
    )


def metric(
    bars: int = 60, close: float = 50.0, mdv: float = 5e7, last: str = "2016-12-30"
) -> dict[str, Any]:
    return {
        "traded_bars": bars,
        "zero_volume_bars": 0,
        "first": "2016-10-03",
        "last": last,
        "last_close": close,
        "mdv": mdv,
    }


@pytest.fixture
def ix() -> rs.Index:
    a, b = date(2016, 10, 24), date(2017, 3, 14)
    return rs.Index.of(
        [
            # A company that later delisted: listed on both sides of the formation.
            row("DEAD", a, "CIK:1", "CIK:1|COMMON", "Dead Co Inc"),
            row("DEAD", b, "CIK:1", "CIK:1|COMMON", "Dead Co Inc"),
            # A reused ticker: the formation-era holder, then another issuer much later.
            row("RE", a, "CIK:2", "CIK:2|COMMON", "Old Holder Inc"),
            row("RE", b, "CIK:2", "CIK:2|COMMON", "Old Holder Inc"),
            row("RE", date(2018, 11, 23), "CIK:3", "CIK:3|COMMON", "New Holder Inc"),
            # A fund, an unresolved stock, an ETN.
            row("FND", a, "CIK:4", "SECCLASS:C1", "Fixture ETF", cat="FUND", etf=True),
            row("FND", b, "CIK:4", "SECCLASS:C1", "Fixture ETF", cat="FUND", etf=True),
            row("UNK", a, None, None, "Unknown Holdings"),
            row("UNK", b, None, None, "Unknown Holdings"),
            row("NOTE", a, None, None, "Fixture Bank Volatility ETN", cat="ETN"),
        ]
    )


def run(
    ix: rs.Index,
    monkeypatch,
    metrics: dict[str, dict[str, Any]],
    pool: str = "STOCK",
    size: int = 2,
) -> dict[str, dict[str, Any]]:
    monkeypatch.setattr(x4, "formation_metrics", lambda _s, _f: metrics)
    monkeypatch.setattr(u, "fund_filers", lambda _s: set())
    life = u.Lifecycle({"events": [], "securities": []}, {"successors": []})
    store: Any = None  # never read: the metrics and fund filers are patched
    recs = u.candidates(
        store, ix, life, F, (date(2017, 1, 3), date(2017, 12, 29)), RULE, pool, set(metrics)
    )
    u.provisional_types({(pool, F): recs}, {})
    u.rank_and_decide(recs, pool, size)
    return {r["ticker"]: r for r in recs}


def test_historical_members_include_later_delistings_and_formation_era_holders(ix, monkeypatch):
    recs = run(
        ix, monkeypatch, {"DEAD": metric(mdv=9e7), "RE": metric(mdv=8e7), "FND": metric(mdv=7e7)}
    )
    assert recs["DEAD"]["decision"] == "MEMBER"  # it delists later: still a member at F
    assert recs["RE"]["entity_key"] == "CIK:2"  # not the ticker's later holder
    assert recs["FND"]["decision"] == "OTHER_POOL_FUND"


def test_unresolved_identity_keeps_its_slot_but_is_not_confirmed(ix, monkeypatch):
    recs = run(
        ix, monkeypatch, {"UNK": metric(mdv=9e7), "DEAD": metric(mdv=8e7), "RE": metric(mdv=7e7)}
    )
    assert recs["UNK"]["decision"] == "MEMBER_IDENTITY_UNRESOLVED"
    assert recs["UNK"]["type_basis"].startswith("provisional")
    assert recs["DEAD"]["decision"] == "MEMBER"
    assert recs["RE"]["decision"] == "BELOW_CUTOFF"  # no backfill of the unresolved slot


def test_rule_thresholds_exclusions_and_notes(ix, monkeypatch):
    recs = run(
        ix,
        monkeypatch,
        {
            "DEAD": metric(bars=40),
            "RE": metric(close=3.0),
            "FND": metric(mdv=5e5),
            "NOTE": metric(),
            "SPY": metric(),
            "BRK.B": metric(),
            "ABC.W": metric(),
        },
    )
    assert recs["DEAD"]["reasons"] == ["FEW_TRADED_BARS"]
    assert recs["RE"]["reasons"] == ["LOW_CLOSE"]
    assert "LOW_DOLLAR_VOLUME" in recs["FND"]["reasons"]
    assert recs["NOTE"]["decision"] == "OTHER_POOL_ETN"
    assert "RULE_EXCLUSION" in recs["SPY"]["reasons"]
    assert "SYMBOL_RULE" not in recs["BRK.B"]["reasons"]
    assert "SYMBOL_RULE" in recs["ABC.W"]["reasons"]
    assert recs["ABC.W"]["decision"] == "NOT_ELIGIBLE"


def test_no_bar_after_the_formation_is_ever_used(ix, monkeypatch):
    recs = run(ix, monkeypatch, {"DEAD": metric(last="2017-01-05")})
    assert "BARS_AFTER_FORMATION" in recs["DEAD"]["reasons"]
    assert recs["DEAD"]["decision"] == "NOT_ELIGIBLE"


def test_missing_history_is_type_unresolved_and_ranked_nowhere(ix, monkeypatch):
    recs = run(ix, monkeypatch, {"GONE": metric(mdv=9e9), "DEAD": metric()})
    assert recs["GONE"]["type"] == "TYPE_UNRESOLVED"
    assert recs["GONE"]["decision"] == "EXCLUDED_TYPE_UNRESOLVED"
    assert recs["GONE"]["rank"] is None
    assert recs["GONE"]["shadow_in_top"] is True


def test_typing_follows_the_v4_v5_definition():
    r = [row("X", F, "CIK:9", "CIK:9|COMMON", "Gold Trust")]
    assert u.type_of("CIK:9|COMMON", r, "CIK:9")[0] == "STOCK"  # a grantor trust: equity
    assert u.type_of("CIK:0000000009|COMMON", r, "CIK:0000000009", {"0000000009"})[0] == "FUND"
    assert u.type_of("SECCLASS:C1", r, "CIK:9")[0] == "FUND"
    assert u.type_of("CIK:9|COMMON", [row("X", F, "CIK:9", "k", "Some ETF")], "CIK:9")[0] == "FUND"
    assert u.type_of(None, [], None)[0] == "TYPE_UNRESOLVED"


def test_lifecycle_in_window_records_ends_transfers_and_successors():
    v2 = {
        "securities": [
            {"security_key": "CIK:1|COMMON", "entity_key": "CIK:1", "class_label": "COMMON"}
        ],
        "events": [
            {
                "security_key": "CIK:1|COMMON",
                "event_type": "DELISTING",
                "code": "D",
                "resolution_status": "CONFIRMED",
                "filing_date": "2017-06-01",
                "not_before": "2017-03-14",
                "not_after": "2017-09-12",
                "effective_precision": "BOUNDED",
                "effective_date": None,
            },
            {
                "security_key": "CIK:1|COMMON",
                "event_type": "EXCHANGE_TRANSFER",
                "code": "T",
                "resolution_status": "CONFIRMED",
                "filing_date": None,
                "not_before": "2017-02-01",
                "not_after": "2017-04-01",
                "effective_precision": "BOUNDED",
                "effective_date": None,
            },
        ],
    }
    g = {
        "successors": [
            {
                "predecessor": "CIK:1|COMMON",
                "filed": "2017-05-01",
                "successor_security": "CIK:1|COMMON#SUCCESSOR@2017-05-01",
                "status": "CONFIRMED",
                "rule": "S1",
            }
        ]
    }
    life = u.Lifecycle(v2, g)
    lf = life.in_window("CIK:1|UNSPECIFIED", "CIK:1", date(2017, 1, 3), date(2017, 12, 29))
    assert lf["ledger_key"] == "CIK:1|COMMON"  # a company-list row's class, when unique
    assert [e["code"] for e in lf["ends"]] == ["D"]
    assert lf["continuity"] == ["T"]
    assert lf["successor"]["successor"].endswith("#SUCCESSOR@2017-05-01")


def test_windows_run_from_the_session_after_each_formation():
    w = u.windows(u.EQUITY_FORMATIONS)
    assert w[date(2016, 12, 30)] == (date(2017, 1, 3), date(2017, 12, 29))
    assert w[date(2018, 12, 31)] == (date(2019, 1, 2), date(2019, 12, 31))


def test_a_notice_bears_only_on_the_members_own_class():
    member = {"security_key": "CIK:1|CLASS_A_COMMON"}
    assert u._same_class("CIK:1|CLASS_A_COMMON", member)
    assert not u._same_class("CIK:1|NAMED_CLASS-abc", member)  # a tracking stock
    assert not u._same_class("CIK:1|PREFERRED-abc", member)
    assert u._same_class("CIK:1|COMMON", {"security_key": "CIK:1|UNSPECIFIED"})
    assert not u._same_class("CIK:2|COMMON", {"security_key": "CIK:1|UNSPECIFIED"})


def test_history_reconciliation_is_mutually_exclusive():
    links = [
        {"asset_id": a, "status": s, "request": {"label": a}}
        for a, s in (
            ("A", "CONFIRMED"),
            ("B", "CONFIRMED"),
            ("C", "CONFIRMED"),
            ("D", "CONFLICTING"),
            ("E", "UNRESOLVED"),
        )
    ]
    g = {"provider_links": links, "successor_registrations_in_histories": [{"asset_id": "C"}]}
    v2 = {"provider_histories": {"identities": [{"asset_id": "B", "verdict": "SPLIT: x"}]}}
    out = u.reconcile_histories(g, v2)
    assert out["total"] == 5
    assert out["by_status"] == {
        "CONFLICTING_SPLIT": 1,
        "IDENTIFIED": 1,
        "IDENTIFIED_FROM_SUCCESSION": 1,
        "IDENTIFIED_SEGMENT_LIMITED": 1,
        "UNRESOLVED": 1,
    }


def test_full_build_is_deterministic_and_matches_graph_v2(stores, tmp_path):  # noqa: F811
    s, s43 = stores
    ledger = lc.build(s)
    graph = rs.build(s, s43, ledger, ledger_sha256="fixture")
    v2 = l2.build(s, s43, ledger, graph, ledger_sha256="fixture", graph_sha256="fixture")
    s432 = x432.Store(tmp_path / "x432")
    graph2 = g2.build(s, s43, s432, graph, v2, graph_sha256="fixture", v2_sha256="fixture")
    a = u.build(s, s43, s432, v2, graph2, v2_sha256="fixture", graph2_sha256="fixture")
    b = u.build(s, s43, s432, v2, graph2, v2_sha256="fixture", graph2_sha256="fixture")
    assert a["content_sha256"] == b["content_sha256"]
    assert set(a["formations"]) == {
        "STOCK|2016-12-30",
        "STOCK|2017-12-29",
        "STOCK|2018-12-31",
        "FUND|2016-12-30",
    }
    assert a["comparison_with_v4_v5"] == {
        "available": False,
        "reason": "no V4/V5 snapshot in the evidence store",
    }
    path = tmp_path / "u.json.gz"
    u.write(a, path)
    assert x4.read_json(path)["content_sha256"] == a["content_sha256"]
    with pytest.raises(FileExistsError):
        u.write(a, path)
    graph2["ticker_edges"] = graph2["ticker_edges"][1:]
    with pytest.raises(ValueError, match="differ from graph V2"):
        u.build(s, s43, s432, v2, graph2, v2_sha256="fixture", graph2_sha256="fixture")
