"""MARKET_INTELLIGENCE_* settings and the versioned default configuration (Phase 3B)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import UnknownSettingError
from app.data_engine.enums import PriceAdjustment
from app.market_intelligence.config import (
    DAILY,
    DEFAULT_CONFIG_CODE,
    DEFAULT_CONFIGS,
    ENV_PREFIX,
    WEEKLY,
    IntelligenceConfig,
    MarketIntelligenceSettings,
    TrendConfig,
    VolatilityConfig,
    get_market_intelligence_settings,
)


def test_defaults(clean_env):
    settings = MarketIntelligenceSettings(_env_file=None)
    assert settings.config == DEFAULT_CONFIG_CODE
    assert settings.benchmark_symbol == "SPY"
    assert settings.price_adjustment is PriceAdjustment.SPLIT
    assert MarketIntelligenceSettings(
        _env_file=None, price_adjustment="split"
    ).price_adjustment is (PriceAdjustment.SPLIT)


def test_unknown_key_is_rejected_as_a_typo(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("MARKET_INTELLIGENCE_BENCHMARK", "QQQ")
    with pytest.raises(UnknownSettingError):
        get_market_intelligence_settings()


def test_env_example_documents_and_loads_every_setting(clean_env, tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[4] / ".env.example"
    text = example.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line.startswith(ENV_PREFIX)}
    assert keys == {f"{ENV_PREFIX}{n}".upper() for n in MarketIntelligenceSettings.model_fields}
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_market_intelligence_settings() == MarketIntelligenceSettings(_env_file=None)


def test_default_configuration_and_windows():
    _, config = DEFAULT_CONFIGS[DEFAULT_CONFIG_CODE]
    assert [t.timeframe for t in config.timeframes] == ["1Day", "1Week"]
    # The longest look-back sets the window: 120 rolling-volatility values of 20 sessions.
    assert DAILY.window == 140
    assert WEEKLY.window == 39
    # The stored form round-trips (it is what the database keeps and versions).
    assert IntelligenceConfig.model_validate(config.model_dump(mode="json")) == config


def test_invalid_configurations_are_rejected():
    with pytest.raises(ValidationError, match="1Day"):
        IntelligenceConfig(timeframes=[WEEKLY])
    with pytest.raises(ValidationError, match="increase"):
        VolatilityConfig(low_below=0.8, elevated_above=0.75)
    with pytest.raises(ValidationError, match="unknown trend components"):
        TrendConfig(weights={"astrology": Decimal(1)})
    with pytest.raises(ValidationError, match="shorter"):
        DAILY.model_copy(update={"fast_ma": 60}).model_validate(
            DAILY.model_dump() | {"fast_ma": 60}
        )
