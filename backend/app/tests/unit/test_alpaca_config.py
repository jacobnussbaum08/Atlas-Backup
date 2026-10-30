"""Alpaca settings. Credential strings here are fake test fixtures, not real keys."""

from __future__ import annotations

import pytest

from app.core.config import InvalidSettingsError, UnknownSettingError
from app.data_engine.providers.alpaca.config import (
    AlpacaDataPlan,
    AlpacaFeed,
    AlpacaSettings,
    get_alpaca_settings,
)

FAKE_KEY = "TESTKEYFIXTURE0001"
FAKE_SECRET = "TestSecretFixtureNotReal0001"


@pytest.fixture
def isolated(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return clean_env


def _settings(**values) -> AlpacaSettings:
    return AlpacaSettings(_env_file=None, **values)


def test_starts_without_credentials(isolated):
    settings = get_alpaca_settings()

    assert settings.credentials_configured is False
    assert settings.data_feed is AlpacaFeed.IEX
    assert settings.data_plan is AlpacaDataPlan.BASIC


def test_reads_prefixed_environment_variables(isolated):
    isolated.setenv("ALPACA_API_KEY", FAKE_KEY)
    isolated.setenv("ALPACA_SECRET_KEY", FAKE_SECRET)
    isolated.setenv("ALPACA_DATA_FEED", "sip")
    isolated.setenv("ALPACA_DATA_PLAN", "algo_trader_plus")

    settings = get_alpaca_settings()

    assert settings.credentials_configured is True
    assert settings.data_feed is AlpacaFeed.SIP
    assert settings.data_plan is AlpacaDataPlan.ALGO_TRADER_PLUS


def test_empty_values_mean_not_configured():
    settings = _settings(api_key="", secret_key="  ")
    assert settings.api_key is None
    assert settings.secret_key is None


@pytest.mark.parametrize(
    "values", [{"api_key": FAKE_KEY}, {"secret_key": FAKE_SECRET}], ids=["key-only", "secret-only"]
)
def test_half_configured_credentials_are_rejected(values):
    with pytest.raises(ValueError, match="both be set"):
        _settings(**values)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("WITH SPACE0001", "whitespace"),
        ("short", "implausible length"),
        ("NonAsciiéééé", "ASCII"),
    ],
)
def test_malformed_credentials_are_rejected(value, message):
    with pytest.raises(ValueError, match=message):
        _settings(api_key=value, secret_key=FAKE_SECRET)


def test_credentials_never_appear_in_repr_or_dump():
    settings = _settings(api_key=FAKE_KEY, secret_key=FAKE_SECRET)

    rendered = repr(settings) + str(settings) + str(settings.model_dump())

    assert FAKE_KEY not in rendered
    assert FAKE_SECRET not in rendered


def test_loader_errors_never_contain_credential_values(isolated):
    isolated.setenv("ALPACA_API_KEY", "BAD KEY WITH SPACES SENTINEL")
    isolated.setenv("ALPACA_SECRET_KEY", FAKE_SECRET)

    with pytest.raises(InvalidSettingsError) as excinfo:
        get_alpaca_settings()

    message = str(excinfo.value)
    assert "api_key" in message
    assert "SENTINEL" not in message
    assert FAKE_SECRET not in message


def test_unsupported_feed_is_rejected():
    with pytest.raises(ValueError, match="'iex', 'sip' or 'delayed_sip'"):
        _settings(data_feed="boats")


def test_misspelled_alpaca_variable_is_rejected(isolated):
    isolated.setenv("ALPACA_DATAFEED", "iex")
    with pytest.raises(UnknownSettingError, match="ALPACA_DATAFEED"):
        get_alpaca_settings()
