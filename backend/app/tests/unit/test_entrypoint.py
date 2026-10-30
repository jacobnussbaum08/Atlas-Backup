from __future__ import annotations

import pytest

from app.__main__ import main

VALID_ATLAS_ENV = {
    "ATLAS_ENVIRONMENT": "development",
    "ATLAS_POSTGRES_HOST": "127.0.0.1",
    "ATLAS_POSTGRES_DB": "atlas",
    "ATLAS_POSTGRES_USER": "atlas",
    "ATLAS_POSTGRES_PASSWORD": "CHANGE_ME_LOCAL_ONLY",
    "ATLAS_REDIS_URL": "redis://127.0.0.1:6379/0",
    "ATLAS_LOG_LEVEL": "CRITICAL",
}
FAKE_KEY = "TESTKEYFIXTURE0001"  # fake fixture, not a real credential


@pytest.fixture
def env(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in VALID_ATLAS_ENV.items():
        clean_env.setenv(key, value)
    return clean_env


def test_entrypoint_refuses_to_start_with_invalid_configuration(
    clean_env, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("ATLAS_ENVIRONMENT", "live")

    assert main() == 2
    err = capsys.readouterr().err
    assert "Invalid Atlas configuration" in err
    assert "environment" in err


def test_entrypoint_never_prints_invalid_input_values(env, capsys):
    env.setenv("ATLAS_ENVIRONMENT", "paper")  # paper rejects the development password

    assert main() == 2
    err = capsys.readouterr().err
    assert "CHANGE_ME_LOCAL_ONLY" not in err


def test_half_configured_alpaca_credentials_stop_startup_without_leaking(env, capsys):
    env.setenv("ALPACA_API_KEY", FAKE_KEY)

    assert main() == 2
    err = capsys.readouterr().err
    assert "ALPACA_SECRET_KEY" in err
    assert FAKE_KEY not in err


def test_streaming_without_credentials_stops_startup(env, capsys):
    env.setenv("MARKET_DATA_STREAM_ENABLED", "true")
    env.setenv("MARKET_DATA_SYMBOLS", "SPY")

    assert main() == 2
    assert "credentials_not_configured" in capsys.readouterr().err


def test_misspelled_market_data_variable_stops_startup(env, capsys):
    env.setenv("MARKET_DATA_STREAMING", "true")

    assert main() == 2
    assert "MARKET_DATA_STREAMING" in capsys.readouterr().err
