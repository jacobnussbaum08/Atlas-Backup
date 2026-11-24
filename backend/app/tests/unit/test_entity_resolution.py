"""X4.3 (D-247): historical entity resolution, built from FIXTURE stores.

Every filing, listing, index line, fund file, bar and identity here is hand-written test
data, not real evidence. OLDCO (ZZ on Nasdaq, an ADS converted by merger) and NEWCO (ZZ on
NYSE later, plus a preferred) share a ticker; a fixture trust moves fund FA from NYSE Arca to
Nasdaq; a provider identity of ZZ carries OLDCO's prices and then another security's.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import date, timedelta
from typing import Any

import pytest

from app.research import lifecycle as lc
from app.research import resolution as rs
from app.research import x4_evidence as x4
from app.research import x43_evidence as x43

TRUST, OLDCO, NEWCO, TWIN_A, TWIN_B = (
    "0000000101",
    "0000000202",
    "0000000303",
    "0000000404",
    "0000000505",
)
SAME_A, SAME_B, RIGHTSCO, EXCHANGE = "0000000606", "0000000707", "0000000808", "0000876661"
ASSET = "a0000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------- fixture builders
def notice_xml(cik: str | None, name: str, cls: str, rule: str, exchange: str) -> str:
    issuer = f"<cik>{int(cik)}</cik>" if cik else ""
    return (
        f"FILED BY:\n\tCOMPANY DATA:\n\tCOMPANY CONFORMED NAME:\t{exchange}\n<DOCUMENT>"
        f"<notificationOfRemoval><exchange><cik>876661</cik><entityName>{exchange}"
        f"</entityName></exchange><issuer>{issuer}<entityName>{name}</entityName>"
        f"</issuer><descriptionClassSecurity>{cls}</descriptionClassSecurity><ruleProvision>"
        f"17 CFR 240.12d2-2({rule})</ruleProvision></notificationOfRemoval>"
    )


def wingdings_form25(checked: str) -> str:
    """An issuer Form 25 marked with Wingdings glyphs ('o' empty, 'x' checked)."""
    marks = {p: ("x" if p == checked else "o") for p in rs.PROVISIONS}
    return (
        "<DOCUMENT><html>FORM 25 Newco Inc New York Stock Exchange (Exact name of Issuer) "
        "Common Stock (Description of class of securities) Please place an X in the box: "
        f"{marks['a1']} 17 CFR 240.12d2-2(a)(1) {marks['a2']} 17 CFR 240.12d2-2(a)(2) "
        f"{marks['a3']} 17 CFR 240.12d2-2(a)(3) {marks['a4']} 17 CFR 240.12d2-2(a)(4) "
        f"{marks['b']} Pursuant to 17 CFR 240.12d2-2(b), the Exchange has complied. "
        f"{marks['c']} Pursuant to 17 CFR 240.12d2-2(c), the Issuer has complied with the "
        "requirements of 17 CFR 240.12d2-2(c) governing withdrawal.</html>"
    )


def trust_form25() -> str:
    return (
        "<DOCUMENT><html>FORM 25 Issuer: Fixture Trust Exchange: NYSE Arca, Inc. (Exact name of "
        "Issuer as specified) Units of beneficial interest (See exhibit A) (Description of class "
        "of securities) ☐ 17 CFR 240.12d2-2(a)(1) ☐ Pursuant to 17 CFR 240.12d2-2(b) ☒ "
        "Pursuant to 17 CFR 240.12d2-2(c), the Issuer has complied. Exhibit A Title of each "
        "class to be withdrawn Name of Exchange IRS Employer Identification No. Fund A ETF NYSE "
        "Arca, Inc. 12-3456780</html>"
    )


def symdir(kind: str, rows: list[tuple[str, str, str, str]]) -> bytes:
    if kind == "nasdaqlisted":
        head = (
            "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|"
            "ETF|NextShares"
        )
        body = [f"{s}|{n}|Q|N|N|100|{e}|N" for s, n, _, e in rows]
    else:
        head = (
            "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
            "NASDAQ Symbol"
        )
        body = [f"{s}|{n}|{x}|{s}|{e}|100|N|{s}" for s, n, x, e in rows]
    return ("\n".join([head, *body, "File Creation Time: 0101201700:00|||||||"]) + "\n").encode()


def fund_file(rows: list[tuple[str, str, str, str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rep", "CIK", "entity", "type", "series_id", "series", "class_id", "class",
                "class_ticker_symbol"])  # fmt: skip
    for cik, series, cls, ticker in rows:
        w.writerow(["811-1", cik, "Fixture Trust", "30", "S1" + cls[-4:], series, cls, "ETF",
                    ticker])  # fmt: skip
    return gzip.compress(buf.getvalue().encode())


def full_index(rows: list[tuple[str, str, str, str]]) -> bytes:
    head = "Form Type   Company Name                                   CIK         Date Filed\n"
    lines = [f"{f:<17}{n:<62}{int(c):<12}{d}  edgar/data/{int(c)}/x.txt" for f, n, c, d in rows]
    return gzip.compress((head + "-" * 40 + "\n" + "\n".join(lines) + "\n").encode())


def weekdays(start: date, n: int) -> list[str]:
    out: list[str] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


SWEEP_DAYS = weekdays(date(2016, 11, 28), 25)  # the fixture 2016-12-30 formation window
LATER_DAYS = weekdays(date(2019, 3, 1), 10)  # after OLDCO's end


@pytest.fixture
def stores(tmp_path, monkeypatch):
    s = x4.Store(tmp_path / "x4")
    notices = [
        # One 25-NSE indexed under the issuer and under the exchange: one notice.
        {"form": "25-NSE", "company": "OLDCO PLC", "cik": OLDCO, "filed": "2018-12-31",
         "path": "edgar/data/202/0000876661-18-000001.txt",
         "text": notice_xml(OLDCO, "OLDCO PLC", "American Depositary Shares", "a)(3",
                            "The Nasdaq Stock Market LLC")},
        {"form": "25-NSE", "company": "NASDAQ STOCK MARKET LLC", "cik": EXCHANGE,
         "filed": "2018-12-31", "path": "edgar/data/876661/0000876661-18-000001.txt",
         "text": notice_xml(OLDCO, "OLDCO PLC", "American Depositary Shares", "a)(3",
                            "The Nasdaq Stock Market LLC")},
        # A notice with no issuer CIK.
        {"form": "25-NSE", "company": "NYSE LLC", "cik": EXCHANGE, "filed": "2018-02-01",
         "path": "edgar/data/876661/0000876661-18-000002.txt",
         "text": notice_xml(None, "Unknown Co", "Common Stock", "b",
                            "NEW YORK STOCK EXCHANGE LLC")},
        # Purchase rights extinguished: X4.2 labels the class COMMON.
        {"form": "25-NSE", "company": "RIGHTSCO INC", "cik": RIGHTSCO, "filed": "2017-10-02",
         "path": "edgar/data/808/0000876661-17-000003.txt",
         "text": notice_xml(RIGHTSCO, "RIGHTSCO INC", "Common Share Purchase Rights", "a)(4",
                            "NEW YORK STOCK EXCHANGE LLC")},
        # An issuer Form 25 in Wingdings glyphs: (c), contradicted by later listings.
        {"form": "25", "company": "NEWCO INC", "cik": NEWCO, "filed": "2017-07-01",
         "path": "edgar/data/303/0000000303-17-000004.txt", "text": wingdings_form25("c")},
        # The trust's voluntary removal of fund FA from NYSE Arca.
        {"form": "25", "company": "FIXTURE TRUST", "cik": TRUST, "filed": "2017-07-26",
         "path": "edgar/data/101/0000000101-17-000005.txt", "text": trust_form25()},
    ]  # fmt: skip
    body = gzip.compress("\n".join(json.dumps(n) for n in notices).encode())
    s.put("sec/form25/2017Q3.jsonl.gz", body, {"kind": "fixture"})
    s.put(
        "sec/funds/investment_company_series_class_2017.csv.gz",
        fund_file([(TRUST, "Fund A ETF", "C000000001", "FA")]),
        {"kind": "fixture"},
    )
    s.put(
        "sec/full-index/2017Q1_form.gz",
        full_index([
            ("20-F", "OLDCO PLC", OLDCO, "2017-03-01"),
            ("10-K", "NEWCO INC", NEWCO, "2017-03-01"),
            ("10-K", "TWIN HOLDINGS, INC.", TWIN_A, "2017-03-01"),
            ("10-K", "TWIN HOLDINGS L.P.", TWIN_B, "2017-03-01"),
            ("10-K", "SAME NAME CORP", SAME_A, "2017-03-01"),
            ("10-K", "SAME NAME CORP", SAME_B, "2017-03-01"),
            ("10-K", "RIGHTSCO INC", RIGHTSCO, "2017-03-01"),
            ("N-CSR", "FIXTURE TRUST", TRUST, "2017-03-01"),
        ]),
        {"kind": "fixture"},
    )  # fmt: skip
    caps = {
        ("symdir_nasdaqlisted", "20161030000000"): symdir("nasdaqlisted", [
            ("ZZ", "Oldco PLC - American Depositary Shares", "", "N")]),
        ("symdir_otherlisted", "20170608000000"): symdir("otherlisted", [
            ("FA", "Fund A ETF", "P", "Y"),
            ("TWN", "Twin Holdings, Inc. Common Stock", "N", "N"),
            ("SNC", "Same Name Corp Common Stock", "N", "N"),
            ("NOB", "Nobody Ltd Common Stock", "N", "N"),
            ("RCO", "Rightsco Inc Common Stock", "N", "N"),
            ("NWC", "Newco Inc Common Stock", "N", "N"),
            ("NWC$A", "Newco Inc 6.5% Series A Preferred Stock", "N", "N")]),
        ("symdir_nasdaqlisted", "20170912000000"): symdir("nasdaqlisted", [
            ("FA", "Fund A ETF", "", "Y"),
            ("ZZ", "Oldco PLC - American Depositary Shares", "", "N")]),
        ("symdir_otherlisted", "20171129000000"): symdir("otherlisted", [
            ("RCO", "Rightsco Inc Common Stock", "N", "N"),
            ("NWC", "Newco Inc Common Stock", "N", "N")]),
        ("symdir_nasdaqlisted", "20181123000000"): symdir("nasdaqlisted", [
            ("FA", "Fund A ETF", "", "Y"),
            ("ZZ", "Oldco PLC - American Depositary Shares", "", "N")]),
        ("symdir_otherlisted", "20190105000000"): symdir("otherlisted", [
            ("ZZ", "Newco Inc Common Stock", "N", "N")]),
        ("symdir_nasdaqlisted", "20190107000000"): symdir("nasdaqlisted", [
            ("FA", "Fund A ETF", "", "Y")]),
    }  # fmt: skip
    for (family, ts), data in caps.items():
        s.put(f"archive/{family}/{ts}.raw", data, {"kind": "fixture", "timestamp": ts},
              http_status=200, digest_verified=True, brackets=[])  # fmt: skip
    sweep = {"ZZ": [{"t": f"{d}T05:00:00Z", "c": 10 + i / 10, "v": 1000 + i}
                    for i, d in enumerate(SWEEP_DAYS)]}  # fmt: skip
    s.put_json(
        "provider/sweeps/2016-12-30/len2/chunk_00000.json.gz",
        {"formation": "2016-12-30", "requests": [{"pages": [{"body": {"bars": sweep}}]}]},
        {"kind": "fixture"},
    )
    s.save()
    monkeypatch.setattr(lc, "NAMED", ())
    monkeypatch.setattr(lc, "ISHARES_2017", ("FA",))
    monkeypatch.setattr(lc, "ISHARES_TRUSTS", (TRUST,))
    # X4.3 store: one provider identity of ZZ as of 2016-12-30, OLDCO's bars then others.
    s43 = x43.Store(tmp_path / "x43")
    s43.put_json(
        "db/provider_identities.json.gz",
        [{"symbol": "ZZ", "revision": 0, "provider_status": "inactive", "security_type": "STOCK",
          "type_basis": "fixture", "cik": None, "scan_start": "2016-01-04",
          "scan_end": "2019-12-31", "first_bar": SWEEP_DAYS[0], "last_bar": LATER_DAYS[-1],
          "delisted_on": None, "delisting_basis": None, "asset_id": ASSET,
          "details": {"asof": "2016-12-30", "request_symbol": "ZZ"},
          "created_at": "2026-11-14"}],
        {"kind": "fixture"},
    )  # fmt: skip
    bars = [
        [d, str(10 + i / 10), str(10 + i / 10), str(1000 + i)] for i, d in enumerate(SWEEP_DAYS)
    ]
    bars += [[d, "55.5", "55.5", "500"] for d in LATER_DAYS]  # another security's prices
    s43.put_json(f"db/provider_bars/{ASSET[0]}.json.gz", {ASSET: bars}, {"kind": "fixture"})
    s43.save()
    return s, s43


@pytest.fixture
def graph(stores):
    s, s43 = stores
    return rs.build(s, s43, lc.build(s), ledger_sha256="fixture")


def notice(doc: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
    return [n for n in doc["notices"] if all(n.get(k) == v for k, v in kw.items())]


# ---------------------------------------------------------------------- text rules
def test_names_and_categories():
    assert rs.nkey("BP p.l.c.") == rs.nkey("BP PLC") == "bp"
    assert rs.nkey("Kohl&#39;s Corporation") == rs.nkey("KOHLS CORP") == "kohls"
    assert rs.nkey("Laboratory Corporation of America Holdings") == rs.nkey(
        "LABORATORY CORP OF AMERICA HOLDINGS"
    )
    assert rs.sorted_key("T. Rowe Price Group, Inc.") == rs.sorted_key("PRICE T ROWE GROUP INC")
    assert rs.category("Common Share Purchase Rights") == "RIGHT"
    assert rs.category("American Depositary Shares, each representing one Preferred") == (
        "PREFERRED"
    )
    assert rs.category("Units, each consisting of one share and one warrant") == "UNIT"
    assert rs.category("Class A Common Stock") == "CLASS_A_COMMON"
    assert rs.category("6.00% Subordinated Debentures due 2042") == "DEBT"
    assert rs.fname("SPDR(R) S&P(R) Biotech ETF") == rs.fname("SPDR S&P Biotech ETF")


def test_a_class_must_match_series_coupon_and_maturity():
    assert rs.compatible("Series A Preferred Stock", "PREFERRED", "6.5% Series A Preferred", "X$A")
    assert not rs.compatible("Series B Preferred Stock", "PREFERRED", "6.5% Series A Pref", "X$A")
    assert not rs.compatible("5.25% Notes due 2045", "DEBT", "5.25% Notes due 2046", "XB")
    assert not rs.compatible("Common Stock", "WARRANT", "Warrant", "XW")
    # A company list cannot show the class: only the plain class, never a Nasdaq warrant.
    assert rs.compatible("Common Stock", "UNSPECIFIED", None, "ABCD")
    assert not rs.compatible("Common Stock", "UNSPECIFIED", None, "ABCDW")


@pytest.mark.parametrize(
    ("marks", "expected"),
    [
        (("o", "x"), "c"),  # Wingdings
        (("¨", "ý"), "c"),  # Wingdings, other code page
        (("[ ]", "[x]"), "c"),
        (("☐", "☒"), "c"),
    ],
)
def test_checkbox_glyphs_name_one_provision(marks, expected):
    empty, checked = marks
    text = wingdings_form25("c").replace(" o ", f" {empty} ").replace(" x ", f" {checked} ")
    assert rs.checkbox_rule(text)[0] == expected


def test_checkbox_ambiguity_is_never_resolved():
    two = wingdings_form25("c").replace("o 17 CFR 240.12d2-2(a)(3)", "x 17 CFR 240.12d2-2(a)(3)")
    assert rs.checkbox_rule(two)[0] is None
    none = wingdings_form25("c").replace(" x ", " o ")
    assert rs.checkbox_rule(none)[0] is None


# ---------------------------------------------------------------------- the graph
def test_duplicate_index_copies_are_one_notice(graph):
    (n,) = notice(graph, issuer=f"CIK:{OLDCO}")
    assert n["copies"] == [OLDCO, EXCHANGE]
    assert n["duplicate_events"]
    assert all(EXCHANGE in d for d in n["duplicate_events"])
    assert graph["summary"]["notice_index_copies"] == 6
    assert graph["summary"]["notice_documents"] == 5


def test_a_notice_links_to_a_ticker_by_issuer_class_and_venue(graph):
    (n,) = notice(graph, issuer=f"CIK:{OLDCO}")
    assert (n["ticker"], n["status"], n["confidence"]) == ("ZZ", "CONFIRMED", "HIGH")
    assert n["observed"]["after_filing"] == "ABSENT"
    assert n["method"] == "LISTED_NAME_CLASS_VENUE"


def test_a_missing_issuer_cik_is_never_guessed(graph):
    (n,) = [n for n in graph["notices"] if n["method"] == "MISSING_CIK"]
    assert n["issuer"] is None
    assert n["ticker"] is None
    assert n["status"] == "UNRESOLVED"


def test_purchase_rights_are_not_the_common_stock(graph):
    (n,) = notice(graph, issuer=f"CIK:{RIGHTSCO}")
    assert n["class_category"] == "RIGHT"
    assert n["label_note"]
    assert "COMMON" in n["label_note"]
    assert n["ticker"] is None  # RCO is the common stock, not the rights


def test_a_wingdings_rule_is_read_and_a_contradicted_removal_conflicts(graph):
    (n,) = notice(graph, issuer=f"CIK:{NEWCO}")
    assert n["rule_x43"]["rule"] == "c"
    assert n["rule_x43"]["status"] == "CONFIRMED"
    assert n["status"] == "CONFLICTING"  # still listed on NYSE 151 days later
    assert n["observed"]["after_filing"] == "STILL_LISTED"


def test_exchange_transfer_continuity_is_proposed_not_applied(graph):
    (n,) = notice(graph, security_key="SECCLASS:C000000001")
    assert (n["ticker"], n["status"]) == ("FA", "CONFIRMED")
    assert n["observed"]["after_filing"] == "ABSENT"  # gone from the NYSE Arca directory
    (p,) = [p for p in graph["ledger_proposals"] if p["event_type"] == "EXCHANGE_TRANSFER"]
    assert (p["from"], p["to"], p["status"]) == (["FA", "NYSE_ARCA"], ["FA", "NASDAQ"], "CONFIRMED")
    assert (p["not_before"], p["not_after"]) == ("2017-06-08", "2017-09-12")


def test_ticker_reuse_keeps_two_securities(graph):
    edges = {(e["ticker"], e["security_key"]) for e in graph["ticker_edges"]}
    assert ("ZZ", f"CIK:{OLDCO}|ADS") in edges
    assert ("ZZ", f"CIK:{NEWCO}|COMMON") in edges
    (r,) = [r for r in graph["ticker_reuse"] if r["ticker"] == "ZZ"]
    assert [h["holder"] for h in r["holders"]] == [f"CIK:{OLDCO}", f"CIK:{NEWCO}"]
    assert r["status"] == "CONFIRMED"


def test_one_issuer_has_several_securities(graph):
    keys = {e["security_key"] for e in graph["ticker_edges"] if e["entity_key"] == f"CIK:{NEWCO}"}
    assert f"CIK:{NEWCO}|COMMON" in keys
    assert any(k.startswith(f"CIK:{NEWCO}|PREFERRED-") for k in keys)


def test_ambiguous_and_unknown_issuer_names_stay_unresolved(graph):
    loose = {u["ticker"]: u for u in graph["unresolved_listings"]}
    assert loose["SNC"]["method"] == "EDGAR_NAME_AMBIGUOUS"
    assert set(loose["SNC"]["candidates"]) == {f"CIK:{SAME_A}", f"CIK:{SAME_B}"}
    assert loose["NOB"]["method"] == "NO_EDGAR_NAME"
    # Inc. and L.P. share the stripped name; the exact legal name decides.
    (twn,) = [e for e in graph["ticker_edges"] if e["ticker"] == "TWN"]
    assert twn["entity_key"] == f"CIK:{TWIN_A}"


def test_provider_history_is_linked_by_prices_and_contamination_is_caught(graph):
    (p,) = graph["provider_links"]
    assert p["formations"][0]["tickers"] == ["ZZ"]
    assert p["entity_key"] == f"CIK:{OLDCO}"  # the formation-era holder, not ZZ's later one
    assert p["status"] == "CONFLICTING"
    (f,) = [f for f in p["findings"] if f["kind"] == "BARS_AFTER_SECURITY_END"]
    assert f["first_bar_after"] == LATER_DAYS[0]
    assert any(f["kind"] == "PRICE_JUMP" for f in p["findings"])


def test_holder_is_never_projected_past_the_evidence(graph, stores):
    s, _ = stores
    obs = rs.observations(s)
    rs.resolve_observations(obs, rs.edgar(s), lc.fund_classes(s))
    ix = rs.Index.of(obs)
    assert rs.holder(ix, "ZZ", date(2017, 1, 3))["entity"] == f"CIK:{OLDCO}"
    later = rs.holder(ix, "ZZ", date(2019, 6, 3))
    assert later["status"] == "UNRESOLVED"
    assert later["entity"] is None
    between = rs.holder(ix, "ZZ", date(2018, 12, 20))  # OLDCO before, NEWCO after
    assert between["status"] == "UNRESOLVED"
    assert "changed issuer" in between["explanation"]


def test_the_graph_is_deterministic_and_written_once(stores, tmp_path):
    s, s43 = stores
    ledger = lc.build(s)
    a = rs.build(s, s43, ledger, ledger_sha256="fixture")
    b = rs.build(x4.Store(s.root), x43.Store(s43.root), lc.build(s), ledger_sha256="fixture")
    assert a["content_sha256"] == b["content_sha256"]
    path = tmp_path / "graph.json.gz"
    sha = rs.write(a, path)
    assert len(sha) == 64
    assert x4.read_json(path)["content_sha256"] == a["content_sha256"]
    with pytest.raises(FileExistsError):
        rs.write(a, path)


def test_the_ledger_must_be_the_x42_file(tmp_path):
    p = tmp_path / "ledger.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match=r"is not the X4.2 ledger"):
        rs.load_ledger(p)


def test_evidence_stores_are_separate_and_verified(stores):
    _, s43 = stores
    assert json.loads(s43.manifest_path.read_text())["decision"] == "D-247"
    assert x43.verify(s43)["ok"]
    s43.path(f"db/provider_bars/{ASSET[0]}.json.gz").write_bytes(b"tampered")
    assert not x43.verify(s43)["ok"]


def test_probe_summary_locates_mapping_segment_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(x43, "PROBE_TICKERS", ("AA", "BB"))
    s43 = x43.Store(tmp_path / "x43")
    days = weekdays(date(2017, 6, 19), 10)

    def probe(bars):
        rows = {t: [{"t": f"{d}T04:00:00Z", "c": c} for d, c in v] for t, v in bars.items()}
        return {"pages": [{"body": {"bars": rows}}]}

    early = {"AA": [(d, 1.0 + i) for i, d in enumerate(days[:5])],
             "BB": [(d, 5.0) for d in days[:5]]}  # fmt: skip
    late = {"AA": [(d, 1.0 + i) for i, d in enumerate(days)],
            "BB": [(d, 5.0) for d in days[7:]]}  # fmt: skip
    s43.put_json("provider/asof/2017-06-23.json.gz", probe(early), {"kind": "fixture"})
    s43.put_json("provider/asof/2017-06-26.json.gz", probe(late), {"kind": "fixture"})
    out = rs.probe_summary(s43)
    (aa,) = out["AA"]["segment_changes"]
    assert aa["earlier_mapping_ends"] == days[4]
    assert aa["common_sessions"] == 5
    assert aa["identical_on_common"] is True
    starts, ends = out["BB"]["segment_changes"]
    assert starts["later_mapping_starts"] == days[7]
    assert starts["identical_on_common"] is None  # no overlap: nothing to compare
    assert ends["earlier_mapping_ends"] == days[4]
