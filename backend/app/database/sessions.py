"""Engine and session management.

Atlas uses synchronous SQLAlchemy with psycopg 3. Each connection is pinned to
UTC and given a statement timeout so a stuck query cannot hold locks forever.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


def build_engine(settings: Settings, url: URL | str | None = None) -> Engine:
    return create_engine(
        url or settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_seconds,
            "application_name": settings.service_name,
            "options": f"-c timezone=UTC -c statement_timeout={settings.db_statement_timeout_ms}",
        },
    )


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
