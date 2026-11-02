"""ASSET_DISCOVERY_* settings: defaults, validation, typo rejection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.asset_discovery.config import AssetDiscoverySettings, get_asset_discovery_settings
from app.core.config import InvalidSettingsError, UnknownSettingError


def test_defaults_are_conservative(clean_env):
    settings = AssetDiscoverySettings(_env_file=None)
    assert settings.enabled is False  # the worker never calls the provider unasked
    assert settings.full_interval_hours == 168
    assert settings.incremental_interval_hours == 24
    assert settings.history_prep_max_assets == 200
    assert settings.assumed_history_start_at == datetime(2016, 1, 1, tzinfo=UTC)
    assert settings.rule_set_code("sip") == "us-equity-sip"
    assert (
        AssetDiscoverySettings(_env_file=None, rule_set="custom").rule_set_code("iex") == "custom"
    )


def test_unknown_key_is_rejected_as_a_typo(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("ASSET_DISCOVERY_ENABLE", "true")
    with pytest.raises(UnknownSettingError):
        get_asset_discovery_settings()


def test_out_of_range_values_are_errors_without_echoing_input(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("ASSET_DISCOVERY_REFRESH_MAX_ASSETS", "999999")
    with pytest.raises(InvalidSettingsError) as caught:
        get_asset_discovery_settings()
    assert "999999" not in str(caught.value)


def test_env_example_documents_and_loads_every_setting(clean_env, tmp_path, monkeypatch):
    from pathlib import Path

    from app.asset_discovery.config import ENV_PREFIX

    example = Path(__file__).resolve().parents[4] / ".env.example"
    text = example.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line.startswith(ENV_PREFIX)}
    assert keys == {f"{ENV_PREFIX}{name}".upper() for name in AssetDiscoverySettings.model_fields}
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    loaded = get_asset_discovery_settings()
    assert loaded == AssetDiscoverySettings(_env_file=None)  # the example states the defaults
