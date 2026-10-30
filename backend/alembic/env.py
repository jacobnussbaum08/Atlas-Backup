"""Alembic migration environment.

The target database comes from, in priority order:
1. ``config.attributes["connection"]`` - an open connection (tests);
2. ``config.attributes["database_url"]`` - an explicit URL (tests/tools);
3. the validated Atlas settings (normal operation).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.models import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    url = config.attributes.get("database_url") or get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return

    url = config.attributes.get("database_url")
    if url is None:
        settings = get_settings()
        configure_logging(settings.log_level, json_output=settings.log_json)
        url = settings.database_url
    engine = create_engine(
        url, poolclass=pool.NullPool, connect_args={"options": "-c timezone=UTC"}
    )
    try:
        with engine.connect() as connection:
            _configure(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
