"""Phase 11A evidence program against PostgreSQL.

Price series are the DETERMINISTIC TEST FIXTURE bars of ``validation_support`` and the MOCK
quotes and trades of ``simulation_support``; the paper position is a simulated position of a
CONTROLLED_TEST account. Nothing here is market data, a strategy result or a profit claim.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError

from app.backtesting.data import sessions_between
from app.database.models import PortfolioAccount
from app.database.models.evidence import (
    PORTFOLIO,
    EvidenceProtocolRecord,
    EvidenceScorecardEntryRecord,
)
from app.database.models.learning import (
    ValidationExperimentRecord,
    ValidationFoldRecord,
    ValidationRobustnessRecord,
    ValidationRunRecord,
)
from app.database.sessions import session_scope
from app.evidence import paper as pp
from app.evidence import service as evidence
from app.evidence.data_audit import AuditScope
from app.evidence.enums import AuditStatus, Classification, ReadinessVerdict
from app.evidence.protocol import EvidenceCriteria
from app.learning.enums import (
    ExperimentKind,
    ResultStatus,
    RobustnessMethod,
    RunStatus,
    SearchMethod,
)
from app.learning.walkforward import ExperimentConfig
from app.main import create_app
from app.tests.integration.backtest_support import make_traded_asset, seed_series
from app.tests.integration.simulation_support import SESSION, D, make_simulation
from app.tests.integration.validation_support import (
    END,
    HISTORY_FIRST,
    START,
    ValidationLab,
    experiment,
    make_validation_lab,
    module_engine,
    wave,
)

pytestmark = pytest.mark.integration
OP = "operator:test"
NOW = datetime(2026, 11, 12, 18, tzinfo=UTC)


@pytest.fixture(scope="module")
def engine_11a(server_url: URL, template_database: str) -> Iterator[Engine]:
    yield from module_engine(server_url, template_database)


@pytest.fixture(scope="module")
def lab(engine_11a: Engine) -> ValidationLab:
    return make_validation_lab(engine_11a)


@pytest.fixture(scope="module")
def protocol_id(lab: ValidationLab) -> uuid.UUID:
    with session_scope(lab.bench.factory) as session:
        row = evidence.register_protocol(
            session,
            EvidenceCriteria(),
            name="test-protocol",
            rationale="TEST FIXTURE protocol",
            actor=OP,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
        return row.id


@pytest.fixture(scope="module")
def run_id(lab: ValidationLab, protocol_id: uuid.UUID) -> uuid.UUID:
    rid = lab.ctx.service.create(ExperimentConfig.model_validate(experiment()), actor=OP)
    assert lab.ctx.service.execute(rid) is RunStatus.COMPLETED
    return rid


# ---------------------------------------------------------------------- protocols
def test_protocols_are_operator_only_versioned_unique_and_immutable(
    lab: ValidationLab, protocol_id: uuid.UUID
) -> None:
    f = lab.bench.factory
    with pytest.raises(evidence.EvidenceError, match="only an operator"), session_scope(f) as s:
        evidence.register_protocol(
            s, EvidenceCriteria(), name="x", rationale="by the system", actor="system", now=NOW
        )
    with pytest.raises(evidence.EvidenceError, match="already registered"), session_scope(f) as s:
        evidence.register_protocol(
            s, EvidenceCriteria(), name="test-protocol", rationale="same again", actor=OP, now=NOW
        )
    stricter = EvidenceCriteria.model_validate({"sample": {"min_oos_trades": 50}})
    with session_scope(f) as s:
        v2 = evidence.register_protocol(
            s, stricter, name="test-protocol", rationale="a stricter version", actor=OP, now=NOW
        )
        assert v2.version == 2
        assert evidence.criteria_of(v2) == stricter
    for sql in (
        "UPDATE evidence_protocols SET rationale = 'changed' WHERE id = :id",
        "DELETE FROM evidence_protocols WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), session_scope(f) as s:
            s.execute(text(sql), {"id": protocol_id})
    with pytest.raises(DBAPIError), session_scope(f) as s:
        s.add(
            EvidenceProtocolRecord(
                name="direct",
                version=1,
                criteria={},
                criteria_sha256="a" * 64,
                rationale="inserted directly",
                registered_by="system",  # not an operator: refused by the database
                registered_at=NOW,
            )
        )


# ---------------------------------------------------------------------- validation records
def test_folds_keep_their_out_of_sample_trades_and_reproduction_is_unchanged(
    lab: ValidationLab, run_id: uuid.UUID
) -> None:
    with session_scope(lab.bench.factory) as session:
        folds = session.scalars(
            select(ValidationFoldRecord).where(ValidationFoldRecord.run_id == run_id)
        ).all()
        assert folds
        for fold in folds:
            oos = fold.out_of_sample
            rows = oos["trades"]
            assert len(rows) == oos["trade_statistics"]["trades"]
            assert sum(r["net_pnl"] for r in rows) == pytest.approx(
                oos["trade_statistics"]["net_pnl"], abs=1e-6
            )
            for r in rows:
                assert fold.test_start.isoformat() <= r["first_session"]
                assert r["last_session"] <= fold.test_end.isoformat()
                assert r["return"] == pytest.approx(r["net_pnl"] / r["entry_value"])
    copy = lab.ctx.service.reproduce(run_id, actor=OP)
    with session_scope(lab.bench.factory) as session:
        reproduced = session.get(ValidationRunRecord, copy)
        assert reproduced is not None
        assert reproduced.reproduced_identical is True


# ---------------------------------------------------------------------- data audit
def test_the_data_audit_finds_gaps_and_never_claims_survivorship_is_absent(
    lab: ValidationLab,
) -> None:
    f = lab.bench.factory
    gappy = make_traded_asset(f, "FIXG")
    seed_series(
        f,
        gappy,
        lambda i, d: None if i % 9 == 4 else wave(i, d),  # every ninth session missing
        first=HISTORY_FIRST,
        last=END,
    )
    scope = AuditScope(
        symbols=("FIXA",),
        start=START,
        end=END,
        benchmark="FIXB",
        provider="alpaca",
        feed="iex",
        known_at=NOW,
        universe_selected_on=END,
    )
    with session_scope(f) as session:
        clean = evidence.run_audit(session, scope, actor=OP, now=NOW)
        assert clean.status is AuditStatus.WARN  # survivorship is always reported
        r = clean.result
        assert r["failures"] == []
        assert r["symbols"]["FIXA"]["missing_raw_sessions"] == []
        assert r["symbols"]["FIXA"]["expected_sessions"] == len(sessions_between(START, END))
        assert r["survivorship"]["bias"] == "PRESENT"
        assert r["calendar"]["sessions_without_benchmark_bar"] == []
        bad = evidence.run_audit(
            session,
            AuditScope(**{**scope.__dict__, "symbols": ("FIXA", "FIXG")}),
            actor=OP,
            now=NOW,
        )
        assert bad.status is AuditStatus.FAIL
        assert any(x.startswith("FIXG:") for x in bad.result["failures"])
    with pytest.raises(DBAPIError), session_scope(f) as session:
        session.execute(text("DELETE FROM evidence_data_audits WHERE id = :id"), {"id": clean.id})


# ---------------------------------------------------------------------- scorecard
def test_a_scorecard_keeps_the_sections_apart_and_fixture_evidence_is_insufficient(
    lab: ValidationLab, run_id: uuid.UUID, protocol_id: uuid.UUID
) -> None:
    lab.ctx.service.evaluate_holdout(run_id, actor=OP)
    f = lab.bench.factory
    with session_scope(f) as session:
        card = evidence.build_scorecard(
            session,
            evidence.ScorecardRequest(protocol_id, {"fixture_proposer": run_id}),
            actor=OP,
            now=NOW,
        )
        assert card.verdict is ReadinessVerdict.NOT_READY
        result = card.result
        s = result["strategies"]["fixture_proposer"]
        # A few months of fixture bars cannot meet a one-year out-of-sample minimum.
        assert s["classification"] == Classification.INSUFFICIENT_EVIDENCE.value
        assert s["backtest"]["note"].startswith("in-sample")
        assert s["out_of_sample"]["run"]["run_id"] == str(run_id)
        assert s["out_of_sample"]["holdout"]["access_number"] == 1
        assert s["out_of_sample"]["baselines"]["cash_return"] == 0.0
        assert s["out_of_sample"]["run"]["per_trade_returns_stored"] in (True, False)
        assert s["paper"] is None  # no account named: no paper section, never a backtest
        codes = {c["code"]: c for c in s["criteria"]}
        assert codes["OOS_SESSIONS"]["status"] == "INSUFFICIENT"
        assert codes["PAPER_EVIDENCE"]["status"] == "NOT_AVAILABLE"
        assert result["portfolio"]["classification"] != Classification.CANDIDATE_FOR_REVIEW.value
        entries = {
            e.subject: e.classification
            for e in session.scalars(
                select(EvidenceScorecardEntryRecord).where(
                    EvidenceScorecardEntryRecord.scorecard_id == card.id
                )
            )
        }
        assert entries["fixture_proposer"] is Classification.INSUFFICIENT_EVIDENCE
        assert PORTFOLIO in entries
        found = evidence.latest_entry(session, "fixture_proposer")
        assert found is not None
        assert found[1].id == card.id
        card_id = card.id
    with pytest.raises(DBAPIError), session_scope(f) as session:
        session.execute(
            text("UPDATE evidence_scorecards SET verdict = 'READY_FOR_REVIEW' WHERE id = :id"),
            {"id": card_id},
        )
    with pytest.raises(DBAPIError), session_scope(f) as session:
        session.execute(
            text(
                "UPDATE evidence_scorecard_entries SET classification = 'CANDIDATE_FOR_REVIEW' "
                "WHERE scorecard_id = :id"
            ),
            {"id": card_id},
        )


# ---------------------------------------------------------------------- paper evidence
def test_paper_evidence_comes_from_the_simulated_positions_and_their_fills(
    fresh_engine: Engine,
) -> None:
    """A real 7B lifecycle (entry, take-profit exit, reconciliation) on MOCK quotes."""
    sim = make_simulation(fresh_engine)
    ex = sim.ex
    account = ex.account("evidence-paper")
    asset = ex.asset("EVP")
    with session_scope(sim.factory) as session:
        proto = evidence.register_protocol(
            session,
            EvidenceCriteria(),
            name="paper-test",
            rationale="TEST FIXTURE protocol",
            actor=OP,
            now=SESSION - timedelta(days=1),
        )
        proto_id = proto.id
    r = ex.reservation(account, asset, "evidence-res-1", target=D("101.00"))
    entry = ex.open_order(account, r, "10", "evidence-order-1")
    sim.at(SESSION + timedelta(minutes=1))
    sim.quote(asset, "99.50", "99.55", at=SESSION + timedelta(seconds=50), ask_size="100")
    sim.simulate()
    ex.dispatch()
    sim.monitor_once()
    ex.dispatch()
    sim.later(30)
    sim.trade(asset, "101.02", "200")
    sim.later(1)
    sim.simulate()
    position = sim.position(entry)
    assert position is not None
    assert position.status.value == "CLOSED"
    ex.dispatch()
    assert ex.risk.reconcile(account) == "PASSED"
    until = sim.clock.now() + timedelta(minutes=5)

    with session_scope(sim.factory) as session:
        trades = pp.closed_trades(session, account, SESSION - timedelta(days=1), until)
        assert len(trades) == 1
        t = trades[0]
        costs = position.commissions + position.fees + position.borrow_accrued
        assert D(str(t.net_pnl)) == position.realized_pnl - costs
        assert t.exit_reason == "TAKE_PROFIT"
        # Opened before the protocol was registered: not evidence under it.
        assert pp.closed_trades(session, account, SESSION + timedelta(days=1), until) == []
        quality = pp.execution_quality(session, account, None, until)
        row = quality[t.strategy_id]
        assert row["ENTRY_fills"] >= 1
        assert row["EXIT_fills"] >= 1
        assert row["entry_vs_arrival_bps"]["n"] >= 1
        assert row["assumed_cost_bps"]["mean"] is not None
        funnel = pp.funnel(session, account, None, until)
        assert funnel["positions_by_status"] == {"CLOSED": 1}
        assert funnel["exits_by_reason"] == {"TAKE_PROFIT": 1}
        rec = pp.reconciliation(session, account)
        assert rec["accounting"]["status"] == "PASSED"
        # Reservation and execution reconciliations were never run here: not "clean".
        assert rec["reservations"] is None
        assert rec["clean"] is None
        name = session.get(PortfolioAccount, account).name  # type: ignore[union-attr]
        card = evidence.build_scorecard(
            session,
            evidence.ScorecardRequest(proto_id, {}, account_name=name),
            actor=OP,
            now=until,
        )
        paper = card.result["strategies"][t.strategy_id]["paper"]
        assert paper["trades"] == 1
        assert card.result["paper_observation"]["closed_trades"] == 1
        # No orchestrator ran: no observed session, so nothing can be concluded.
        assert card.result["paper_observation"]["observed_sessions"] == 0
        assert card.result["strategies"][t.strategy_id]["classification"] == (
            Classification.INSUFFICIENT_EVIDENCE.value
        )
        assert card.verdict is ReadinessVerdict.NOT_READY


# ---------------------------------------------------------------------- API
@pytest.fixture
def api(lab: ValidationLab, make_settings: Any) -> TestClient:
    app = create_app(
        make_settings(operator_token="op-secret"), health_checks={}, validation=lab.ctx
    )
    return TestClient(app)


def test_the_api_reads_freely_and_writes_only_with_an_operator_token(
    api: TestClient, run_id: uuid.UUID, protocol_id: uuid.UUID
) -> None:
    H = {"X-Atlas-Operator-Token": "op-secret"}
    with api:
        assert api.get("/api/v1/evidence/criteria/default").json()["criteria_sha256"]
        body = {
            "name": "api-protocol",
            "rationale": "TEST FIXTURE via the API",
            "criteria": {"sample": {"min_oos_trades": 77}},
        }
        assert api.post("/api/v1/evidence/protocols", json=body).status_code == 401
        created = api.post("/api/v1/evidence/protocols", json=body, headers=H)
        assert created.status_code == 201, created.text
        assert created.json()["registered_by"] == "operator:api"
        again = api.post("/api/v1/evidence/protocols", json=body, headers=H)
        assert again.status_code == 409
        assert any(
            p["name"] == "api-protocol" for p in api.get("/api/v1/evidence/protocols").json()
        )
        audit = api.post(
            "/api/v1/evidence/audits",
            json={
                "symbols": ["FIXA"],
                "start": START.isoformat(),
                "end": END.isoformat(),
                "benchmark": "FIXB",
            },
            headers=H,
        )
        assert audit.status_code == 201, audit.text
        card = api.post(
            "/api/v1/evidence/scorecards",
            json={
                "protocol_id": str(protocol_id),
                "strategy_runs": {"fixture_proposer": str(run_id)},
                "data_audit_id": audit.json()["id"],
            },
            headers=H,
        )
        assert card.status_code == 201, card.text
        assert card.json()["verdict"] == "NOT_READY"
        latest = api.get("/api/v1/evidence/scorecards/latest").json()
        assert latest["id"] == card.json()["id"]
        assert api.get(f"/api/v1/evidence/scorecards/{uuid.uuid4()}").status_code == 404
        assert (
            api.post(
                "/api/v1/evidence/scorecards", json={"protocol_id": str(protocol_id)}
            ).status_code
            == 401
        )


# ---------------------------------------------------------------------- combined runs
def test_a_strategy_is_judged_on_its_stand_alone_replay_inside_a_combined_evaluation(
    lab: ValidationLab, protocol_id: uuid.UUID
) -> None:
    """TEST FIXTURE records of a two-strategy EVALUATION run (inserted directly): only the
    stitched statistics exist, so criteria needing trades, DSR or a holdout stay unavailable
    and nothing better than INSUFFICIENT_EVIDENCE (or a rejection) can come out."""
    f = lab.bench.factory
    individual = {
        "alpha": {
            "sessions": 300,
            "net_return": -0.02,
            "max_drawdown": 0.03,
            "sharpe_annualized": -0.4,
            "trade_statistics": {
                "trades": 60,
                "net_pnl": -2000.0,
                "gross_pnl": -1800.0,
                "costs": 200.0,
                "profit_factor": 0.8,
                "expectancy": -33.3,
                "win_rate": 0.4,
                "top5_share_of_net_profit": None,
            },
        },
        "beta": {
            "sessions": 300,
            "net_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe_annualized": None,
            "trade_statistics": {"trades": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "costs": 0.0},
        },
    }
    with session_scope(f) as s:
        exp = ValidationExperimentRecord(
            name="TEST FIXTURE combined",
            kind=ExperimentKind.EVALUATION,
            config={"fixture": True},
            config_sha256="0" * 64,
            strategy_ids=["alpha", "beta"],
            search_method=SearchMethod.NONE,
            max_trials=1,
            created_by="test",
        )
        s.add(exp)
        s.flush()
        run = ValidationRunRecord(
            experiment_id=exp.id,
            status=RunStatus.QUEUED,
            base_config={"fixture": True},
            base_config_sha256="0" * 64,
            engine_version="test",
            code_version="test",
            seed=0,
            start_date=START,
            end_date=END,
            data_known_at=NOW,
            requested_by="test",
            holdout_start=END,
            holdout_end=END,
        )
        s.add(run)
        s.flush()
        rid = run.id
    with session_scope(f) as s:
        s.execute(
            update(ValidationRunRecord)
            .where(ValidationRunRecord.id == rid)
            .values(status=RunStatus.RUNNING)
        )
    with session_scope(f) as s:
        s.add(
            ValidationRobustnessRecord(
                run_id=rid,
                method=RobustnessMethod.REGIME_SEGMENTATION,
                scope="individual strategies on the same test windows",
                status=ResultStatus.OK,
                result={"individual": individual},
                assumptions=["TEST FIXTURE"],
                limitations=[],
            )
        )
    with session_scope(f) as s:
        s.execute(
            update(ValidationRunRecord)
            .where(ValidationRunRecord.id == rid)
            .values(status=RunStatus.COMPLETED, result_sha256="1" * 64, summary={"fixture": 1})
        )
    with session_scope(f) as s:
        card = evidence.build_scorecard(
            s,
            evidence.ScorecardRequest(protocol_id, {"alpha": rid, "beta": rid}),
            actor=OP,
            now=NOW,
        )
        alpha = card.result["strategies"]["alpha"]
        beta = card.result["strategies"]["beta"]
        assert "stand-alone" in alpha["out_of_sample"]["run"]["source"]
        # 60 losing trades over 300 sessions: a sufficient, negative sample.
        assert alpha["classification"] == Classification.REJECTED.value
        assert beta["classification"] == Classification.INSUFFICIENT_EVIDENCE.value
        codes = {c["code"]: c["status"] for c in beta["criteria"]}
        assert codes["DEFLATED_SHARPE"] == "NOT_AVAILABLE"
        assert codes["HOLDOUT"] == "NOT_AVAILABLE"
    with pytest.raises(evidence.EvidenceError, match="did not evaluate"), session_scope(f) as s:
        evidence.build_scorecard(
            s, evidence.ScorecardRequest(protocol_id, {"gamma": rid}), actor=OP, now=NOW
        )
