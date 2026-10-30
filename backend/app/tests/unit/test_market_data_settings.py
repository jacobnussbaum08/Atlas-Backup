from __future__ import annotations

import pytest

from app.core.config import (
    InvalidSettingsError,
    MarketDataSettings,
    UnknownSettingError,
    get_market_data_settings,
)


@pytest.fixture
def isolated(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no developer .env
    return clean_env


def _settings(**values) -> MarketDataSettings:
    return MarketDataSettings(_env_file=None, **values)


def test_defaults_are_safe(isolated):
    settings = get_market_data_settings()

    assert settings.market_data_provider == "alpaca"
    assert settings.market_data_stream_enabled is False
    assert settings.market_data_symbols == ()  # the universe is configuration, not code
    assert settings.historical_data_batch_size == 1000
    assert settings.historical_data_max_retries == 5


def test_symbols_are_parsed_normalised_and_ordered(isolated):
    isolated.setenv("MARKET_DATA_SYMBOLS", " spy, QQQ ,AAPL,msft,NVDA,BRK.B ")

    symbols = get_market_data_settings().market_data_symbols

    assert symbols == ("SPY", "QQQ", "AAPL", "MSFT", "NVDA", "BRK.B")


@pytest.mark.parametrize("value", ["SPY,SPY", "SPY,spy"])
def test_duplicate_symbols_are_rejected(value):
    with pytest.raises(ValueError, match="duplicate"):
        _settings(market_data_symbols=value)


@pytest.mark.parametrize("value", ["SPY,AA PL", "SPY,$X", "SPY,ÄPPLE"])
def test_malformed_symbols_are_rejected(value):
    with pytest.raises(ValueError, match="invalid symbol"):
        _settings(market_data_symbols=value)


def test_provider_identifier_is_normalised_and_validated():
    assert _settings(market_data_provider=" Alpaca ").market_data_provider == "alpaca"
    with pytest.raises(ValueError, match="provider identifier"):
        _settings(market_data_provider="alpaca markets")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market_data_reconnect_max_delay", 0),
        ("market_data_reconnect_max_delay", 3601),
        ("market_data_heartbeat_timeout", -1),
        ("historical_data_batch_size", 0),
        ("historical_data_max_retries", 21),
    ],
)
def test_numeric_bounds(field, value):
    with pytest.raises(ValueError, match="Input should be"):
        _settings(**{field: value})


def test_stream_flag_parses_booleans(isolated):
    isolated.setenv("MARKET_DATA_STREAM_ENABLED", "true")
    assert get_market_data_settings().market_data_stream_enabled is True


@pytest.mark.parametrize("key", ["MARKET_DATA_SYMBOL", "HISTORICAL_DATA_BATCHSIZE"])
def test_misspelled_variables_are_rejected(isolated, key):
    isolated.setenv(key, "1")
    with pytest.raises(UnknownSettingError, match=key):
        get_market_data_settings()


def test_loader_errors_are_sanitised(isolated):
    isolated.setenv("HISTORICAL_DATA_BATCH_SIZE", "not-a-number-SENTINEL")

    with pytest.raises(InvalidSettingsError) as excinfo:
        get_market_data_settings()

    assert "historical_data_batch_size" in str(excinfo.value)
    assert "SENTINEL" not in str(excinfo.value)


def test_env_example_market_data_section_is_valid(isolated, tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parents[4] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    settings = get_market_data_settings()

    assert settings.market_data_symbols == ("SPY", "QQQ", "AAPL", "MSFT", "NVDA")
    assert settings.market_data_stream_enabled is False
