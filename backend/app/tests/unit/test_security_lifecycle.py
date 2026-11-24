"""X4.2 (D-246): the security lifecycle ledger, built from a FIXTURE X4.1 store.

Every filing, listing, fund file and submission here is hand-written test data, not real
evidence. Two fixture funds (FA, FB), a fixture trust, and fixture companies OLDCO (ZZ on
Nasdaq, converted by merger) and NEWCO (YY, then ZZ on NYSE) exercise the rules.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import date
from typing import Any

import pytest

from app.research import lifecycle as lc
from app.research import x4_evidence as x4

TRUST, OLDCO, NEWCO, FUNDCO = "0000000101", "0000000202", "0000000303", "0000000404"
CHECKED, EMPTY = "☒", "☐"


def notice_xml(cik: str, name: str, cls: str, rule: str, exchange: str) -> str:
    return (
        f"FILED BY:\n\tCOMPANY DATA:\n\tCOMPANY CONFORMED NAME:\t{exchange}\n<DOCUMENT>"
        f"<notificationOfRemoval><exchange><cik>0000876661</cik><entityName>{exchange}"
        f"</entityName></exchange><issuer><cik>{int(cik)}</cik><entityName>{name}</entityName>"
        f"</issuer><descriptionClassSecurity>{cls}</descriptionClassSecurity><ruleProvision>"
        f"17 CFR 240.12d2-2({rule})</ruleProvision></notificationOfRemoval>"
    )


def issuer_form25(exhibit: list[tuple[str, str]]) -> str:
    rows = " ".join(
        f"{name} NYSE Arca, Inc{dot} 12-345678{i}" for i, (name, dot) in enumerate(exhibit)
    )
    return (
        "<DOCUMENT><html>FORM 25 Issuer: Fixture Trust Exchange: NYSE Arca, Inc. (Exact name of "
        "Issuer as specified) Address (Address) Units of beneficial interest (See exhibit A) "
        f"(Description of class of securities) {EMPTY} 17 CFR 240.12d2-2(a)(1) {EMPTY} "
        f"Pursuant to 17 CFR 240.12d2-2(b) {CHECKED} Pursuant to 17 CFR 240.12d2-2(c), the "
        f"Issuer has complied. Exhibit A Title of each class to be withdrawn Name of Exchange "
        f"IRS Employer Identification No. {rows}</html>"
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
    w.writerow(
        [
            "rep_file_num",
            "CIK",
            "entity_name",
            "entity_org_type",
            "series_id",
            "series_name",
            "class_id",
            "class_name",
            "class_ticker_symbol",
        ]
    )
    for cik, series, cls, ticker in rows:
        w.writerow(
            ["811-1", cik, "Fixture Trust", "30", "S1" + cls[-4:], series, cls, "ETF", ticker]
        )
    return gzip.compress(buf.getvalue().encode())


def submissions(cik: str, name: str, forms: tuple[tuple[str, str], ...] = ()) -> str:
    return json.dumps(
        {
            "cik": cik,
            "url": "fixture",
            "http_status": 200,
            "text": json.dumps(
                {
                    "name": name,
                    "filings": {
                        "recent": {
                            "form": [f for f, _ in forms],
                            "filingDate": [d for _, d in forms],
                        }
                    },
                }
            ),
        }
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = x4.Store(tmp_path / "x4")
    notices = [
        {
            "form": "25",
            "company": "FIXTURE TRUST",
            "cik": TRUST,
            "filed": "2017-07-26",
            "path": "edgar/data/101/a.txt",
            "text": issuer_form25([("Fund A ETF", "."), ("Fund B ETF", "")]),
        },
        {
            "form": "25-NSE",
            "company": "OLDCO",
            "cik": OLDCO,
            "filed": "2018-12-31",
            "path": "edgar/data/202/b.txt",
            "text": notice_xml(
                OLDCO, "OLDCO", "American Depositary Shares", "a)(3", "The Nasdaq Stock Market LLC"
            ),
        },
        # The same notice indexed twice: one event, not two.
        {
            "form": "25-NSE",
            "company": "OLDCO",
            "cik": OLDCO,
            "filed": "2018-12-31",
            "path": "edgar/data/202/b.txt",
            "text": notice_xml(
                OLDCO, "OLDCO", "American Depositary Shares", "a)(3", "The Nasdaq Stock Market LLC"
            ),
        },
        {
            "form": "25-NSE",
            "company": "FUNDCO",
            "cik": FUNDCO,
            "filed": "2018-06-01",
            "path": "edgar/data/404/c.txt",
            "text": notice_xml(FUNDCO, "FUNDCO", "Fund C ETF", "a)(2", "NYSE ARCA, INC."),
        },
        {
            "form": "25-NSE",
            "company": "NEWCO",
            "cik": NEWCO,
            "filed": "2018-03-01",
            "path": "edgar/data/303/d.txt",
            "text": notice_xml(NEWCO, "NEWCO", "Common Stock", "b", "NEW YORK STOCK EXCHANGE LLC"),
        },
    ]
    s.put(
        "sec/form25/2017Q3.jsonl.gz",
        gzip.compress("\n".join(json.dumps(n) for n in notices).encode()),
        {"kind": "fixture"},
    )
    s.put(
        "sec/funds/investment_company_series_class_2017.csv.gz",
        fund_file(
            [
                (TRUST, "Fund A ETF", "C000000001", "FA"),
                (TRUST, "Fund B ETF", "C000000002", "FB"),
                (FUNDCO, "Fund C ETF", "C000000003", "FC"),
            ]
        ),
        {"kind": "fixture"},
    )
    caps = {
        ("symdir_otherlisted", "20170608000000"): symdir(
            "otherlisted",
            [
                ("FA", "Fund A ETF", "P", "Y"),
                ("FB", "Fund B ETF", "P", "Y"),
                ("YY", "Newco Inc Common Stock", "N", "N"),
            ],
        ),
        ("symdir_nasdaqlisted", "20170912000000"): symdir(
            "nasdaqlisted",
            [("FA", "Fund A ETF", "", "Y"), ("ZZ", "Oldco - American Depositary Shares", "", "N")],
        ),
        ("symdir_otherlisted", "20171129000000"): symdir(
            "otherlisted",
            [("FB", "Fund B ETF", "Z", "Y"), ("YY", "Newco Inc Common Stock", "N", "N")],
        ),
        ("symdir_nasdaqlisted", "20181123000000"): symdir(
            "nasdaqlisted", [("ZZ", "Oldco - American Depositary Shares", "", "N")]
        ),
        ("symdir_nasdaqlisted", "20190107000000"): symdir("nasdaqlisted", []),
        ("symdir_otherlisted", "20190105000000"): symdir(
            "otherlisted", [("ZZ", "Newco Inc Common Stock", "N", "N")]
        ),
        # A secondary list contradicting the directory: ZZ on Nasdaq for Newco.
        ("companylist_nasdaq", "20190104000000"): b'"Symbol","Name"\n"ZZ","Newco Inc"\n',
    }
    for (family, ts), data in caps.items():
        s.put(
            f"archive/{family}/{ts}.raw",
            data,
            {"kind": "fixture", "timestamp": ts},
            http_status=200,
            digest_verified=True,
            brackets=[],
        )
    subs = [
        submissions(OLDCO, "OLDCO PLC", (("15-12G", "2019-01-10"),)),
        submissions(NEWCO, "NEWCO INC"),
    ]
    s.put(
        "sec/submissions/bundle_000.jsonl.gz",
        gzip.compress("\n".join(subs).encode()),
        {"kind": "fixture"},
    )
    s.save()
    monkeypatch.setattr(
        lc,
        "NAMED",
        (
            lc.Named("ZZ", OLDCO, "ADS", "oldco", "fixture"),
            lc.Named("YY", NEWCO, "COMMON", "newco inc", "fixture"),
            lc.Named("ZZ", NEWCO, "COMMON", "newco inc", "fixture"),
        ),
    )
    monkeypatch.setattr(lc, "ISHARES_2017", ("FA", "FB"))
    monkeypatch.setattr(lc, "ISHARES_TRUSTS", (TRUST,))
    return s


def by(doc, **kw):
    return [e for e in doc["events"] if all(e.get(k) == v for k, v in kw.items())]


def test_an_exchange_transfer_keeps_the_security(store):
    doc = lc.build(store)
    (t,) = by(doc, event_type="EXCHANGE_TRANSFER", ticker="FA")
    assert (t["from_venue"], t["to_venue"]) == ("NYSE_ARCA", "NASDAQ")
    assert t["resolution_status"] == "CONFIRMED"
    assert t["confidence"] == "HIGH"  # the trust's exhibit names Fund A exactly
    assert t["effective_precision"] == "BOUNDED"
    assert (t["not_before"], t["not_after"], t["filing_date"]) == (
        "2017-06-08",
        "2017-09-12",
        "2017-07-26",
    )
    assert t["effective_date"] is None  # never fabricated
    tl = lc.reconstruct(doc, "SECCLASS:C000000001")
    assert tl["ended"] is None
    assert tl["venues"] == ["NASDAQ", "NYSE_ARCA"]
    # Exhibit typed "Inc" without its period: still parsed as its own row.
    (fb,) = by(doc, event_type="EXCHANGE_TRANSFER", ticker="FB")
    assert fb["to_venue"] == "CBOE_BZX"
    assert fb["confidence"] == "HIGH"


def test_a_voluntary_or_exchange_removal_alone_ends_nothing(store):
    doc = lc.build(store)
    key = f"CIK:{NEWCO}|COMMON"
    removal = by(doc, event_type="VENUE_REMOVAL", security_key=key)
    assert removal[0]["reason"] == "EXCHANGE_INITIATED_REMOVAL"
    assert not by(doc, event_type="DELISTING", security_key=key)
    assert removal[0]["effective_precision"] == "UNKNOWN"
    assert removal[0]["not_before"] is None
    assert removal[0]["not_after"] is None


def test_a_conversion_ends_the_class_with_bounds_not_a_made_up_date(store):
    doc = lc.build(store)
    key = f"CIK:{OLDCO}|ADS"
    (end,) = by(doc, event_type="DELISTING", security_key=key)
    assert end["reason"] == "CLASS_CONVERTED_BY_OPERATION_OF_LAW"
    assert end["filing_date"] == "2018-12-31"
    assert end["effective_date"] is None
    assert (end["not_before"], end["not_after"]) == ("2018-11-23", "2019-01-07")
    (merger,) = by(doc, event_type="MERGER_ACQUISITION", security_key=key)
    assert merger["resolution_status"] == "UNRESOLVED"  # no business-combination filing
    assert merger["counterparty_entity_key"] is None


def test_ticker_reuse_never_merges_two_securities(store):
    doc = lc.build(store)
    old = lc.reconstruct(doc, f"CIK:{OLDCO}|ADS")
    new = lc.reconstruct(doc, f"CIK:{NEWCO}|COMMON")
    assert old["tickers"] == ["ZZ"]
    assert new["tickers"] == ["YY", "ZZ"]
    assert old["ended"] is not None
    assert new["ended"] is None
    (change,) = by(doc, event_type="TICKER_CHANGE")
    assert (change["from_ticker"], change["to_ticker"]) == ("YY", "ZZ")
    assert change["security_key"] == f"CIK:{NEWCO}|COMMON"
    assert (change["not_before"], change["not_after"]) == ("2017-11-29", "2019-01-05")


def test_conflicting_lower_source_is_marked_not_dropped(store):
    doc = lc.build(store)
    (c,) = by(
        doc,
        event_type="LISTING_OBSERVED",
        ticker="ZZ",
        venue="NASDAQ",
        security_key=f"CIK:{NEWCO}|COMMON",
    )
    assert c["resolution_status"] == "CONFLICTING"
    assert "symbol directory takes precedence" in c["explanation"]


def test_duplicate_notices_make_one_event_with_their_evidence(store):
    doc = lc.build(store)
    key = f"CIK:{OLDCO}|ADS"
    assert len(by(doc, event_type="VENUE_REMOVAL", security_key=key)) == 1
    assert len(by(doc, event_type="DELISTING", security_key=key)) == 1


def test_a_registered_fund_class_retired_is_a_fund_termination(store):
    doc = lc.build(store)
    (t,) = by(doc, event_type="FUND_TERMINATION")
    assert t["security_key"] == "SECCLASS:C000000003"
    assert t["reason"] == "CLASS_REDEEMED_OR_RETIRED"


def test_issuer_and_security_identities_are_separate(store):
    doc = lc.build(store)
    keys = {s["security_key"]: s for s in doc["securities"]}
    assert keys[f"CIK:{OLDCO}|ADS"]["entity_key"] == f"CIK:{OLDCO}"
    assert keys["SECCLASS:C000000001"]["entity_key"] == f"CIK:{TRUST}"
    assert keys["SECCLASS:C000000001"]["sec_class_id"] == "C000000001"
    assert lc.class_label("Class A Common Stock, par value $0.001") == "CLASS_A_COMMON"
    assert lc.class_label("Common Stock") == "COMMON"
    assert lc.class_label("American Depositary Shares each representing one") == "ADS"
    assert lc.class_label("5.25% Notes due 2025").startswith("CLASS-")


def test_the_ledger_is_deterministic_and_its_hash_order_free(store):
    a, b = lc.build(store), lc.build(store)
    assert a == b
    assert lc.content_sha256(list(reversed(a["events"]))) == a["content_sha256"]


def test_temporal_validation_catches_bad_dates_and_late_listings():
    sec: dict[str, dict[str, Any]] = {"S": {}}
    base = {
        "security_key": "S",
        "effective_date": None,
        "effective_precision": "UNKNOWN",
        "not_before": None,
        "not_after": None,
        "filing_date": None,
        "ticker": "X",
        "resolution_status": "CONFIRMED",
    }
    events: list[dict[str, Any]] = [
        {
            **base,
            "code": "bad-bounds",
            "event_type": "EXCHANGE_TRANSFER",
            "effective_precision": "BOUNDED",
            "not_before": date(2018, 2, 1),
            "not_after": date(2018, 1, 1),
        },
        {
            **base,
            "code": "exact-no-date",
            "event_type": "LISTING_OBSERVED",
            "effective_precision": "EXACT",
        },
        {**base, "code": "end", "event_type": "DELISTING", "filing_date": date(2018, 1, 1)},
        {
            **base,
            "code": "late",
            "event_type": "LISTING_OBSERVED",
            "effective_precision": "EXACT",
            "effective_date": date(2018, 6, 1),
        },
    ]
    issues = {i["code"]: i for i in lc.validate(events, sec)}
    assert "bounds out of order" in issues["bad-bounds"]["issue"]
    assert "exact without a date" in issues["exact-no-date"]["issue"]
    assert issues["late"]["mark"] == "CONFLICTING"


def test_a_ledger_file_is_never_replaced(tmp_path, store):
    doc = lc.build(store)
    path = tmp_path / "ledger.json"
    sha = lc.write(doc, path)
    assert lc.write(doc, path) == sha  # an identical rebuild is accepted
    with pytest.raises(FileExistsError):
        lc.write({**doc, "content_sha256": "0" * 64}, path)
