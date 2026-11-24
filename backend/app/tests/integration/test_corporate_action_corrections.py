"""D-243: corporate-action correction records and sets, and their use by the replay.

Bars, dividends, identities and correction records are TEST FIXTURES, not market or
provider data. ``OLDA`` plays a former ticker of FIXA.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.backtesting import corrections
from app.backtesting.data import sessions_between
from app.backtesting.engine import BacktestError
from app.backtesting.service import BacktestRequestError
from app.database.models import (
    BacktestJournalEntryRecord,
    BacktestTradeRecord,
    CorporateActionEventRecord,
    CorporateActionSetRecord,
)
from app.database.sessions import session_scope
from app.research import ca_dividends, dividend_completeness
from app.research.exclusions import sha256_file
from app.tests.integration.backtest_support import (
    RUN_END,
    RUN_START,
    D,
    make_bench,
    make_traded_asset,
    rows,
    seed_series,
    trend,
)
from app.tests.integration.test_backtest_engine_versions import seed_dividends, summary
from app.tests.integration.test_backtest_exclusion_windows import (
    CONTAM,
    exclude_window,
    halved,
    research_request,
    seed_universe,
)
from app.tests.integration.test_backtest_live_pipeline import WIDE, completed

pytestmark = pytest.mark.integration

RUN_DAYS = [s.day for s in sessions_between(RUN_START, RUN_END)]
KEEP_DAY = RUN_DAYS[6]
SET = "CA_FIXTURE_V1"


def event(
    asset_id: uuid.UUID,
    code: str,
    event_type: str,
    *,
    ex: date | None = None,
    rate: str | None = None,
    pid: str | None = None,
    ticker: str = "OLDA",
    status: str = "VERIFIED",
    **extra: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "code": code,
        "asset_id": asset_id,
        "asset_label": "FIXA",
        "event_type": event_type,
        "provider": "alpaca",
        "provider_ticker": ticker,
        "provider_action_id": pid,
        "ex_date": ex,
        "cash_amount": None if rate is None else Decimal(rate),
        "special": None if ex is None else False,
        "foreign": None if ex is None else False,
        "known_on": ex,
        "verification_status": status,
        "evidence_sha256": "6" * 64,
        "created_by": "test",
    }
    values.update(extra)
    return values


def register_set(factory: Any, records: list[dict[str, Any]], code: str = SET) -> str:
    """FIXTURE correction set: every record stored, the VERIFIED ones as members."""
    with session_scope(factory) as session:
        rows_ = [CorporateActionEventRecord(**r) for r in records]
        session.add_all(rows_)
        session.flush()
        members = [r for r in rows_ if r.verification_status == "VERIFIED"]
        sha = corrections.content_sha256([corrections.event_content(r) for r in members])
        session.add(
            CorporateActionSetRecord(
                code=code,
                version=1,
                scope="DIVIDEND_IDENTITY",
                description="fixture",
                members=[{"code": r.code, "revision": r.revision} for r in members],
                content_sha256=sha,
                preregistration_code="PREREG_FIXTURE",
                document_sha256="7" * 64,
                evidence_sha256="6" * 64,
                created_by="test",
            )
        )
    return sha


def renamed_fixture(factory: Any, asset: uuid.UUID) -> list[dict[str, Any]]:
    """FIXA's own provider dividends (0.05 every session) belong to another security except
    on KEEP_DAY (voided); its real dividends (0.07) were filed under OLDA. On KEEP_DAY a
    record repeats the own dividend's terms under another id (a duplicate)."""
    out = [
        event(
            asset,
            "TS-FIXA-OLDA",
            "TICKER_SUCCESSION",
            valid_from=RUN_DAYS[0],
            valid_to=RUN_DAYS[-1],
        )
    ]
    for day in RUN_DAYS:
        if day == KEEP_DAY:
            out.append(
                event(asset, f"DIV-dup-{day}", "CASH_DIVIDEND", ex=day, rate="0.05", pid="dup")
            )
            continue
        out.append(
            event(
                asset,
                f"VOID-{day}",
                "CASH_DIVIDEND_VOID",
                ex=day,
                rate="0.05",
                pid=f"fixture-FIXA-{day.isoformat()}",
                ticker="FIXA",
            )
        )
        out.append(
            event(
                asset,
                f"DIV-{day}",
                "CASH_DIVIDEND",
                ex=day,
                rate="0.07",
                pid=f"old-{day}",
                link_code="TS-FIXA-OLDA",
            )
        )
    # Never applied: not VERIFIED, so not a set member.
    out.append(
        event(
            asset,
            "DIV-unresolved",
            "CASH_DIVIDEND",
            ex=RUN_DAYS[3],
            rate="5.00",
            pid="unknown",
            status="UNRESOLVED",
        )
    )
    return out


def test_a_correction_set_credits_former_ticker_dividends_once(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    seed_dividends(bench.factory, asset, "FIXA", D("0.05"))
    sha = register_set(bench.factory, renamed_fixture(bench.factory, asset))

    control = completed(bench, bench.run(bench.request(risk=WIDE)))
    assert "corporate_actions" not in control.config
    run = completed(bench, bench.run(bench.request(risk=WIDE, corporate_actions=SET)))
    assert run.config["corporate_actions"] == {
        "code": SET,
        "content_sha256": sha,
        "policy": "ledger-dividend-identity-v1",
        "preregistration_code": "PREREG_FIXTURE",
    }
    entries = [
        j for j in rows(bench, BacktestJournalEntryRecord, run.id) if j.template == "cash_dividend"
    ]
    assert entries, summary(bench, run.id)
    by_rate = Counter(j.params["rate"] for j in entries)
    assert set(by_rate) <= {"0.07", "0.05"}
    assert "5.00" not in by_rate  # the UNRESOLVED record is never applied
    for j in entries:
        day = date.fromisoformat(j.params["ex_date"])
        if j.params["rate"] == "0.05":
            assert day == KEEP_DAY  # every other own dividend was voided
            assert j.source_ref.endswith(f":dividend:{day}")  # a provider row, no correction
        else:
            assert day != KEEP_DAY
            assert j.source_ref.endswith(f":DIV-{day}")
    # One dividend per session held, never two: the duplicate on KEEP_DAY was skipped.
    per_trade = Counter(j.source_ref.split(":")[1] for j in entries)
    for t in rows(bench, BacktestTradeRecord, run.id):
        assert per_trade.get(str(t.seq), 0) == (t.holding_sessions or 0)
    details = summary(bench, run.id)["dividends"]
    fixed = details["corrections"]
    assert fixed["code"] == SET
    assert fixed["voided"] == len(RUN_DAYS) - 1
    assert fixed["credited_events"] == len(RUN_DAYS) - 1
    assert fixed["duplicates_skipped"] == 1
    assert details["postings"]["from_corrections"] == by_rate["0.07"]


def test_x2_stays_in_force_over_a_correction(fresh_engine: Engine) -> None:
    """A verified correction on a contaminated session releases nothing: the X2 window still
    closes the position before it, so the corrected dividend is never received."""
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    b = make_traded_asset(bench.factory, "FIXB")
    seed_series(bench.factory, a, lambda i, _d: trend(i))
    seed_series(bench.factory, b, halved)
    seed_universe(bench.factory, {"FIXA": a, "FIXB": b})
    exclude_window(bench.factory, b, "FIXB", CONTAM)
    register_set(
        bench.factory,
        [
            event(
                b,
                "DIV-FIXB-CONTAM",
                "CASH_DIVIDEND",
                ex=CONTAM,
                rate="50.00",
                pid="contam",
                asset_label="FIXB",
            )
        ],
    )
    run = completed(bench, bench.run(research_request(bench, corporate_actions=SET)))
    assert run.config["exclusions"]["windows"]
    entries = [
        j for j in rows(bench, BacktestJournalEntryRecord, run.id) if j.template == "cash_dividend"
    ]
    assert not [j for j in entries if j.params["rate"] == "50.00"]
    assert summary(bench, run.id)["dividends"]["corrections"]["credited_events"] == 1


def test_a_set_must_be_registered_hashed_and_wanted(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    seed_series(bench.factory, asset, lambda i, _d: trend(i))
    with pytest.raises(BacktestRequestError, match="not registered"):
        bench.service.create(bench.request(risk=WIDE, corporate_actions=SET), actor="t")
    register_set(
        bench.factory,
        [event(asset, "DIV-1", "CASH_DIVIDEND", ex=RUN_DAYS[2], rate="0.1", pid="p1")],
    )
    with pytest.raises(BacktestRequestError, match="dividends modelled"):
        bench.service.create(
            bench.request(risk=WIDE, corporate_actions=SET, dividends=False), actor="t"
        )
    # A set whose recorded hash does not match its members is refused.
    with session_scope(bench.factory) as session:
        session.add(
            CorporateActionSetRecord(
                code="CA_TAMPERED_V1",
                version=1,
                scope="DIVIDEND_IDENTITY",
                description="fixture",
                members=[{"code": "DIV-1", "revision": 0}],
                content_sha256="0" * 64,
                preregistration_code="P",
                document_sha256="7" * 64,
                evidence_sha256="6" * 64,
                created_by="test",
            )
        )
    with pytest.raises(BacktestRequestError, match="content hash"):
        bench.service.create(
            bench.request(risk=WIDE, corporate_actions="CA_TAMPERED_V1"), actor="t"
        )
    # The replay re-checks: a configuration recording another hash is refused.
    with bench.session() as session:
        config = bench.service.resolve(session, bench.request(risk=WIDE, corporate_actions=SET))
        assert config.corporate_actions is not None
        wrong = config.model_copy(
            update={"corporate_actions": {**config.corporate_actions, "content_sha256": "1" * 64}}
        )
        with pytest.raises(BacktestError, match="hashes to"):
            bench.service.replay(session, wrong)


def test_a_set_known_later_is_not_visible_earlier(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    with bench.session() as session:
        before = session.scalar(text("SELECT now()"))
    sha = register_set(
        bench.factory,
        [event(asset, "DIV-1", "CASH_DIVIDEND", ex=RUN_DAYS[2], rate="0.1", pid="p1")],
    )
    with bench.session() as session:
        with pytest.raises(corrections.CorrectionSetError):
            corrections.load_set(session, SET, known_at=before)
        loaded = corrections.load_set(session, SET, known_at=datetime.now(UTC) + timedelta(1))
    assert loaded.content_sha256 == sha
    assert [d.provider_action_id for d in loaded.dividends()] == ["p1"]


def test_correction_records_are_checked_and_append_only(fresh_engine: Engine) -> None:
    bench = make_bench(fresh_engine)
    a = make_traded_asset(bench.factory, "FIXA")
    bad = (
        event(a, "B1", "CASH_DIVIDEND", ex=None, rate="0.1", pid="x"),  # no ex-date
        event(a, "B2", "CASH_DIVIDEND", ex=RUN_DAYS[1], rate="0", pid="x"),  # zero cash
        event(a, "B3", "CASH_DIVIDEND", ex=RUN_DAYS[1], rate="0.1", pid=None),  # no provider id
        event(a, "B4", "TICKER_SUCCESSION", valid_from=RUN_DAYS[3], valid_to=RUN_DAYS[1]),
        event(a, "B5", "SPIN_OFF", ex=RUN_DAYS[1], rate="0.1", pid="x"),  # not in this scope
        event(a, "B6", "CASH_DIVIDEND", ex=RUN_DAYS[1], rate="0.1", pid="x", status="MAYBE"),
    )
    for values in bad:
        with pytest.raises(IntegrityError), session_scope(bench.factory) as session:
            session.add(CorporateActionEventRecord(**values))
    good = event(a, "G1", "CASH_DIVIDEND", ex=RUN_DAYS[1], rate="0.1", pid="x")
    with session_scope(bench.factory) as session:
        session.add(CorporateActionEventRecord(**good))
    with pytest.raises(IntegrityError), session_scope(bench.factory) as session:
        session.add(CorporateActionEventRecord(**good))  # same code and revision
    with session_scope(bench.factory) as session:  # a new revision is a new record
        session.add(CorporateActionEventRecord(**{**good, "revision": 1, "cash_amount": D("0.2")}))
    register_set(bench.factory, [], code="CA_EMPTY_V1")
    for statement in (
        "UPDATE corporate_action_events SET cash_amount = 9",
        "DELETE FROM corporate_action_events",
        "TRUNCATE corporate_action_events",
        "UPDATE corporate_action_sets SET content_sha256 = 'x'",
        "DELETE FROM corporate_action_sets",
    ):
        with pytest.raises(DBAPIError), session_scope(bench.factory) as session:
            session.execute(text(statement))


def _fixture_evidence(asset: uuid.UUID, close: str | None) -> dict[str, Any]:
    """FIXTURE evidence in the shape ``ca_dividends.build`` writes."""

    def record(kind: str, pid: str, ex: str, rate: str, decision: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "label": "FIXA",
            "asset_id": str(asset),
            "ticker": "OLDA" if kind == "LINK" else "FIXA",
            "source": "fixture",
            "dividend": {
                "provider_action_id": pid,
                "symbol": "OLDA",
                "ex_date": ex,
                "rate": rate,
                "special": False,
                "foreign": False,
                "payable_date": None,
            },
            "provider_raw": {"fixture": True},
            "verification": {
                "identity": "SAME_SECURITY",
                "tolerance": "0.002",
                "sessions": [
                    {"session": "2017-03-01", "asset_raw_close": close, "match": True},
                    {"session": "2017-02-28", "asset_raw_close": close, "match": True},
                ],
            },
            "decision": decision,
            "reason": None,
        }

    return {
        "code": ca_dividends.SET_CODE,
        "tolerance": "0.002",
        "window": {"first_ex": "2016-01-05", "last_ex": "2019-12-31"},
        "links": [{"label": "FIXA", "ticker": "OLDA", "source": "fixture"}],
        "assets": {"FIXA": {"asset_id": str(asset), "identity_sha256": None}},
        "records": [
            record("LINK", "p1", "2017-03-02", "0.25", "CREDIT"),
            record("LINK", "p2", "2017-06-02", "0.25", "UNRESOLVED"),
            record("LINK", "p3", "2017-09-01", "0.25", "REJECTED"),
            record("OWN", "p4", "2017-12-01", "0.30", "VOID"),
            record("OWN", "p5", "2017-12-04", "0.30", "KEEP"),
        ],
    }


def test_registration_is_once_cited_and_checked_against_the_data(
    fresh_engine: Engine, tmp_path: Path
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")  # no 2016-19 bars: closes are None
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_fixture_evidence(asset, None)))
    sha = sha256_file(evidence)
    uncited = tmp_path / "uncited.md"
    uncited.write_text("no hash here")
    with pytest.raises(ValueError, match="does not cite"):
        ca_dividends.register(bench.factory, uncited, evidence, progress=lambda _m: None)
    doc = tmp_path / "doc.md"
    doc.write_text(f"evidence sha256 {sha}")
    # The evidence's RAW closes must still be the stored ones.
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(_fixture_evidence(asset, "10.00")))
    changed_doc = tmp_path / "changed.md"
    changed_doc.write_text(sha256_file(changed))
    with pytest.raises(ValueError, match="RAW close changed"):
        ca_dividends.register(bench.factory, changed_doc, changed, progress=lambda _m: None)

    got = ca_dividends.register(bench.factory, doc, evidence, progress=lambda _m: None)
    assert got["records"] == {
        "CASH_DIVIDEND:REJECTED": 1,
        "CASH_DIVIDEND:UNRESOLVED": 1,
        "CASH_DIVIDEND:VERIFIED": 1,
        "CASH_DIVIDEND_VOID:VERIFIED": 1,
        "TICKER_SUCCESSION:VERIFIED": 1,
    }
    assert got["members"] == 3  # the succession, the credit and the void
    with bench.session() as session:
        loaded = corrections.load_set(
            session, ca_dividends.SET_CODE, known_at=datetime.now(UTC) + timedelta(1)
        )
        stored = session.scalars(select(CorporateActionEventRecord)).all()
    assert loaded.content_sha256 == got["content_sha256"]
    assert sorted((d.action, d.provider_action_id) for d in loaded.dividends()) == [
        ("CASH_DIVIDEND", "p1"),
        ("CASH_DIVIDEND_VOID", "p4"),
    ]
    credit = next(r for r in stored if r.provider_action_id == "p1")
    assert credit.link_code == "TS-FIXA-OLDA"
    assert credit.known_on == date(2017, 3, 2)
    assert credit.provider_ticker == "OLDA"
    with pytest.raises(ValueError, match="already registered"):
        ca_dividends.register(bench.factory, doc, evidence, progress=lambda _m: None)


# ---------------------------------------------------------------- X3.1 (D-244): set V2
def _x31_evidence(asset: uuid.UUID, base_sha: str, close: str | None) -> dict[str, Any]:
    """FIXTURE X3.1 evidence: an own dividend voided, a successor-ticker dividend credited,
    an own dividend left unresolved and one kept (no record)."""
    aid = str(asset)

    def rec(kind: str, decision: str, pid: str, ex: str, ticker: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "asset_id": aid,
            "label": "FIXA",
            "ticker": ticker,
            "source": "fixture",
            "dividend": {
                "provider_action_id": pid, "symbol": ticker, "ex_date": ex, "rate": "0.30",
                "special": False, "foreign": False, "payable_date": None,
            },
            "provider_raw": None,
            "verification": {
                "identity": "FIXTURE",
                "sessions": [{"session": "2018-03-01", "asset_raw_close": close}],
            },
            "decision": decision,
            "reason": None,
        }  # fmt: skip

    return {
        "code": dividend_completeness.SET_CODE,
        "base_set": {"code": SET, "content_sha256": base_sha},
        "tolerance": "0.002",
        "window": {"first_ex": "2016-01-05", "last_ex": "2019-12-31"},
        "calendar": "fixture",
        "assets": {aid: {"label": "FIXA", "identity": {"identity_sha256": None}}},
        "links": [{"asset_id": aid, "ticker": "NEWA", "source": "fixture link"}],
        "records": [
            rec("OWN", "VOID", "x1", "2018-03-02", "FIXA"),
            rec("OWN", "UNRESOLVED", "x2", "2018-06-02", "FIXA"),
            rec("OWN", "KEEP", "x3", "2018-09-02", "FIXA"),
            rec("LINK", "CREDIT", "x4", "2018-12-03", "NEWA"),
        ],
    }


def test_set_v2_holds_every_v1_member_plus_the_new_verified_records(
    fresh_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = make_bench(fresh_engine)
    asset = make_traded_asset(bench.factory, "FIXA")
    base_sha = register_set(bench.factory, renamed_fixture(bench.factory, asset))
    monkeypatch.setattr(dividend_completeness, "BASE_SET", SET)
    monkeypatch.setattr(dividend_completeness, "PREREGISTRATION", "PREREG_X31_FIXTURE")
    evidence = tmp_path / "x31.json"
    evidence.write_text(json.dumps(_x31_evidence(asset, base_sha, None)))
    doc = tmp_path / "x31.md"
    doc.write_text(f"evidence {sha256_file(evidence)}")
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(_x31_evidence(asset, "0" * 64, None)))
    stale_doc = tmp_path / "stale.md"
    stale_doc.write_text(sha256_file(stale))
    with pytest.raises(ValueError, match="changed since the evidence"):
        dividend_completeness.register(bench.factory, stale_doc, stale, progress=lambda _m: None)

    got = dividend_completeness.register(bench.factory, doc, evidence, progress=lambda _m: None)
    assert got["new_records"] == {
        "CASH_DIVIDEND:VERIFIED": 1,
        "CASH_DIVIDEND_VOID:UNRESOLVED": 1,
        "CASH_DIVIDEND_VOID:VERIFIED": 1,
        "TICKER_SUCCESSION:VERIFIED": 1,
    }
    later = datetime.now(UTC) + timedelta(1)
    with bench.session() as session:
        v1 = corrections.load_set(session, SET, known_at=later)
        v2 = corrections.load_set(session, dividend_completeness.SET_CODE, known_at=later)
    assert v1.content_sha256 == base_sha  # the base set is untouched
    assert got["members"] == len(v1.events) + 3 == len(v2.events)
    assert {(e.code, e.revision) for e in v1.events} < {(e.code, e.revision) for e in v2.events}
    new = {d.provider_action_id: d.action for d in v2.dividends()}
    assert new["x1"] == corrections.VOID
    assert new["x4"] == corrections.CREDIT
    assert "x2" not in new  # unresolved: recorded, never applied
    assert "x3" not in new  # kept: nothing to correct
    with pytest.raises(ValueError, match="already registered"):
        dividend_completeness.register(bench.factory, doc, evidence, progress=lambda _m: None)
