"""Sanitization of untrusted provider text (spec §24: news and outside text are untrusted).

Provider fields may contain HTML (Alpaca documents that content can), entities, control
characters or absurd lengths. Atlas stores plain text only:

* tags are removed (an HTML parser, not a regular expression, so ``<script>`` bodies and
  comments are dropped rather than kept as text), entities are decoded once;
* control and format characters are removed (except ordinary whitespace, which is
  collapsed), so text cannot reorder or hide itself in a terminal or a page;
* empty results are ``None``: a missing summary or author stays missing (never "").

URLs are kept only when they are absolute ``http``/``https`` URLs without credentials;
``javascript:``, ``data:`` and the like are rejected. Dashboards still render every field
as text, never as HTML.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

MAX_HEADLINE = 1000
MAX_SUMMARY = 4000
MAX_AUTHOR = 256
MAX_SOURCE = 64
MAX_URL = 2048
MAX_CONTENT = 200_000

_DROPPED_ELEMENTS = frozenset({"script", "style", "template", "noscript", "iframe", "object"})
_BLOCK_ELEMENTS = frozenset({"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED_ELEMENTS:
            self._skip += 1
        elif tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_ELEMENTS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_ELEMENTS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def strip_html(value: str) -> str:
    """Plain text of an HTML (or plain) string; entities decoded exactly once."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def clean_text(value: object, *, max_length: int) -> str | None:
    """Untrusted provider text -> plain, single-spaced text, or ``None`` when empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = strip_html(value) if ("<" in value or "&" in value) else value
    text = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch)[0] != "C"
    )
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    return text


def clean_url(value: object) -> str | None:
    """An absolute http(s) URL without embedded credentials, or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL or any(ch.isspace() for ch in candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    return candidate


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_AUTHOR",
    "MAX_CONTENT",
    "MAX_HEADLINE",
    "MAX_SOURCE",
    "MAX_SUMMARY",
    "MAX_URL",
    "canonical_json",
    "clean_text",
    "clean_url",
    "sha256_hex",
    "strip_html",
]
