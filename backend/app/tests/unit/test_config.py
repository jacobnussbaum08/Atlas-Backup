from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings, UnknownSettingError, get_settings

REQUIRED_ENV = {
    "ATLAS_ENVIRONMENT": "development",
    "ATLAS_POSTGRES_HOST": "db.internal",
    "ATLAS_POSTGRES_DB": "atlas",
    "ATLAS_POSTGRES_USER": "atlas",
    "ATLAS_POSTGRES_PASSWORD": "s3cret-value",
    "ATLAS_REDIS_URL": "redis://cache.internal:6379/0",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for key, value in {**REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_loads_and_validates_from_environment(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env file
    _set_env(clean_env)

    settings = get_settings()

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.postgres_port == 5432
    assert settings.log_json is True
    assert settings.database_url.drivername == "postgresql+psycopg"
    assert settings.database_url.host == "db.internal"


def test_missing_required_values_fail_with_field_names(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as excinfo:
        get_settings()

    missing = {err["loc"][0] for err in excinfo.value.errors() if err["type"] == "missing"}
    assert {"environment", "postgres_host", "postgres_db", "postgres_user"} <= missing
    assert {"postgres_password", "redis_url"} <= missing


@pytest.mark.parametrize("environment", ["live", "production", "prod", ""])
def test_unknown_environments_are_rejected(make_settings, environment):
    with pytest.raises(ValidationError):
        make_settings(environment=environment)


def test_secrets_are_masked_in_repr_and_url(make_settings):
    settings = make_settings(postgres_password="very-secret-pw")

    assert "very-secret-pw" not in repr(settings)
    assert "very-secret-pw" not in str(settings.database_url)
    assert settings.database_url.password == "very-secret-pw"


def test_paper_environment_rejects_development_password(make_settings):
    with pytest.raises(ValidationError, match="development default"):
        make_settings(environment="paper", postgres_password="CHANGE_ME_LOCAL_ONLY")


def test_paper_environment_rejects_debug_logging(make_settings):
    with pytest.raises(ValidationError, match="DEBUG"):
        make_settings(environment="paper", postgres_password="Unique-Pw-123", log_level="DEBUG")


def test_paper_environment_accepts_unique_password(make_settings):
    settings = make_settings(environment="paper", postgres_password="Unique-Pw-123")
    assert settings.environment is Environment.PAPER


def test_redis_url_must_not_embed_credentials(make_settings):
    with pytest.raises(ValidationError, match="ATLAS_REDIS_PASSWORD"):
        make_settings(redis_url="redis://:hunter2@cache:6379/0")


def test_empty_redis_password_means_no_password(make_settings):
    assert make_settings(redis_password="").redis_password is None
    assert make_settings(redis_password="pw").redis_password is not None


def test_redis_url_must_be_redis_scheme(make_settings):
    with pytest.raises(ValidationError):
        make_settings(redis_url="http://cache:6379")


def test_log_level_is_normalised_and_validated(make_settings):
    assert make_settings(log_level="warning").log_level == "WARNING"
    with pytest.raises(ValidationError):
        make_settings(log_level="verbose")


@pytest.mark.parametrize("port", [0, 70000])
def test_ports_are_range_checked(make_settings, port):
    with pytest.raises(ValidationError):
        make_settings(postgres_port=port)


def test_cors_origins_accept_comma_separated_env(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_env(clean_env, ATLAS_CORS_ORIGINS="http://localhost:5173, http://127.0.0.1:5173")

    origins = [str(o) for o in get_settings().cors_origins]

    assert origins == ["http://localhost:5173/", "http://127.0.0.1:5173/"]


def test_unknown_atlas_variable_in_env_file_is_rejected(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = [f"{k}={v}" for k, v in REQUIRED_ENV.items()] + ["ATLAS_POSTGRS_PORT=5433"]
    (tmp_path / ".env").write_text("\n".join(lines), encoding="utf-8")

    with pytest.raises(UnknownSettingError, match="ATLAS_POSTGRS_PORT"):
        get_settings()


def test_unknown_atlas_variable_in_process_environment_is_rejected(
    clean_env, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _set_env(clean_env, ATLAS_LIVE_TRADING="true")

    with pytest.raises(UnknownSettingError, match="ATLAS_LIVE_TRADING"):
        get_settings()


def test_test_harness_variables_are_not_application_settings(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_env(
        clean_env,
        ATLAS_TEST_DATABASE_URL="postgresql+psycopg://x@y/z",
        ATLAS_REQUIRE_INTEGRATION="1",
    )

    assert get_settings().environment is Environment.DEVELOPMENT


def test_non_atlas_keys_in_env_file_are_ignored(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = [f"{k}={v}" for k, v in REQUIRED_ENV.items()] + ["POSTGRES_HOST_PORT=5433"]
    (tmp_path / ".env").write_text("\n".join(lines), encoding="utf-8")

    assert get_settings().postgres_port == 5432


def test_env_example_file_is_valid(clean_env, tmp_path, monkeypatch):
    """The shipped .env.example (incl. non-ATLAS compose keys) must load as-is."""
    example = Path(__file__).resolve().parents[4] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = get_settings()

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.redis_password is None


def test_env_file_is_read(clean_env, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = [f"{k}={v}" for k, v in REQUIRED_ENV.items()]
    (tmp_path / ".env").write_text("\n".join(lines), encoding="utf-8")

    assert get_settings().postgres_host == "db.internal"


def test_settings_are_immutable(make_settings):
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.environment = Environment.PAPER


def test_there_is_no_trading_mode_or_live_switch():
    """Paper-only isolation: no configuration field may select live trading (spec 24.1)."""
    names = set(Settings.model_fields)
    assert not {n for n in names if "live" in n or "trading_mode" in n or "broker" in n}
