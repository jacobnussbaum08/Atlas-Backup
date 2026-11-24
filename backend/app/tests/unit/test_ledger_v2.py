"""X4.3.1 (D-248): the lifecycle ledger V2, built from the X4.3 FIXTURE stores.

The fixture filings, listings and bars are hand-written test data (see
``test_entity_resolution``), not evidence.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.research import ledger_v2 as l2
from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43
from app.tests.unit.test_entity_resolution import (  # noqa: F401 (pytest fixture)
    EXCHANGE,
    NEWCO,
    OLDCO,
    RIGHTSCO,
    stores,
)


def build(s: x4.Store, s43: x43.Store, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    ledger = lc.build(s)
    graph = graph or rs.build(s, s43, ledger, ledger_sha256="fixture")
    return l2.build(s, s43, ledger, graph, ledger_sha256="fixture", graph_sha256="fixture")


@pytest.fixture
def v2(stores):  # noqa: F811
    return build(*stores)


def events(doc: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
    return [e for e in doc["events"] if all(e.get(k) == v for k, v in kw.items())]


def test_exchange_copies_are_duplicates_not_events(v2):
    removal = f"VENUE_REMOVAL|CIK:{OLDCO}|ADS|NASDAQ|2018-12-31|25-NSE"
    dups = {c: d for c, d in v2["v1_events"].items() if d["disposition"] == "DUPLICATE_OF"}
    assert dups[f"VENUE_REMOVAL|CIK:{EXCHANGE}|ADS|NASDAQ|2018-12-31|25-NSE"]["v2"] == [removal]
    assert dups[f"DELISTING|CIK:{EXCHANGE}|ADS"]["v2"] == [f"DELISTING|CIK:{OLDCO}|ADS"]
    assert not [e for e in v2["events"] if e["security_key"].startswith(f"CIK:{EXCHANGE}")]
    assert v2["v1_securities"][f"CIK:{EXCHANGE}|ADS"]["disposition"] == "EXCHANGE_ALIAS"
    assert len(events(v2, code=removal)) == 1
    # A notice naming no issuer is not the exchange's: its own key, issuer unresolved.
    (u,) = v2["unresolved"]["notices_without_issuer"]
    (e,) = events(v2, security_key=u["security_key"])
    assert e["resolution_status"] == "UNRESOLVED"
    assert not next(s for s in v2["securities"] if s["security_key"] == u["security_key"])[
        "entity_key"
    ]


def test_rights_are_a_separate_class_and_do_not_end_the_common(v2):
    old = f"DELISTING|CIK:{RIGHTSCO}|COMMON"
    d = v2["v1_events"][old]
    assert d["disposition"] == "RECLASSIFIED"
    (new,) = d["v2"]
    assert new.startswith(f"DELISTING|CIK:{RIGHTSCO}|RIGHT-")
    assert not events(v2, code=old)
    end = events(v2, code=new)[0]
    assert end["reason"] == "CLASS_RIGHTS_EXTINGUISHED"
    assert end["effective_precision"] == "UNKNOWN"  # not linked to a ticker: no bounds
    assert end["not_before"] is None
    assert end["effective_date"] is None
    assert v2["v1_securities"][f"CIK:{RIGHTSCO}|COMMON"]["disposition"] == "RELABELLED"


def test_named_and_category_classes():
    lilac = "LiLAC Class A Ordinary Shares &amp; LiLAC Class C Ordinary Shares"
    assert l2.named_class(lilac, ["Liberty Global plc"]) == ["lilac"]
    assert l2.named_class("Common Shares of Beneficial Interest", ["Some Fund"]) == []
    assert l2.named_class("Ordinary Shares, par value NIS 0.04", ["Magic Ltd"]) == []
    key, why = l2.v2_key(
        "CIK:1|ADS",
        "ADS",
        "American Depositary Shares(Each representing One Class A Preferred)",
        [],
    )
    assert key.startswith("CIK:1|PREFERRED-")
    assert why == "CATEGORY_PREFERRED"
    key, why = l2.v2_key("CIK:2|CLASS_A_COMMON", "CLASS_A_COMMON", lilac, ["Liberty Global plc"])
    assert key.startswith("CIK:2|NAMED_CLASS-")
    assert l2.v2_key("CIK:3|COMMON", "COMMON", "Common Stock", ["X Inc"]) == ("CIK:3|COMMON", None)


def test_a_glyph_rule_revises_the_removal(v2):
    code = f"VENUE_REMOVAL|CIK:{NEWCO}|COMMON|NYSE|2017-07-01|25"
    (e,) = events(v2, code=code)
    assert e["reason"] == "ISSUER_VOLUNTARY_WITHDRAWAL"
    assert e["revision"] == 1
    d = v2["v1_events"][code]
    assert d["disposition"] == "REVISED"
    assert "reason" in d["changed_fields"]
    # The X4.3 link conflicts (still listed afterwards): stated, not linked.
    assert e["ticker"] is None
    assert "CONFLICTING" in e["explanation"]


def test_an_unreadable_rule_stays_unresolved(stores):  # noqa: F811
    s, s43 = stores
    ledger = lc.build(s)
    graph = copy.deepcopy(rs.build(s, s43, ledger, ledger_sha256="fixture"))
    for n in graph["notices"]:
        if n["issuer"] == f"CIK:{NEWCO}":
            n["rule_x43"] = {**n["rule_x43"], "rule": None, "reason": None, "status": "UNRESOLVED"}
    doc = l2.build(s, s43, ledger, graph, ledger_sha256="fixture", graph_sha256="fixture")
    (e,) = events(doc, code=f"VENUE_REMOVAL|CIK:{NEWCO}|COMMON|NYSE|2017-07-01|25")
    assert e["reason"] == "UNPARSED_RULE:25"
    (u,) = doc["unresolved"]["unreadable_rules"]
    assert u["security_key"] == f"CIK:{NEWCO}|COMMON"


def test_ticker_reuse_keeps_two_securities(v2):
    old = lc.reconstruct(v2, f"CIK:{OLDCO}|ADS")
    new = lc.reconstruct(v2, f"CIK:{NEWCO}|COMMON")
    assert old["ended"] is not None
    assert new["ended"] is None
    (end,) = events(v2, code=f"DELISTING|CIK:{OLDCO}|ADS")
    assert end["ticker"] == "ZZ"  # the X4.3 link, incorporated
    assert (end["not_before"], end["not_after"]) == ("2018-11-23", "2019-01-07")
    assert any(r["artifact"].endswith("x43_identity_graph_v1.json.gz") for r in end["evidence"])


def test_a_ticker_change_is_accepted_with_its_evidence(v2):
    (p,) = [p for p in v2["proposals"] if p["event_type"] == "TICKER_CHANGE"]
    assert p["proposed_code"] == f"TICKER_CHANGE|CIK:{NEWCO}|COMMON|NWC->ZZ"
    assert p["verdict"] == "ACCEPTED"
    (e,) = events(v2, code=p["v2_code"])
    assert (e["from_ticker"], e["to_ticker"]) == ("NWC", "ZZ")
    assert e["effective_precision"] == "BOUNDED"
    assert e["effective_date"] is None
    assert e["confidence"] == "MEDIUM"
    assert len(e["evidence"]) >= 3


def test_an_exchange_transfer_proposal_is_judged(stores, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lc, "ISHARES_2017", ())  # V1 then lacks the FA transfer
    doc = build(*stores)
    (p,) = [p for p in doc["proposals"] if p["event_type"] == "EXCHANGE_TRANSFER"]
    assert p["verdict"] == "ACCEPTED"
    (e,) = events(doc, code=p["v2_code"])
    assert (e["from_venue"], e["to_venue"]) == ("NYSE_ARCA", "NASDAQ")
    assert e["filing_date"] == "2017-07-26"
    assert lc.reconstruct(doc, "SECCLASS:C000000001")["ended"] is None


def test_a_transfer_already_in_v1_is_corroborated_not_duplicated(v2):
    (p,) = [p for p in v2["proposals"] if p["event_type"] == "EXCHANGE_TRANSFER"]
    assert p["verdict"] == "CORROBORATES_V1"
    assert len(events(v2, event_type="EXCHANGE_TRANSFER")) == 1


def test_contaminated_history_is_split_and_never_joined(v2):
    (h,) = v2["provider_histories"]["identities"]
    segs = h["segments"]
    assert segs[0]["security_key"] == f"CIK:{OLDCO}|ADS"
    assert segs[-1]["security_key"] == f"CIK:{NEWCO}|COMMON"
    assert h["verdict"].startswith("SPLIT")
    (j,) = v2["provider_histories"]["jumps"]
    assert j["finding"] == "SECURITY_BOUNDARY"


def test_successors_are_kept_apart():
    v = l2.V2.__new__(l2.V2)
    v.g = {
        "notices": [
            {"accession": "A", "security_key": "CIK:1|COMMON", "issuer": "CIK:1",
             "ticker": "XX", "filed": "2017-01-30", "reason": "CLASS_CONVERTED_BY_OPERATION_OF_LAW",
             "observed": {"after_filing": "SUCCESSOR_UNDER_SAME_TICKER"},
             "successor": {"ticker": "XX", "on": "2017-10-08"}},
        ],
        "ticker_edges": [],
    }  # fmt: skip
    (s,) = l2._successors(v)
    assert s["status"] == "UNRESOLVED"
    assert s["relationship"] == "SUCCESSOR_UNIDENTIFIED"
    assert s["price_inheritance"] == "FORBIDDEN"


def test_join_rules_need_identity_not_just_prices():
    assert set(l2.JOIN_RULES) >= {"J1_SAME_SECURITY", "J2_IDENTICAL_OVERLAP", "J5_OUTCOMES"}
    assert "never sufficient" in l2.JOIN_RULES["J2_IDENTICAL_OVERLAP"]


def test_v1_v2_reconciliation_is_complete(v2, stores):  # noqa: F811
    s, _ = stores
    ledger = lc.build(s)
    assert set(v2["v1_events"]) == {e["code"] for e in ledger["events"]}
    v2_codes = {e["code"] for e in v2["events"]}
    for d in v2["v1_events"].values():
        assert set(d["v2"]) <= v2_codes
    assert all(v2["event_origins"][c] for c in v2_codes)
    assert all(e["evidence"] for e in v2["events"])
    r = v2["reconciliation"]
    assert sum(r["v1_dispositions"].values()) == r["v1_total"] == len(ledger["events"])
    assert r["every_v1_event_has_a_disposition"]
    assert set(v2["v1_securities"]) == {x["security_key"] for x in ledger["securities"]}
    assert v2["supersedes"]["set_code"] == lc.SET_CODE
    assert v2["validation_issues"] == []


def test_v2_is_deterministic_and_written_once(stores, tmp_path):  # noqa: F811
    a, b = build(*stores), build(*stores)
    assert a["document_sha256"] == b["document_sha256"]
    assert a["content_sha256"] == lc.content_sha256(a["events"])
    path = tmp_path / "v2.json.gz"
    sha = l2.write(a, path)
    stored = x4.read_json(path)
    assert l2.document_sha256(stored) == a["document_sha256"]
    assert len(sha) == 64
    with pytest.raises(FileExistsError):
        l2.write(a, path)
