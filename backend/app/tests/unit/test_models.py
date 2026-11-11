"""Static checks on the ORM metadata that protect database conventions."""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, Enum, Float

from app.database.models import Base, PortfolioAccount, PortfolioAccountType

TABLES = Base.metadata.sorted_tables


def test_expected_tables_exist():
    assert set(Base.metadata.tables) == {
        # Phase 1 foundation
        "assets",
        "asset_identifiers",
        "portfolio_accounts",
        "audit_logs",
        # Phase 2A provider registry
        "market_data_providers",
        "market_data_feeds",
        # Phase 2B historical ingestion
        "market_data_ingestion_jobs",
        "market_data_ingestion_job_symbols",
        "market_data_raw_events",
        "market_bars",
        "market_data_quality_events",
        # Phase 2C live streaming
        "market_quotes",
        "market_trades",
        "market_data_stream_sessions",
        "market_data_stream_status",
        "market_data_stream_gaps",
        # Phase 2D reliability and recovery
        "market_data_coverage",
        "market_data_gaps",
        "market_data_recovery_jobs",
        "market_data_recovery_status",
        # Phase 3A asset discovery and universe
        "exchanges",
        "provider_exchange_codes",
        "provider_assets",
        "asset_reference_versions",
        "asset_lifecycle_events",
        "discovery_jobs",
        "discovery_results",
        "qualification_rule_sets",
        "scoring_versions",
        "asset_metric_values",
        "qualification_evaluations",
        "watchlist_scores",
        "universe_membership_events",
        # Phase 3B market intelligence
        "intelligence_configs",
        "intelligence_runs",
        "market_feature_snapshots",
        "regime_classifications",
        "market_summaries",
        "cross_asset_relationships",
        # Phase 3C news and event intelligence
        "news_providers",
        "news_sources",
        "news_articles",
        "news_article_revisions",
        "news_asset_mappings",
        "news_ingestion_jobs",
        "news_stream_sessions",
        "news_stream_status",
        "news_events",
        "event_classifications",
        "event_asset_relationships",
        "economic_events",
        "economic_event_revisions",
        # Phase 4A strategy laboratory
        "feature_definitions",
        "strategies",
        "strategy_configurations",
        "strategy_versions",
        "strategy_status_events",
        "strategy_feature_snapshots",
        "strategy_evaluations",
        "strategy_evaluation_snapshots",
        "strategy_evaluation_errors",
        "strategy_proposals",
        "strategy_proposal_events",
        "strategy_proposal_conflicts",  # Phase 4B
        "ml_datasets",  # Phase 5A
        "ml_dataset_versions",
        "ml_dataset_observations",
        "ml_training_runs",
        "ml_model_artifacts",
        "ml_model_versions",
        "ml_model_status_events",
        "ml_model_evaluations",
        "ml_calibration_results",
        "ml_predictions",
        "decision_policies",  # Phase 5B
        "decision_policy_versions",
        "decision_runs",
        "decision_inputs",
        "ai_decisions",
        "decision_evidence",
        "decision_scoring_components",
        "decision_conflicts",
        "decision_evaluations",
        "llm_requests",
        "llm_responses",
        "llm_token_usage",
        "llm_cost_records",
        "llm_analyses",
        # Phase 6A portfolio and double-entry accounting
        "currencies",
        "fx_rates",
        "instrument_specs",
        "ledger_accounts",
        "posting_templates",
        "journal_entries",
        "journal_lines",
        "asset_quantity_ledger",
        "currency_balances",
        "portfolio_snapshots",
        "portfolio_snapshot_positions",
        "reconciliation_runs",
        "reconciliation_results",
        # Phase 6B deterministic risk engine
        "risk_configurations",
        "risk_configuration_versions",
        "risk_asset_classifications",
        "risk_exposure_snapshots",
        "risk_period_baselines",
        "risk_assessments",
        "risk_assessment_reasons",
        "risk_circuit_breaker_events",
        "risk_events",
        # Phase 6C atomic risk reservations
        "risk_reservations",
        "risk_reservation_requests",
        "risk_reservation_events",
        "risk_reservation_states",
        "risk_reservation_approvals",
        "risk_reservation_reconciliations",
        "system_health_events",
        # Phase 7A paper execution and order management (simulated orders only)
        "execution_requests",
        "orders",
        "order_events",
        "order_fills",
        "order_requests",
        "paper_broker_state",
        "paper_broker_orders",
        "paper_broker_events",
        "outbox_events",
        # Phase 7B simulated execution and position management
        "execution_models",
        "execution_attempts",
        "execution_fill_details",
        "paper_positions",
        "position_events",
        "position_close_requests",
        # Phase 8A backtesting (simulations over stored history; insert-only results)
        "backtest_runs",
        "backtest_strategy_versions",
        "backtest_event_cursors",
        "backtest_proposals",
        "backtest_rejections",
        "backtest_orders",
        "backtest_fills",
        "backtest_trades",
        "backtest_equity_snapshots",
        "backtest_metrics",
        "backtest_benchmark_results",
        "backtest_run_events",
        "backtest_run_errors",
        "backtest_journal_entries",
        "backtest_evaluation_counts",
    }


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_all_timestamps_are_timezone_aware(table):
    naive = [c.name for c in table.columns if isinstance(c.type, DateTime) and not c.type.timezone]
    assert naive == []


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_no_floating_point_columns(table):
    assert [c.name for c in table.columns if isinstance(c.type, Float)] == []


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_every_table_has_a_primary_key_and_named_constraints(table):
    assert table.primary_key.columns
    for constraint in table.constraints:
        assert constraint.name, f"unnamed constraint on {table.name}"


def test_portfolio_accounts_hold_no_balances():
    """The ledger is authoritative; account rows must not carry cached money values."""
    columns = set(PortfolioAccount.__table__.columns.keys())
    forbidden = {"balance", "cash", "equity", "buying_power", "pnl"}
    assert not {c for c in columns if any(word in c for word in forbidden)}


def test_portfolio_account_type_is_paper_only():
    assert list(PortfolioAccountType) == [PortfolioAccountType.PAPER]
    column_type = PortfolioAccount.__table__.c.account_type.type
    assert isinstance(column_type, Enum)
    assert column_type.enums == ["PAPER"]
