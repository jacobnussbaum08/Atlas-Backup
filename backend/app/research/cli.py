"""Research data foundation CLI (D-239): ``python -m app.research.cli <command>``.

Simulation database only (the database name must contain ``sim``). GET-only provider and
SEC requests; credentials are never printed. Commands, in order:

``build-universe`` (first time) · ``correct-symbology`` · ``form-current`` · ``load-bars`` ·
``infer-delistings`` · ``dividends`` · ``lots-missing`` · ``classify`` · ``validate`` ·
``reserve`` · ``preregister CODE DOCUMENT RESERVATION...`` · ``exclusion-evidence ARTIFACT...`` ·
``register-exclusions DOCUMENT EVIDENCE`` (D-240) · ``x2-evidence`` ·
``register-x2 DOCUMENT EVIDENCE`` (D-242) · ``x3-evidence`` · ``register-x3`` (D-243) ·
``x31-calendar`` · ``x31-retrieve`` · ``x31-evidence`` · ``register-x31`` (D-244) (the
remaining commands are the
first build's lot passes and the superseded ETF V2 formation, kept for the record).
Artifacts are never replaced (``_fresh``).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.database.sessions import build_engine, build_session_factory

OUT = Path("config/simulation/runs")


def _fresh(path: Path) -> Path:
    """The path itself, or ``name.2.json``, ``name.3.json``...: artifacts are never replaced."""
    n, candidate = 1, path
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}.{n}{path.suffix}")
    return candidate


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.research.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "build-universe",
        "load-bars",
        "infer-delistings",
        "dividends",
        "lot-evidence",
        "register-lots",
        "classify",
        "validate",
        "reserve",
        "add-etf-v2",
        "lot-evidence-wide",
        "register-lots-wide",
        "correct-symbology",
        "form-current",
        "lots-missing",
        "sample-spreads",
    ):
        sub.add_parser(name)
    pre = sub.add_parser("preregister")
    pre.add_argument("code")
    pre.add_argument("document", type=Path)
    pre.add_argument("reservations", nargs="+")
    ev = sub.add_parser("exclusion-evidence")  # D-240: names of artifacts in OUT to hash
    ev.add_argument("artifacts", nargs="*")
    reg = sub.add_parser("register-exclusions")  # D-240: once; append-only
    reg.add_argument("document", type=Path)
    reg.add_argument("evidence", type=Path)
    sub.add_parser("x2-evidence")  # D-242: from the database and the audit's artifacts
    x3 = sub.add_parser("x3-evidence")  # D-243: dividend identity (GET only; look-ups saved)
    x3.add_argument("--lookups", default="ca_dividend_lookups_1.json")
    x3.add_argument("--seed", default=None)  # an earlier look-ups file whose closes to reuse
    sub.add_parser("x31-calendar")  # D-244: calendar vs provider calendar, SPY, stored bars
    sub.add_parser("x31-retrieve")  # D-244: dividends again in ex-date terms (appends rows)
    x31 = sub.add_parser("x31-evidence")  # D-244: completeness and identity (look-ups saved)
    x31.add_argument("--lookups", default="x31_dividend_lookups_1.json")
    x31.add_argument("--seed", default=None)  # an earlier look-ups file whose closes to reuse
    x31.add_argument("--pages", default="x31_dividend_retrieval_pages.json")
    x4 = sub.add_parser("x4-capture")  # D-245: evidence only (GET; artifacts never replaced)
    x4.add_argument(
        "step",
        choices=[
            "assets",
            "snapshot",
            "archive",
            "sec",
            "sweep",
            "classes",
            "history",
            "dividends",
            "derive",
        ],
    )
    x4.add_argument("--formation", default=None)
    x4.add_argument("--length", type=int, default=None)
    sub.add_parser("x4-verify")  # D-245: hashes and offline re-derivation
    sub.add_parser("x42-build")  # D-246: the lifecycle ledger from the X4.1 evidence (a file)
    x42r = sub.add_parser("x42-register")  # D-246: once, into a database at migration 0028
    x42r.add_argument("ledger", type=Path)
    x43c = sub.add_parser("x43-capture")  # D-247: X4.3 evidence (read-only DB snapshot; GET)
    x43c.add_argument("step", choices=["identities", "asof"])
    sub.add_parser("x43-build")  # D-247: the identity graph from X4.1, X4.2 and X4.3 (a file)
    sub.add_parser("x43-verify")  # D-247: X4.3 evidence hashes; rebuild and compare the graph
    sub.add_parser("x431-build")  # D-248: the lifecycle ledger V2 (a file, written once)
    sub.add_parser("x431-verify")  # D-248: V2 hashes recomputed; V2 rebuilt and compared
    x431r = sub.add_parser("x431-register")  # D-248: once, beside V1, into a database at 0028
    x431r.add_argument("ledger", type=Path)
    sub.add_parser("x432-capture")  # D-249: provider corporate actions (GET; never replaced)
    sub.add_parser("x432-build")  # D-249: the identity graph V2 (a file, written once)
    sub.add_parser("x432-verify")  # D-249: X4.3.2 evidence hashes; graph V2 rebuilt and compared
    sub.add_parser("x44-build")  # D-250: the historical universe (a file, written once)
    sub.add_parser("x44-verify")  # D-250: hashes recomputed; the universe rebuilt and compared
    x45c = sub.add_parser("x45-capture")  # D-251: member bars, corporate actions, recovery (GET)
    x45c.add_argument("step", choices=["bars", "actions", "recovery"])
    sub.add_parser("x45-build")  # D-251: the member dataset (a file, written once)
    sub.add_parser("x45-verify")  # D-251: evidence and dataset hashes; dataset rebuilt
    sub.add_parser("x46-build")  # D-252: research-data validation (a file, written once)
    sub.add_parser("x46-verify")  # D-252: input hashes; validation rebuilt and compared
    x461c = sub.add_parser("x461-capture")  # D-253: exit filings (SEC) and admitted members (GET)
    x461c.add_argument("step", choices=["filings", "bars", "actions"])
    sub.add_parser("x461-build")  # D-253: universe V2, member data V2, validation V2 (written once)
    sub.add_parser("x461-verify")  # D-253: evidence and pins; every revision rebuilt and compared
    x5c = sub.add_parser("x5-capture")  # D-254: SEC filings and provider prices (GET only)
    x5c.add_argument("step", choices=["filings", "prices"])
    sub.add_parser("x5-build")  # D-254: corporate-action ledger and total returns (written once)
    sub.add_parser("x5-verify")  # D-254: pins and evidence; both artifacts rebuilt and compared
    sub.add_parser("x6-build")  # D-255: execution inputs and ledger reconciliation (written once)
    sub.add_parser("x6-verify")  # D-255: X5 and X4.6.1 pins; the artifact rebuilt and compared
    sub.add_parser(
        "x7-capture-spreads"
    )  # D-256: measured SIP spreads 2017-19 (SELECT; written once)
    x7s = sub.add_parser("x7-signals")  # D-256: registered strategies' signals (written once)
    x7s.add_argument("--workers", type=int, default=6)
    sub.add_parser("x7-backtest")  # D-256: every run and scenario from the signals (written once)
    sub.add_parser("x7-verify")  # D-256: pins; signals and results rebuilt and compared
    sub.add_parser("x7v2-classify-funds")  # D-257 C2: fund classification (offline; written once)
    sub.add_parser("x7v2-capture-spreads")  # D-257 C6: spreads V2 (SELECT; identity-checked; once)
    sub.add_parser("x7v2-backtest")  # D-257: the V2 runs (refused unless the freeze is complete)
    sub.add_parser("x7v2-verify")  # D-257: V2 results rebuilt and compared
    sub.add_parser("x7v2-evaluate")  # D-257: the frozen decision rule on the V2 results
    tab = sub.add_parser("x31-table")  # D-244: the asset-by-asset report from the evidence
    tab.add_argument("evidence", type=Path)
    tab.add_argument("out", type=Path)
    reg31 = sub.add_parser("register-x31")  # D-244: once; append-only
    reg31.add_argument("document", type=Path)
    reg31.add_argument("evidence", type=Path)
    reg3 = sub.add_parser("register-x3")  # D-243: once; append-only
    reg3.add_argument("document", type=Path)
    reg3.add_argument("evidence", type=Path)
    reg2 = sub.add_parser("register-x2")  # D-242: once; append-only
    reg2.add_argument("document", type=Path)
    reg2.add_argument("evidence", type=Path)
    args = parser.parse_args(argv)
    settings = get_settings()
    if "sim" not in settings.postgres_db:
        raise SystemExit("research commands run against the simulation database only")
    factory = build_session_factory(build_engine(settings))
    result: object
    if args.command == "build-universe":
        from app.research.universe import build

        result = build(factory)
    elif args.command == "add-etf-v2":
        from app.research.universe import ETF_RULE_V2, add_rule

        result = add_rule(factory, ETF_RULE_V2)
    elif args.command == "correct-symbology":
        from app.research.symbology import correct

        result = correct(factory)
    elif args.command == "form-current":
        from sqlalchemy import select

        from app.database.models import ResearchUniverseRecord
        from app.research.identity import EQUITY_RULE_V4, ETF_RULE_V5, form

        with factory() as session:
            have = set(session.scalars(select(ResearchUniverseRecord.code)))
        formed: dict[str, Any] = {}
        for rule in (EQUITY_RULE_V4, ETF_RULE_V5):  # point-in-time identity (identity.py)
            if rule["code"] not in have:
                formed[rule["code"]] = form(factory, rule)
        result = formed
    elif args.command == "lots-missing":
        from sqlalchemy import select

        from app.database.models import InstrumentSpecRecord
        from app.research.bars import members
        from app.research.lots import gather, register

        with factory() as session:
            have = set(
                session.scalars(
                    select(InstrumentSpecRecord.asset_id).where(
                        InstrumentSpecRecord.created_by == "operator:research-foundation"
                    )
                )
            )
            pending = sorted({s for a, s in members(session, current=True) if a not in have})
        path = _fresh(OUT / "research_lot_evidence_current.json")
        evidence = gather(factory, path, only=pending, minutes=120)
        result = {"evidence": evidence, "registered": register(factory, path)}
    elif args.command == "load-bars":
        from app.research.bars import load

        result = load(factory, _fresh(OUT / "research_bar_manifest.json"))
    elif args.command == "sample-spreads":
        from app.research.spreads_sample import run as sample_spreads

        result = sample_spreads(factory)
    elif args.command == "infer-delistings":
        from app.research.bars import infer_delistings

        result = infer_delistings(factory)
    elif args.command == "dividends":
        from datetime import date

        from app.backtesting.dividends import retrieve
        from app.research.bars import members, request_for

        with factory() as session:
            symbols = sorted({request_for(session, a, s)[0] for a, s in members(session, True)})
        totals: dict[str, int] = {}
        for i in range(0, len(symbols), 50):
            part = retrieve(factory, symbols[i : i + 50], date(2014, 9, 1), date(2019, 12, 31))
            for k, v in part.items():
                totals[k] = totals.get(k, 0) + v
        result = {"symbols": len(symbols), **totals}
    elif args.command == "lot-evidence":
        from app.research.lots import gather

        result = gather(factory, OUT / "research_lot_evidence.json")
    elif args.command == "lot-evidence-wide":
        import json as _json

        from app.research.lots import gather

        first = _json.loads((OUT / "research_lot_evidence.json").read_text())["symbols"]
        pending = [s for s, v in first.items() if v["status"] != "VERIFIED"]
        result = gather(factory, OUT / "research_lot_evidence_wide.json", only=pending, minutes=120)
    elif args.command == "register-lots-wide":
        from app.research.lots import register

        result = register(factory, OUT / "research_lot_evidence_wide.json")
    elif args.command == "register-lots":
        from app.research.lots import register

        result = register(factory, OUT / "research_lot_evidence.json")
    elif args.command == "classify":
        from app.research.classification import classify

        result = classify(factory)
    elif args.command == "validate":
        from app.research.validation import run

        result = run(factory, _fresh(OUT / "research_validation.json"))
    elif args.command == "reserve":
        from app.research.reservations import reserve
        from app.research.validation import dataset_sha256

        with factory() as session:
            sha = dataset_sha256(session)
        result = {"dataset_sha256": sha, "added": reserve(factory, sha)}
    elif args.command == "exclusion-evidence":
        from app.research import exclusions

        artifacts = {
            name: {"path": (OUT / name).as_posix(), "sha256": exclusions.sha256_file(OUT / name)}
            for name in args.artifacts
        }
        result = exclusions.evidence(
            factory,
            _fresh(OUT / "research_exclusions_x1_evidence.json"),
            extra={"validated_artifacts": artifacts},
        )
    elif args.command == "register-exclusions":
        from app.research import exclusions as x1

        result = x1.register(factory, args.document, args.evidence)
    elif args.command == "x2-evidence":
        from app.research import exclusions_x2

        result = exclusions_x2.evidence(
            factory, OUT, _fresh(OUT / "research_exclusions_x2_evidence.json")
        )
    elif args.command == "x3-evidence":
        from app.research import ca_dividends

        result = ca_dividends.build(
            factory,
            OUT,
            OUT / args.lookups,
            _fresh(OUT / "ca_dividend_corrections_evidence.json"),
            seed=OUT / args.seed if args.seed else None,
        )
    elif args.command == "x31-calendar":
        from datetime import date

        from app.research import calendar_check

        result = calendar_check.build(
            factory, _fresh(OUT / "x31_calendar_evidence.json"), date(2026, 11, 13)
        )
    elif args.command == "x31-retrieve":
        from app.research import dividend_completeness

        result = dividend_completeness.retrieve_all(
            factory, _fresh(OUT / "x31_dividend_retrieval_pages.json")
        )
    elif args.command == "x31-evidence":
        from app.research import dividend_completeness

        result = dividend_completeness.build(
            factory,
            OUT,
            OUT / args.lookups,
            _fresh(OUT / "x31_dividend_completeness_evidence.json"),
            seed=OUT / args.seed if args.seed else None,
            retrieval_pages=OUT / args.pages,
        )
    elif args.command == "x4-capture":
        from datetime import date as _date

        from app.research import x4_evidence as x4e

        store = x4e.Store()
        forms = [_date.fromisoformat(args.formation)] if args.formation else list(x4e.FORMATIONS)
        if args.step == "assets":
            result = x4e.capture_assets(store)
        elif args.step == "snapshot":
            rel = "db/research_snapshot.json.gz"
            result = (
                {"exists": True}
                if store.has(rel)
                else store.put_json(
                    rel,
                    x4e.db_snapshot(factory),
                    {
                        "kind": "simulation_database_snapshot",
                        "read_only": True,
                        "tables": ["research_universe_candidates", "research_securities"],
                    },
                )
            )
            store.save()
        elif args.step == "archive":
            result = x4e.capture_archive(store)
        elif args.step == "sec":
            result = x4e.capture_sec(store)
        elif args.step == "sweep":
            lengths = [args.length] if args.length else [1, 2, 3, 4]
            swept: dict[str, Any] = {}
            for f in forms:
                for n in lengths:
                    swept[f"{f}:len{n}"] = x4e.sweep(store, f, f"len{n}", x4e.letter_tickers(n))
            result = swept
        elif args.step == "classes":
            cands = sorted({t for v in x4e.class_candidates(store).values() for t in v})
            classes: dict[str, Any] = {"candidates": len(cands)}
            for f in forms:
                classes[str(f)] = x4e.sweep(store, f, "classes", cands)
            result = classes
        elif args.step == "history":
            result = x4e.capture_history(store)
        elif args.step == "dividends":
            result = x4e.capture_dividends(store)
        else:
            result = x4e.derive(store)
    elif args.command == "x42-build":
        from app.research import lifecycle
        from app.research import x4_evidence as x4e

        doc = lifecycle.build(x4e.Store())
        path = lifecycle.OUT / "x42_ledger_v1.json"
        result = {
            "path": str(path),
            "sha256": lifecycle.write(doc, path),
            "content_sha256": doc["content_sha256"],
            **doc["summary"],
        }
    elif args.command == "x42-register":
        from sqlalchemy import text as _text

        from app.research import lifecycle

        with factory() as session:
            version = session.scalar(_text("select version_num from alembic_version"))
        if version != "0028":
            raise SystemExit(f"the ledger needs migration 0028; this database is at {version}")
        result = lifecycle.register(factory, args.ledger)
    elif args.command == "x4-verify":
        from app.research import x4_evidence as x4e

        result = x4e.verify(x4e.Store())
    elif args.command == "x43-capture":
        from app.research import x43_evidence as x43e

        store43 = x43e.Store()
        if args.step == "identities":
            result = x43e.capture_identities(factory, store43)
        else:
            result = x43e.capture_asof(store43)
    elif args.command == "x43-build":
        from app.research import resolution
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e

        graph = resolution.build(x4e.Store(), x43e.Store(), resolution.load_ledger())
        path = resolution.GRAPH_PATH
        result = {
            "path": str(path),
            "sha256": resolution.write(graph, path),
            "content_sha256": graph["content_sha256"],
            **graph["summary"],
        }
    elif args.command == "x43-verify":
        from app.research import resolution
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e

        store43 = x43e.Store()
        result = {
            "x43_evidence": x43e.verify(store43),
            "graph": resolution.verify(
                resolution.GRAPH_PATH, x4e.Store(), store43, resolution.load_ledger()
            ),
        }
    elif args.command == "x431-build":
        from app.research import ledger_v2, resolution
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e

        v2 = ledger_v2.build(
            x4e.Store(), x43e.Store(), resolution.load_ledger(), ledger_v2.load_graph()
        )
        result = {
            "path": str(ledger_v2.V2_PATH),
            "sha256": ledger_v2.write(v2),
            "content_sha256": v2["content_sha256"],
            "document_sha256": v2["document_sha256"],
            **v2["reconciliation"],
        }
    elif args.command == "x431-verify":
        from app.research import ledger_v2
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e

        result = ledger_v2.verify(ledger_v2.V2_PATH, x4e.Store(), x43e.Store())
    elif args.command == "x431-register":
        from sqlalchemy import text as _text

        from app.research import ledger_v2

        with factory() as session:
            version = session.scalar(_text("select version_num from alembic_version"))
        if version != "0028":
            raise SystemExit(f"the ledger needs migration 0028; this database is at {version}")
        result = ledger_v2.register(factory, args.ledger)
    elif args.command == "x432-capture":
        from app.research import x432_evidence as x432e

        result = x432e.capture_corporate_actions(x432e.Store())
    elif args.command == "x432-build":
        from app.research import graph_v2, ledger_v2
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e
        from app.research import x432_evidence as x432e

        g2 = graph_v2.build(
            x4e.Store(),
            x43e.Store(),
            x432e.Store(),
            ledger_v2.load_graph(),
            graph_v2.load_ledger_v2(),
        )
        result = {
            "path": str(graph_v2.GRAPH2_PATH),
            "sha256": graph_v2.write(g2),
            "content_sha256": g2["content_sha256"],
            **g2["summary"],
            "diff_vs_v1": {
                k: v
                for k, v in g2["diff_vs_v1"].items()
                if k not in ("provider_link_changes", "removed_edges_sample")
            },
        }
    elif args.command == "x432-verify":
        from app.research import graph_v2
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e
        from app.research import x432_evidence as x432e

        store432 = x432e.Store()
        result = {
            "x432_evidence": x432e.verify(store432),
            "graph_v2": graph_v2.verify(graph_v2.GRAPH2_PATH, x4e.Store(), x43e.Store(), store432),
        }
    elif args.command in ("x44-build", "x44-verify"):
        from app.research import graph_v2, universe_x44
        from app.research import x4_evidence as x4e
        from app.research import x43_evidence as x43e
        from app.research import x432_evidence as x432e

        if args.command == "x44-verify":
            result = universe_x44.verify(
                universe_x44.UNIVERSE_PATH, x4e.Store(), x43e.Store(), x432e.Store()
            )
        else:
            u = universe_x44.build(
                x4e.Store(),
                x43e.Store(),
                x432e.Store(),
                graph_v2.load_ledger_v2(),
                universe_x44.load_graph_v2(),
            )
            result = {
                "path": str(universe_x44.UNIVERSE_PATH),
                "sha256": universe_x44.write(u),
                "content_sha256": u["content_sha256"],
                "counts": {k: f["counts"]["decisions"] for k, f in u["formations"].items()},
                "priority_notice_impacts": u["priority_notice_impacts"],
            }
    elif args.command in ("x45-capture", "x45-build", "x45-verify"):
        from app.research import graph_v2, universe_x44
        from app.research import x45_data as x45d

        store45 = x45d.Store()
        if args.command == "x45-verify":
            result = x45d.verify(x45d.DATA_PATH, store45)
        else:
            uni = x45d.load_universe()
            v2 = graph_v2.load_ledger_v2()
            if args.command == "x45-build":
                ds = x45d.build(store45, uni, v2, universe_x44.load_graph_v2())
                result = {
                    "path": str(x45d.DATA_PATH),
                    "sha256": x45d.write(ds),
                    "content_sha256": ds["content_sha256"],
                    **ds["summary"],
                }
            elif args.step == "bars":
                result = x45d.capture_bars(store45, uni)
            elif args.step == "actions":
                result = x45d.capture_actions(store45, uni, v2)
            else:
                result = x45d.capture_recovery(store45, x45d.recovery_needs(uni, store45, v2))
    elif args.command in ("x46-build", "x46-verify"):
        from app.research import x46_validation as x46

        if args.command == "x46-verify":
            result = x46.verify()
        else:
            pinned = x46.preflight_hashes()
            if not pinned["ok"]:
                raise SystemExit(f"preflight hashes differ: {pinned}")
            dataset, uni, ledger, graph2 = x46.load_inputs()
            doc = x46.build(dataset, uni, ledger, x46.stores(), pinned, graph2)
            result = {
                "path": str(x46.VALIDATION_PATH),
                "sha256": x46.write(doc),
                "content_sha256": doc["content_sha256"],
                "status_table": x46.write_status_table(doc),
                **doc["summary"]["all"],
            }
    elif args.command in ("x461-capture", "x461-build", "x461-verify"):
        from app.research import graph_v2, x46_validation, x461_corrections, x461_revisions
        from app.research import x4_evidence as x4e
        from app.research import x45_data as x45mod

        s461 = x461_corrections.Store()
        if args.command == "x461-verify":
            result = x461_revisions.verify(x4e.Store(), s461)
        elif args.command == "x461-capture":
            if args.step == "filings":
                v1 = x4e.read_json(Path(x461_revisions.VALIDATION_V1[0]))
                d1 = x4e.read_json(x45mod.DATA_PATH)
                result = x461_corrections.capture_filings(
                    s461, x461_corrections.filing_plan(v1, d1, x4e.Store())
                )
            else:
                mini = x461_revisions.new_member_universe(
                    x4e.read_json(x461_revisions.UNIVERSE_V2_PATH)
                )
                ledger = graph_v2.load_ledger_v2()
                result = (
                    x45mod.capture_bars(s461, mini, None, ledger)  # type: ignore[arg-type]
                    if args.step == "bars"
                    else x45mod.capture_actions(s461, mini, ledger)  # type: ignore[arg-type]
                )
        else:
            x4s = x4e.Store()
            uv2 = x461_revisions.rebuild_universe_v2(x4s, s461)
            dv2 = x461_revisions.rebuild_data_v2(x4s, s461, uv2)
            result = {
                "universe_v2": x461_revisions.write(uv2, x461_revisions.UNIVERSE_V2_PATH),
                "data_v2": x461_revisions.write(dv2, x461_revisions.DATA_V2_PATH),
            }
            val = x461_revisions.build_validation_v2(s461)
            result["validation_v2"] = x46_validation.write(val, x461_revisions.VALIDATION_V2_PATH)
            result["status_table"] = x46_validation.write_status_table(
                val, x461_revisions.STATUS_TABLE_V2
            )
            result["summary"] = val["summary"]["all"]
    elif args.command in ("x5-capture", "x5-build", "x5-verify"):
        from app.research import x5_accounting, x5_capture

        if args.command == "x5-capture":
            s5 = x5_capture.Store()
            result = (
                x5_capture.capture_filings(s5)
                if args.step == "filings"
                else x5_capture.capture_prices(s5)
            )
        elif args.command == "x5-verify":
            result = x5_accounting.verify()
        else:
            ledger, tr = x5_accounting.build(x5_accounting.stores())
            result = {
                "ledger": x5_accounting.write(ledger, x5_accounting.LEDGER_PATH),
                "ledger_content_sha256": ledger["content_sha256"],
                "total_returns": x5_accounting.write(tr, x5_accounting.TR_PATH),
                "total_returns_content_sha256": tr["content_sha256"],
                "ledger_summary": ledger["summary"],
                "total_return_summary": {
                    k: v for k, v in tr["summary"].items() if k != "slots_without_series"
                },
            }
    elif args.command.startswith("x7v2-"):
        # The pipeline is imported only after its anchor passes (D-259), so a changed pipeline
        # never executes, not even at import.
        from app.research import x7v2_classification, x7v2_evaluator, x7v2_gate, x7v2_spreads

        if args.command == "x7v2-classify-funds":
            doc = x7v2_classification.build()
            result = {"file_sha256": x7v2_classification.write(doc), "summary": doc["summary"]}
        elif args.command == "x7v2-capture-spreads":
            result = x7v2_spreads.capture(factory, settings.postgres_db)
        else:
            x7v2_gate.check_pipeline()
            from app.research import x7v2_pipeline

            if args.command == "x7v2-backtest":
                result = x7v2_pipeline.cli_backtest()
            elif args.command == "x7v2-verify":
                result = x7v2_pipeline.verify()
            else:
                result = x7v2_evaluator.cli_evaluate()
    elif args.command.startswith("x7-"):
        from app.research import x7_pipeline, x7_spreads

        if args.command == "x7-capture-spreads":
            result = x7_spreads.capture(factory, settings.postgres_db)
        elif args.command == "x7-signals":
            result = x7_pipeline.cli_signals(args.workers)
        elif args.command == "x7-backtest":
            result = x7_pipeline.cli_backtest()
        else:
            result = x7_pipeline.verify()
    elif args.command in ("x6-build", "x6-verify"):
        from app.research import x6_build

        if args.command == "x6-verify":
            result = x6_build.verify()
        else:
            doc = x6_build.build()
            result = {
                "artifact": x6_build.write(doc),
                "content_sha256": doc["content_sha256"],
                "validity_ok": doc["validity_check"]["ok"],
                "formation_steps": doc["formation_steps"]["summary"],
                "exits": doc["exits"]["by_treatment"],
                "reconciliation": {k: v for k, v in doc["reconciliation"].items() if k != "rows"},
            }
    elif args.command == "x31-table":
        from app.research import dividend_completeness
        from app.research.exclusions import sha256_file

        ev = json.loads(args.evidence.read_text())
        ev["_sha256"] = sha256_file(args.evidence)
        args.out.write_text(
            dividend_completeness.asset_table(ev, args.evidence.as_posix()), encoding="utf-8"
        )
        result = {"table": str(args.out), "assets": len(ev["assets"])}
    elif args.command == "register-x31":
        from app.research import dividend_completeness

        result = dividend_completeness.register(factory, args.document, args.evidence)
    elif args.command == "register-x3":
        from app.research import ca_dividends

        result = ca_dividends.register(factory, args.document, args.evidence)
    elif args.command == "register-x2":
        from app.research import exclusions_x2

        result = exclusions_x2.register(factory, args.document, args.evidence)
    else:
        from app.research.reservations import preregister

        result = preregister(factory, args.code, args.document, args.reservations)
    print(json.dumps(result, indent=1, default=str), flush=True)


if __name__ == "__main__":
    main()
