"""Wiring of the news components for the API, the CLI and the stream worker."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.news.config import NewsSettings, get_news_settings
from app.news.ingestion import NewsIngestionService
from app.news.providers.base import NewsProvider
from app.news.providers.registry import create_news_provider


@dataclass(frozen=True)
class NewsContext:
    settings: NewsSettings
    provider: NewsProvider
    service: NewsIngestionService
    session_factory: sessionmaker[Session]
    clock: Clock


def build_news_context(
    session_factory: sessionmaker[Session],
    clock: Clock,
    settings: NewsSettings | None = None,
    provider: NewsProvider | None = None,
) -> NewsContext:
    settings = settings or get_news_settings()
    provider = provider or create_news_provider(settings, clock)
    service = NewsIngestionService(session_factory, provider, settings, clock)
    return NewsContext(settings, provider, service, session_factory, clock)


__all__ = ["NewsContext", "build_news_context"]
