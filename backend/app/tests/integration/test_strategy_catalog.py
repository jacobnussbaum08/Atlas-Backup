"""Strategy catalog, immutable versions and status against real PostgreSQL (Phase 4A)."""

from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.database.models import (
    AuditLog,
    FeatureDefinitionRecord,
    StrategyConfigurationRecord,
    StrategyRecord,
    StrategyStatusEventRecord,
    StrategyVersionRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import REGISTRY, FeatureRegistry
from app.strategies import catalog
from app.strategies.base import StrategyDefinitionError, StrategyParameters
from app.strategies.registry import default_registry
from app.tests.integration.strategy_support import (
    FixtureParameters,
    ProposingStrategy,
    make_lab,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def factory(fresh_engine):
    return sessionmaker(bind=fresh_engine, expire_on_commit=False)


def sync(factory, strategies, registry=REGISTRY):
    with session_scope(factory) as session:
        return catalog.sync_catalog(session, strategies, registry)


def test_sync_registers_the_six_families_once(factory):
    families = default_registry()
    report = sync(factory, families)
    assert report.strategies_registered == 6
    assert sorted(report.versions_created) == sorted(f"{s.strategy_id} v1" for s in families)
    assert report.features_registered == len(REGISTRY.definitions())
    assert report.problems == []
    again = sync(factory, families)
    assert (again.strategies_registered, again.versions_created, again.features_registered) == (
        0,
        [],
        0,
    )
    with session_scope(factory) as session:
        versions = session.scalars(select(StrategyVersionRecord)).all()
        assert {v.strategy_id for v in versions} == {s.strategy_id for s in families}
        assert all(not v.implemented for v in versions)
        momentum = catalog.latest_version(session, "momentum")
        config = catalog.configuration(session, momentum)
    # Every parameter is stored explicitly (defaults included) with its feature keys.
    assert config.parameters["lookback_periods"] == 20
    assert config.parameters["min_volume_ratio"] == "1.2"
    assert "return.v1(offset=0,periods=20)@1Day" in {
        f["key"] for f in momentum.required_features["1Day"]
    }


def test_changing_a_definition_without_a_new_version_is_refused(factory):
    sync(factory, [])
    changed = FeatureRegistry(
        replace(d, method=d.method + " (edited)") if d.name == "rsi" else d
        for d in REGISTRY.definitions()
    )
    with pytest.raises(catalog.FeatureDefinitionConflictError, match="rsi v1 changed"):
        sync(factory, [], changed)


def test_new_parameters_create_a_new_immutable_version(factory):
    strategy = default_registry().get("trend_following")
    sync(factory, [strategy])
    with session_scope(factory) as session:
        v2, created = catalog.create_version(
            session, strategy, {"min_adx": "25"}, reason="stricter trend filter", actor="test"
        )
        same, again = catalog.create_version(
            session, strategy, {"min_adx": "25"}, reason="repeat", actor="test"
        )
        assert (v2.version, created, same.id, again) == (2, True, v2.id, False)
        params = catalog.configuration(session, v2).parameters
        assert params["min_adx"] == "25"
        assert params["fast_ema_length"] == 20  # untouched fields keep their values
        back, created_back = catalog.create_version(
            session, strategy, {"min_adx": "20"}, reason="revert", actor="test"
        )
        # Reverting reuses the original configuration but is a new version number.
        v1 = session.scalars(
            select(StrategyVersionRecord).where(StrategyVersionRecord.version == 1)
        ).one()
        assert (back.version, created_back, back.configuration_id) == (3, True, v1.configuration_id)
        assert session.scalar(select(func.count()).select_from(StrategyConfigurationRecord)) == 2
        actions = session.scalars(
            select(AuditLog.action).where(AuditLog.entity_id == "trend_following")
        ).all()
    assert actions.count("strategy.version_created") == 3


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"unknown": 1}, "Extra inputs are not permitted"),
        ({"fast_ema_length": 1}, "greater than or equal to 2"),
        ({"fast_ema_length": 60, "slow_ema_length": 50}, "fast_ema_length < slow_ema_length"),
        ({"timeframes": ["1Hour"]}, "unsupported timeframe"),
        ({"directions": ["LONG", "LONG"]}, "unique"),
    ],
)
def test_invalid_parameters_are_rejected_without_a_version(factory, params, message):
    strategy = default_registry().get("trend_following")
    sync(factory, [strategy])
    with pytest.raises(StrategyDefinitionError, match=message), session_scope(factory) as s:
        catalog.create_version(s, strategy, params, reason="bad", actor="test")
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(StrategyVersionRecord)) == 1


def test_versions_configurations_and_definitions_are_append_only(factory):
    sync(factory, default_registry())
    statements = [
        update(StrategyVersionRecord).values(reason="rewritten"),
        update(StrategyConfigurationRecord).values(parameters={}),
        update(FeatureDefinitionRecord).values(method="rewritten"),
        update(StrategyRecord).values(family="BREAKOUT"),
        text("DELETE FROM strategy_versions"),
        text("TRUNCATE strategy_configurations CASCADE"),
    ]
    for statement in statements:
        with pytest.raises(DBAPIError, match="append-only"), session_scope(factory) as session:
            session.execute(statement)


def test_interface_only_strategies_cannot_be_enabled(factory):
    strategy = default_registry().get("breakout")
    sync(factory, [strategy])
    with pytest.raises(catalog.StrategyStatusError, match="Phase 4B"), session_scope(factory) as s:
        catalog.set_status(s, strategy, enabled=True, reason="try", actor="test")
    with session_scope(factory) as session:
        _, changed = catalog.set_status(
            session, strategy, enabled=False, reason="explicitly off", actor="test"
        )
        assert changed is True
        assert catalog.is_enabled(session, "breakout") is False


def test_status_changes_are_recorded_history(fresh_engine):
    lab = make_lab(fresh_engine, enable=False)
    with session_scope(lab.factory) as session:
        assert catalog.is_enabled(session, "fixture_proposer") is False  # default: disabled
    lab.enable("fixture_proposer")
    lab.enable("fixture_proposer")  # no-op
    lab.enable("fixture_proposer", False)
    with session_scope(lab.factory) as session:
        events = session.scalars(
            select(StrategyStatusEventRecord.enabled).order_by(StrategyStatusEventRecord.id)
        ).all()
        assert events == [True, False]
        assert catalog.is_enabled(session, "fixture_proposer") is False
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(update(StrategyStatusEventRecord).values(enabled=True))


def test_a_logic_change_in_code_creates_a_version_with_the_same_parameters(factory):
    sync(factory, [ProposingStrategy()])

    class Revised(ProposingStrategy):
        logic_version: ClassVar[str] = "fixture-proposer-2"

    report = sync(factory, [Revised()])
    assert report.versions_created == ["fixture_proposer v2"]
    with session_scope(factory) as session:
        rows = session.scalars(
            select(StrategyVersionRecord).order_by(StrategyVersionRecord.version)
        ).all()
    assert [r.logic_version for r in rows] == ["fixture-proposer-1", "fixture-proposer-2"]
    assert rows[0].configuration_id == rows[1].configuration_id
    assert rows[1].reason == "code changed: logic_version"


class _StricterParameters(FixtureParameters):
    new_threshold: int  # required: the stored configuration does not have it


class _StricterStrategy(ProposingStrategy):
    logic_version: ClassVar[str] = "fixture-proposer-3"
    Parameters: ClassVar[type[StrategyParameters]] = _StricterParameters


def test_unvalidatable_stored_parameters_are_reported_not_replaced(factory):
    sync(factory, [ProposingStrategy()])
    report = sync(factory, [_StricterStrategy()])
    assert report.versions_created == []
    [problem] = report.problems
    assert "fixture_proposer: the current configuration no longer validates" in problem
    with session_scope(factory) as session:
        assert session.scalar(select(func.count()).select_from(StrategyVersionRecord)) == 1
