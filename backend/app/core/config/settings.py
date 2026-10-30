"""Application configuration.

All configuration is read from environment variables prefixed with ``ATLAS_``
(optionally from a ``.env`` file) and validated once at startup. Invalid or
missing configuration aborts startup instead of falling back to guesses.

There is intentionally no trading-mode setting: Atlas is paper-trading only
(spec section 24.1), so the mode is a code constant, not a configuration switch.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from dotenv import dotenv_values
from pydantic import (
    AnyHttpUrl,
    Field,
    RedisDsn,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import URL

ENV_PREFIX = "ATLAS_"
ENV_FILE = ".env"

# ATLAS_* keys that belong to the test harness, not to the application.
_NON_APPLICATION_KEYS = frozenset({"ATLAS_REQUIRE_INTEGRATION"})
_NON_APPLICATION_PREFIXES = ("ATLAS_TEST_",)

# Development-only credentials shipped in .env.example. They are accepted in the
# development and test environments and rejected in the paper environment.
INSECURE_DEFAULT_PASSWORDS = frozenset({"atlas", "CHANGE_ME_LOCAL_ONLY", "postgres", "password"})


class UnknownSettingError(ValueError):
    """An ATLAS_* variable does not correspond to any setting (usually a typo)."""


class InvalidSettingsError(ValueError):
    """Settings failed validation. The message names fields but never contains input values."""


def describe_validation_error(exc: ValidationError, title: str = "Settings") -> str:
    """Render a pydantic error without input values (they may be credentials)."""
    lines = [f"{title}: {exc.error_count()} invalid value(s)"]
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        location = ".".join(str(part) for part in error["loc"]) or "(settings)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PAPER = "paper"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Validated runtime configuration. Construct via :func:`get_settings`."""

    # extra="ignore" lets .env also hold keys for other tools (e.g. Docker Compose
    # host ports); misspelled ATLAS_* keys are caught by find_unknown_atlas_keys().
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # ------------------------------------------------------------ application
    environment: Environment
    service_name: str = Field(default="atlas-ai-trader", min_length=1)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: Annotated[list[AnyHttpUrl], NoDecode] = Field(default_factory=list)

    # Shared secret required in X-Atlas-Operator-Token for operational endpoints
    # (e.g. starting a historical fetch). Unset: allowed only in development/test.
    operator_token: SecretStr | None = None

    # ------------------------------------------------------------ logging
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # ------------------------------------------------------------ PostgreSQL
    postgres_host: str = Field(min_length=1)
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = Field(min_length=1)
    postgres_user: str = Field(min_length=1)
    postgres_password: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=100)

    # ------------------------------------------------------------ Redis
    redis_url: RedisDsn
    redis_password: SecretStr | None = None
    redis_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # ------------------------------------------------------------ validators
    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("redis_password", "operator_token", mode="before")
    @classmethod
    def _empty_password_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("redis_url")
    @classmethod
    def _redis_url_without_credentials(cls, value: RedisDsn) -> RedisDsn:
        if value.password or value.username:
            raise ValueError(
                "ATLAS_REDIS_URL must not embed credentials; use ATLAS_REDIS_PASSWORD instead"
            )
        return value

    @model_validator(mode="after")
    def _paper_environment_requires_real_secrets(self) -> Self:
        if self.environment is Environment.PAPER:
            if self.postgres_password.get_secret_value() in INSECURE_DEFAULT_PASSWORDS:
                raise ValueError(
                    "ATLAS_POSTGRES_PASSWORD uses a development default; "
                    "set a unique password for the paper environment"
                )
            if self.log_level == "DEBUG":
                raise ValueError("ATLAS_LOG_LEVEL=DEBUG is not permitted in the paper environment")
        return self

    # ------------------------------------------------------------ derived values
    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL. Rendering it with ``str()`` masks the password."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


def find_unknown_keys(
    keys: Iterable[str], prefixes: tuple[str, ...], known: Iterable[str]
) -> list[str]:
    """Return keys under ``prefixes`` that are not in ``known`` (case-insensitive).

    Test-harness keys (``ATLAS_TEST_*``, ``ATLAS_REQUIRE_INTEGRATION``) are never reported.
    """
    known_upper = {name.upper() for name in known}
    unknown = {
        key.upper()
        for key in keys
        if key.upper().startswith(prefixes)
        and key.upper() not in known_upper
        and key.upper() not in _NON_APPLICATION_KEYS
        and not key.upper().startswith(_NON_APPLICATION_PREFIXES)
    }
    return sorted(unknown)


def find_unknown_atlas_keys(keys: Iterable[str]) -> list[str]:
    """Return ATLAS_* keys that match no setting, so typos fail instead of being ignored."""
    known = {f"{ENV_PREFIX}{name}" for name in Settings.model_fields}
    return find_unknown_keys(keys, (ENV_PREFIX,), known)


def environment_keys(env_file: str | Path = ENV_FILE) -> set[str]:
    """Names (never values) of variables in the process environment and the ``.env`` file."""
    keys = set(os.environ)
    path = Path(env_file)
    if path.is_file():
        keys |= {key for key in dotenv_values(path, encoding="utf-8") if key}
    return keys


def reject_unknown_keys(prefixes: tuple[str, ...], known: Iterable[str]) -> None:
    unknown = find_unknown_keys(environment_keys(), prefixes, known)
    if unknown:
        raise UnknownSettingError(f"Unknown Atlas setting(s): {', '.join(unknown)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises ``UnknownSettingError`` for unrecognised ATLAS_* keys in the process
    environment or ``.env``, and ``pydantic.ValidationError`` for invalid values.
    """
    reject_unknown_keys((ENV_PREFIX,), {f"{ENV_PREFIX}{name}" for name in Settings.model_fields})
    return Settings()  # values come from the environment and .env
