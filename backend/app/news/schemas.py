"""Provider-independent news records (Phase 3C).

Adapters turn provider payloads into :class:`ProviderNewsArticle` (one *observation* of an
article: what the provider sent, when Atlas received it and how). The ingestion store turns
observations into articles and revisions. Missing fields stay ``None`` / empty: an article
without a summary, author, URL or symbols is stored as such, never filled in.

Two hashes identify what an observation says:

* ``content_sha256``: the stored, sanitized fields (headline, summary, author, source, URL,
  sorted symbols). Equal hashes mean nothing Atlas keeps has changed.
* ``body_sha256``: the article body, when the observation carried one. Only a hash: the
  body itself is kept only when ``NEWS_STORE_CONTENT`` allows it (D-074). Observations
  without a body (REST requests without content) never contradict ones with a body.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data_engine.schemas import UtcDatetime
from app.news.enums import ArticleOrigin
from app.news.text import canonical_json, sha256_hex


class NewsImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    size: str
    url: str


class ProviderNewsArticle(BaseModel):
    """One observation of one article, normalized and sanitized by the adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    provider_article_id: str = Field(min_length=1, max_length=128)
    headline: str = Field(min_length=1)
    summary: str | None = None
    author: str | None = None
    # Publisher/feed code as the provider names it (e.g. "benzinga"); None if not reported.
    source: str | None = None
    url: str | None = None
    # Provider symbols in the provider's order, de-duplicated, upper-case.
    symbols: tuple[str, ...] = ()
    # Provider timestamps, preserved exactly (UTC).
    published_at: UtcDatetime
    provider_updated_at: UtcDatetime | None = None
    # The body only when retention is permitted; ``has_body`` says whether one was sent.
    content: str | None = None
    has_body: bool = False
    body_sha256: str | None = None
    images: tuple[NewsImage, ...] = ()
    # Provider-assigned categories, verbatim (Alpaca supplies none).
    provider_categories: tuple[str, ...] = ()
    # The provider item without its body (verbatim otherwise), for provenance.
    provider_payload: dict[str, Any] = Field(default_factory=dict)
    origin: ArticleOrigin
    received_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.content is not None and not self.has_body:
            raise ValueError("content given for an observation without a body")
        if self.body_sha256 is not None and not self.has_body:
            raise ValueError("body hash given for an observation without a body")
        return self

    @property
    def content_sha256(self) -> str:
        return content_hash(
            headline=self.headline,
            summary=self.summary,
            author=self.author,
            source=self.source,
            url=self.url,
            symbols=self.symbols,
        )


def content_hash(
    *,
    headline: str,
    summary: str | None,
    author: str | None,
    source: str | None,
    url: str | None,
    symbols: tuple[str, ...] | list[str],
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "headline": headline,
                "summary": summary,
                "author": author,
                "source": source,
                "url": url,
                "symbols": sorted(symbols),
            }
        )
    )


class MalformedArticle(BaseModel):
    """A provider item that could not be normalized. Reported, never silently dropped."""

    model_config = ConfigDict(frozen=True)

    provider_article_id: str | None
    reason: str
    item: dict[str, Any]


class NewsQuery(BaseModel):
    """One historical retrieval. ``start``/``end`` are inclusive; empty symbols = all news."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = ()
    start: UtcDatetime
    end: UtcDatetime
    page_size: int = Field(default=50, ge=1)
    include_content: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols contains duplicates")
        return self


class NewsPage(BaseModel):
    """One page of a historical response, normalized."""

    model_config = ConfigDict(frozen=True)

    page_number: int = Field(ge=1)
    articles: tuple[ProviderNewsArticle, ...]
    malformed: tuple[MalformedArticle, ...] = ()
    next_page_token: str | None
    requested_at: UtcDatetime
    received_at: UtcDatetime


class NewsStreamBatch(BaseModel):
    """One decoded stream frame: articles, malformed items and control notices."""

    model_config = ConfigDict(frozen=True)

    received_at: UtcDatetime
    articles: tuple[ProviderNewsArticle, ...] = ()
    malformed: tuple[MalformedArticle, ...] = ()
    notices: tuple[dict[str, Any], ...] = ()


class NewsProviderCapabilities(BaseModel):
    """What a news adapter can deliver (not what a subscription is entitled to)."""

    model_config = ConfigDict(frozen=True)

    provider: str
    display_name: str
    historical: bool
    streaming: bool
    max_page_size: int | None
    symbol_filter: bool
    wildcard_stream: bool
    provider_categories: bool
    # The market-data provider whose symbol namespace article symbols use (asset mapping).
    symbol_namespace: str
    content_policy: str
    notes: tuple[str, ...] = ()


class NewsAuthenticationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    status: str
    checked_at: datetime
    detail: str | None = None


__all__ = [
    "MalformedArticle",
    "NewsAuthenticationResult",
    "NewsImage",
    "NewsPage",
    "NewsProviderCapabilities",
    "NewsQuery",
    "NewsStreamBatch",
    "ProviderNewsArticle",
    "content_hash",
]
