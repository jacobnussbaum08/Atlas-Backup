"""Alpaca news payload -> :class:`ProviderNewsArticle` (Phase 3C).

Fields (https://docs.alpaca.markets/us/reference/news-3, checked 2026-11-02; the stream
sends the same object with ``"T": "n"``): ``id`` (int64), ``headline``, ``author``,
``created_at`` and ``updated_at`` (RFC-3339), ``summary``, ``content`` (may contain HTML),
``images`` (``{size, url}``), ``symbols``, ``source`` and an optional ``url``.

Observed on real responses (2026-11-02): missing values arrive as empty strings (author,
summary, content), some articles have no symbols, and sources include ``benzinga`` and
``fiscal.ai``. Empty strings become ``None``; nothing is filled in.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.data_engine.providers.alpaca.normalize import BarFormatError, parse_timestamp
from app.news.enums import ArticleOrigin
from app.news.schemas import NewsImage, ProviderNewsArticle
from app.news.text import (
    MAX_AUTHOR,
    MAX_CONTENT,
    MAX_HEADLINE,
    MAX_SOURCE,
    MAX_SUMMARY,
    clean_text,
    clean_url,
)

PROVIDER_ID = "alpaca"
MAX_SYMBOLS = 500
MAX_SYMBOL_LENGTH = 32
_IMAGE_SIZES = frozenset({"thumb", "small", "large"})


class NewsFormatError(ValueError):
    """The item cannot be normalized (required field missing or malformed)."""


def _article_id(value: object) -> str:
    if isinstance(value, bool):
        raise NewsFormatError("field 'id' is not an integer")
    if isinstance(value, int):
        if value <= 0:
            raise NewsFormatError("field 'id' is not positive")
        return str(value)
    if isinstance(value, str) and value.isdigit() and len(value) <= 20:
        return str(int(value))
    raise NewsFormatError("missing or malformed field 'id'")


def _timestamp(item: dict[str, Any], key: str, *, required: bool) -> datetime | None:
    value = item.get(key)
    if value is None or value == "":
        if required:
            raise NewsFormatError(f"missing required field '{key}'")
        return None
    try:
        return parse_timestamp(value)
    except BarFormatError as exc:
        raise NewsFormatError(f"field '{key}': {exc}") from None


def _symbols(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise NewsFormatError("field 'symbols' is not a list")
    symbols: list[str] = []
    for entry in value[:MAX_SYMBOLS]:
        if not isinstance(entry, str):
            continue
        symbol = "".join(entry.split()).upper()
        if symbol and len(symbol) <= MAX_SYMBOL_LENGTH and symbol.isprintable():
            symbols.append(symbol)
    return tuple(dict.fromkeys(symbols))


def _images(value: object) -> tuple[NewsImage, ...]:
    if not isinstance(value, list):
        return ()
    images: list[NewsImage] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        size, url = entry.get("size"), clean_url(entry.get("url"))
        if isinstance(size, str) and size in _IMAGE_SIZES and url is not None:
            images.append(NewsImage(size=size, url=url))
    return tuple(images)


def normalize_article(
    item: dict[str, Any],
    *,
    origin: ArticleOrigin,
    received_at: datetime,
    store_content: bool,
) -> ProviderNewsArticle:
    """Normalize one Alpaca news object. Raises :class:`NewsFormatError`."""
    article_id = _article_id(item.get("id"))
    raw_headline = item.get("headline")
    if raw_headline is not None and not isinstance(raw_headline, str):
        raise NewsFormatError("field 'headline' is not a string")
    headline = clean_text(raw_headline, max_length=MAX_HEADLINE)
    if headline is None:
        raise NewsFormatError("article has no headline")
    published_at = _timestamp(item, "created_at", required=True)
    assert published_at is not None  # noqa: S101  # required=True
    updated_at = _timestamp(item, "updated_at", required=False)

    raw_content = item.get("content")
    has_body = isinstance(raw_content, str) and raw_content.strip() != ""
    body_sha256 = (
        hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
        if has_body and isinstance(raw_content, str)
        else None
    )
    content = (
        clean_text(raw_content, max_length=MAX_CONTENT) if has_body and store_content else None
    )

    # Provenance: the provider object as sent, without its body (D-074) or stream type tag.
    payload = {k: v for k, v in item.items() if k not in ("content", "T")}
    if has_body:
        payload["content_length"] = len(raw_content) if isinstance(raw_content, str) else 0

    return ProviderNewsArticle(
        provider=PROVIDER_ID,
        provider_article_id=article_id,
        headline=headline,
        summary=clean_text(item.get("summary"), max_length=MAX_SUMMARY),
        author=clean_text(item.get("author"), max_length=MAX_AUTHOR),
        source=_source(item.get("source")),
        url=clean_url(item.get("url")),
        symbols=_symbols(item.get("symbols")),
        published_at=published_at,
        provider_updated_at=updated_at,
        content=content,
        has_body=has_body,
        body_sha256=body_sha256,
        images=_images(item.get("images")),
        provider_categories=(),  # Alpaca supplies no category metadata
        provider_payload=payload,
        origin=origin,
        received_at=received_at,
    )


def _source(value: object) -> str | None:
    text = clean_text(value, max_length=MAX_SOURCE)
    return text.lower() if text else None


__all__ = ["PROVIDER_ID", "NewsFormatError", "normalize_article"]
