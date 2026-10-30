"""Integration fixtures backed by a real PostgreSQL server.

``ATLAS_TEST_DATABASE_URL`` must point at a server where the user may create
databases (e.g. the docker-compose ``postgres`` service). Each test session
creates a uniquely named throw-away database, migrates it to head with the real
Alembic migrations, and drops it afterwards.

Without the variable these tests are skipped, unless
``ATLAS_REQUIRE_INTEGRATION=1`` is set, in which case they fail instead.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[4]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _require(env_var: str, purpose: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    message = f"{env_var} is not set; {purpose} integration tests need a real server"
    if os.environ.get("ATLAS_REQUIRE_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


def alembic_config(url: URL) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["database_url"] = url
    return config


@pytest.fixture(scope="session")
def server_url() -> URL:
    return make_url(_require("ATLAS_TEST_DATABASE_URL", "PostgreSQL")).set(
        drivername="postgresql+psycopg"
    )


@pytest.fixture(scope="session")
def database_url(server_url: URL) -> Iterator[URL]:
    """A fresh, empty database for this test session."""
    name = f"atlas_it_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield server_url.set(database=name)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def migrated_url(database_url: URL) -> URL:
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture(scope="session")
def engine(migrated_url: URL) -> Iterator[Engine]:
    engine = create_engine(migrated_url, connect_args={"options": "-c timezone=UTC"})
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
