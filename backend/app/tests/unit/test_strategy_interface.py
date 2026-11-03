"""Strategy interface, the six family modules, proposal validation, expiry and settings
(Phase 4A; pure, no database)."""

from __future__ import annotations

import inspect
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.asset_discovery.sessions import CALENDARS
from app.core.config import InvalidSettingsError
from app.database.models import AssetClass
from app.feature_engine.registry import (
    REGISTRY,
    FeatureRequest,
    FeatureSource,
    FeatureStatus,
    FeatureValue,
)
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies import families
from app.strategies.base import (
    AssetView,
    EvaluationContext,
    Evidence,
    InterfaceOnlyStrategy,
    ProposalDraft,
    StrategyDefinitionError,
    StrategyNotImplementedError,
    StrategyParameters,
)
from app.strategies.config import StrategySettings, get_strategy_settings
from app.strategies.enums import (
    Direction,
    EvaluationMode,
    StrategyFamily,
    StrengthKind,
    TriggerKind,
)
from app.strategies.periods import expires_at
from app.strategies.proposals import ProposalValidationError, validate_drafts
from app.strategies.registry import StrategyRegistry, default_registry, discover

CAL = CALENDARS["XNYS"]
CUTOFF = datetime(2026, 11, 3, 22, 30, tzinfo=UTC)  # Tuesday after the close


# --- the six families ----------------------------------------------------------------------
def test_six_independent_families_are_discovered():
    strategies = default_registry()
    assert len(strategies) == 6
    assert {s.family for s in strategies} == set(StrategyFamily)
    assert len({s.strategy_id for s in strategies}) == 6
    modules = {inspect.getmodule(type(s)).__name__ for s in strategies}
    assert len(modules) == 6  # one module each
    assert all(m.startswith(families.__name__ + ".") for m in modules)


@pytest.mark.parametrize("strategy", discover(), ids=lambda s: s.strategy_id)
def test_every_family_exposes_the_full_interface(strategy):
    assert strategy.implemented is True  # Phase 4B: logic implemented
    assert not isinstance(strategy, InterfaceOnlyStrategy)
    assert not strategy.logic_version.endswith("-interface-1")
    assert strategy.description
    assert set(strategy.supported_asset_classes) <= set(AssetClass)
    assert strategy.schedule.triggers
    params = strategy.default_parameters()
    assert params.signal_ttl_periods >= 1
    assert params.holding_horizon_periods >= 1
    assert set(params.directions) == {Direction.LONG, Direction.SHORT}
    schema = strategy.parameters_schema()
    assert schema["additionalProperties"] is False
    for timeframe in strategy.supported_timeframes:
        resolved = REGISTRY.resolve_all(strategy.required_features(params, timeframe))
        assert resolved
        assert any(f.definition.name == "close" for f in resolved)
        assert all(f.timeframe == timeframe for f in resolved)


def test_interface_only_strategies_still_refuse_to_evaluate():
    with pytest.raises(StrategyNotImplementedError, match="Phase 4B"):
        _Probe().evaluate(make_context({}), _Probe().default_parameters())


def test_event_driven_requires_news_and_cannot_make_it_optional():
    strategy = default_registry().get("event_driven")
    params = strategy.default_parameters()
    news = [
        f
        for f in REGISTRY.resolve_all(strategy.required_features(params, "1Day"))
        if f.definition.source is FeatureSource.NEWS
    ]
    # Phase 4B: counts anchored to the newest session close, so events precede the reaction.
    counts = [f for f in news if f.definition.name == "period_news_count"]
    assert len(counts) == 1 + len(params.categories)  # ANY plus each category
    assert all(not f.optional for f in counts)
    assert TriggerKind.NEWS_EVENT in strategy.schedule.triggers


@pytest.mark.parametrize(
    ("strategy_id", "params", "message"),
    [
        ("momentum", {"acceleration_periods": 30}, "shorter than lookback"),
        ("mean_reversion", {"rsi_oversold": "45", "rsi_overbought": "55"}, None),
        ("mean_reversion", {"rsi_oversold": "60"}, "less than 50"),
        ("volatility", {"atr_short": 30, "atr_long": 20}, "shorter than atr_long"),
        ("trend_following", {"slow_ema_length": 300}, "less than or equal to 200"),
        ("breakout", {"max_width_percentile": "1.5"}, "less than or equal to 1"),
        ("event_driven", {"categories": ["EARNINGS", "EARNINGS"]}, "unique"),
        ("event_driven", {"timeframes": ["1Week"]}, "not supported by event_driven"),
        ("momentum", {"signal_ttl_periods": 0}, "greater than or equal to 1"),
        ("momentum", {"surprise": True}, "Extra inputs"),
    ],
)
def test_parameter_schemas_validate(strategy_id, params, message):
    strategy = default_registry().get(strategy_id)
    if message is None:
        strategy.parse_parameters(params)
        return
    with pytest.raises(StrategyDefinitionError, match=message):
        strategy.parse_parameters(params)


def test_parameters_are_immutable():
    params = default_registry().get("breakout").default_parameters()
    with pytest.raises(Exception, match="frozen"):
        params.channel_length = 99  # type: ignore[misc]


class _Probe(InterfaceOnlyStrategy):
    strategy_id: ClassVar[str] = "probe_strategy"
    family = StrategyFamily.MOMENTUM
    name = "Probe"
    description = "TEST FIXTURE"
    logic_version: ClassVar[str] = "probe-1"
    supported_asset_classes = (AssetClass.EQUITY,)
    supported_timeframes = ("1Day",)

    def required_features(self, params, timeframe):
        return [FeatureRequest("close", timeframe)]


def test_registry_validates_strategies_when_registered():
    StrategyRegistry([_Probe()])  # a new strategy needs no change elsewhere
    cases = [
        (type("Bad", (_Probe,), {"strategy_id": "Bad-Id"}), "invalid strategy id"),
        (type("NoLogicVersion", (_Probe,), {"logic_version": " "}), "logic_version"),
        (type("Hourly", (_Probe,), {"supported_timeframes": ("1Hour",)}), "no data"),
        (
            type(
                "UnknownFeature",
                (_Probe,),
                {"required_features": lambda self, p, t: [FeatureRequest("magic", t)]},
            ),
            "unknown feature",
        ),
    ]
    for cls, message in cases:
        with pytest.raises(StrategyDefinitionError, match=message):
            StrategyRegistry([cls()])
    with pytest.raises(StrategyDefinitionError, match="duplicate"):
        StrategyRegistry([_Probe(), _Probe()])


# --- proposal validation -------------------------------------------------------------------
def value(key: str, number: float, status: FeatureStatus = FeatureStatus.VALID) -> FeatureValue:
    return FeatureValue(
        key=key,
        name=key.split(".")[0],
        version="v1",
        source=FeatureSource.BARS,
        timeframe="1Day",
        params={},
        value=number if status is FeatureStatus.VALID else None,
        status=status,
        reason=None,
        available_at=CUTOFF - timedelta(hours=1),
    )


CLOSE, SMA, ATR = "close.v1()@1Day", "sma.v1(length=5)@1Day", "atr.v1(length=14)@1Day"


def make_context(features) -> EvaluationContext:
    return EvaluationContext(
        asset=AssetView(uuid.uuid4(), "FXA", AssetClass.EQUITY, "XNAS"),
        timeframe="1Day",
        mode=EvaluationMode.LIVE,
        basis=KnowledgeBasis.RECORDED,
        data_cutoff=CUTOFF,
        features=features,
        trigger=TriggerKind.BAR_CLOSE,
    )


def draft(**overrides) -> ProposalDraft:
    values: dict[str, Any] = {
        "direction": Direction.LONG,
        "reference_entry": Decimal("100.123456789"),
        "invalidation_level": Decimal("95"),
        "target_reference": Decimal("110"),
        "signal_strength": Decimal("0.61234"),
        "reasoning": " Close above its average. ",
        "evidence": (Evidence(CLOSE, "trigger", "newest close"),),
    } | overrides
    return ProposalDraft(**values)


@pytest.fixture
def ctx():
    return make_context(
        {
            CLOSE: value(CLOSE, 100.0),
            SMA: value(SMA, 98.0),
            ATR: value(ATR, 0, FeatureStatus.UNDEFINED),
        }
    )


def validate(ctx, drafts, params=None, expiry=None):
    return validate_drafts(
        drafts,
        ctx,
        params or StrategyParameters(),
        expires_at=expiry or CUTOFF + timedelta(days=1),
        snapshot_ids=[uuid.uuid4()],
    )


def test_valid_draft_becomes_a_complete_proposal(ctx):
    [spec], notes = validate(ctx, [draft()])
    assert notes == []
    assert spec.reference_entry == Decimal("100.12345679")  # NUMERIC(20, 8)
    assert spec.signal_strength == Decimal("0.6123")
    assert spec.strength_kind is StrengthKind.UNCALIBRATED_SCORE
    assert spec.signal_at == spec.data_cutoff == CUTOFF
    assert spec.market == "EQUITY"
    assert spec.reasoning == "Close above its average."
    assert spec.evidence[0]["value"] == repr(100.0)
    assert spec.holding_horizon_periods == 5
    assert len(spec.feature_snapshot_ids) == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"invalidation_level": Decimal("101")}, "below the entry"),
        ({"target_reference": Decimal("99")}, "above the entry"),
        (
            {"direction": Direction.SHORT, "invalidation_level": Decimal("99")},
            "above the entry",
        ),
        ({"signal_strength": Decimal("1.2")}, r"within \[0, 1\]"),
        ({"signal_strength": 0.5}, "Decimal"),
        ({"reference_entry": Decimal("-1")}, "positive"),
        ({"reference_entry": Decimal("NaN")}, "finite"),
        ({"reasoning": "  "}, "reasoning summary"),
        ({"evidence": ()}, "supporting evidence"),
        ({"evidence": (Evidence("rsi.v1(length=14)@1Day", "trigger", "x"),)}, "not in the"),
        ({"evidence": (Evidence(ATR, "context", "atr"),)}, "UNDEFINED"),
    ],
)
def test_invalid_drafts_are_refused(ctx, overrides, message):
    with pytest.raises(ProposalValidationError, match=message):
        validate(ctx, [draft(**overrides)])


def test_direction_rules_and_minimum_strength(ctx):
    with pytest.raises(ProposalValidationError, match="more than one LONG"):
        validate(ctx, [draft(), draft()])
    long_only = StrategyParameters(directions=(Direction.LONG,))
    with pytest.raises(ProposalValidationError, match="SHORT proposals are not enabled"):
        validate(
            ctx,
            [draft(direction=Direction.SHORT, invalidation_level=None, target_reference=None)],
            long_only,
        )
    picky = StrategyParameters(min_signal_strength=Decimal("0.7"))
    specs, notes = validate(ctx, [draft()], picky)
    assert specs == []
    assert "below 0.7" in notes[0]
    with pytest.raises(ProposalValidationError, match="after the signal"):
        validate(ctx, [draft()], expiry=CUTOFF)


def test_context_only_serves_declared_features(ctx):
    assert ctx.number("close") == 100.0
    assert ctx.feature("sma", length=5).value == 98.0
    with pytest.raises(StrategyDefinitionError, match="not declared"):
        ctx.feature("sma", length=50)
    with pytest.raises(StrategyDefinitionError, match="no valid numeric"):
        ctx.number("atr")


# --- expiry ----------------------------------------------------------------------------------
def close_of(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T21:00:00+00:00")  # 16:00 EST


@pytest.mark.parametrize(
    ("signal", "timeframe", "periods", "expected"),
    [
        (CUTOFF, "1Day", 1, close_of("2026-11-04")),  # after Tue close -> Wed close
        (CUTOFF.replace(hour=15), "1Day", 1, close_of("2026-11-03")),  # intraday -> same day
        (CUTOFF, "1Day", 3, close_of("2026-11-06")),
        (datetime(2026, 11, 7, 12, tzinfo=UTC), "1Day", 1, close_of("2026-11-09")),  # Sat
        (datetime(2026, 11, 25, 22, tzinfo=UTC), "1Day", 1, datetime(2026, 11, 27, 18, tzinfo=UTC)),
        (CUTOFF, "1Week", 1, close_of("2026-11-06")),  # this week's last session
        (close_of("2026-11-06") + timedelta(hours=1), "1Week", 1, close_of("2026-11-13")),
    ],
)
def test_expiry_counts_completed_periods_on_the_exchange_calendar(
    signal, timeframe, periods, expected
):
    # 26 Nov 2026 is Thanksgiving (closed); 27 Nov closes early at 13:00 EST.
    assert expires_at(CAL, timeframe, signal, periods) == expected


def test_expiry_rejects_bad_input():
    with pytest.raises(ValueError, match="at least 1"):
        expires_at(CAL, "1Day", CUTOFF, 0)
    with pytest.raises(ValueError, match="unsupported"):
        expires_at(CAL, "1Hour", CUTOFF, 1)


# --- settings ----------------------------------------------------------------------------------
def test_settings_defaults_are_safe(clean_env, tmp_path):
    clean_env.chdir(tmp_path)
    settings = get_strategy_settings()
    assert settings.worker_enabled is False
    assert settings.max_attempts == 3
    assert settings.lease_seconds > settings.evaluation_timeout_seconds


def test_settings_reject_typos_and_inconsistent_values(clean_env, tmp_path):
    clean_env.chdir(tmp_path)
    clean_env.setenv("STRATEGY_WORKER_ENABLD", "true")
    with pytest.raises(Exception, match="STRATEGY_WORKER_ENABLD"):
        get_strategy_settings()
    with pytest.raises(ValueError, match="lease_seconds must exceed"):
        StrategySettings(_env_file=None, lease_seconds=30, evaluation_timeout_seconds=60)
    clean_env.delenv("STRATEGY_WORKER_ENABLD")
    get_strategy_settings.cache_clear()
    clean_env.setenv("STRATEGY_MAX_CONCURRENCY", "0")
    with pytest.raises(InvalidSettingsError, match="max_concurrency"):
        get_strategy_settings()


def test_draft_dataclasses_are_frozen():
    with pytest.raises(AttributeError):
        replace(draft()).direction = Direction.SHORT  # type: ignore[misc]


def test_env_example_documents_and_loads_every_setting(clean_env, tmp_path, monkeypatch):
    from pathlib import Path

    from app.strategies.config import ENV_PREFIX

    example = Path(__file__).resolve().parents[4] / ".env.example"
    text = example.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line.startswith(ENV_PREFIX)}
    assert keys == {f"{ENV_PREFIX}{name}".upper() for name in StrategySettings.model_fields}
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_strategy_settings() == StrategySettings(_env_file=None)  # the example = defaults
