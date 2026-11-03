"""News normalization and sanitization (Phase 3C) on constructed TEST FIXTURE items."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.news.enums import ArticleOrigin
from app.news.providers.alpaca.normalize import NewsFormatError, normalize_article
from app.news.schemas import content_hash
from app.news.text import clean_text, clean_url, strip_html
from app.tests.fixtures.alpaca_news import news_item

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
RECEIVED = datetime(2026, 3, 2, 14, 30, 2, tzinfo=UTC)


def _normalize(item, *, store_content=False, origin=ArticleOrigin.HISTORICAL_REST):
    return normalize_article(item, origin=origin, received_at=RECEIVED, store_content=store_content)


def test_fields_are_normalized_and_timestamps_kept_apart():
    updated = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    article = _normalize(
        news_item(
            62801667,
            T0,
            headline="Fixture Corp Reports Q3 Results",
            summary="A summary.",
            symbols=["aapl", "AAPL", " msft "],
            source="Benzinga",
            url="https://example.com/a",
            updated_at=updated,
        )
    )
    assert article.provider == "alpaca"
    assert article.provider_article_id == "62801667"
    assert article.symbols == ("AAPL", "MSFT")  # upper-cased, de-duplicated, order kept
    assert article.source == "benzinga"
    assert article.published_at == T0  # the provider's created_at
    assert article.provider_updated_at == updated
    assert article.received_at == RECEIVED  # never substituted for publication
    assert article.origin is ArticleOrigin.HISTORICAL_REST


def test_missing_information_stays_missing():
    # Observed on real responses: missing summary/author/content arrive as "".
    article = _normalize(news_item(1, T0, summary="", author="", content="", symbols=[]))
    assert article.summary is None
    assert article.author is None
    assert article.url is None
    assert article.symbols == ()
    assert article.has_body is False
    assert article.content is None
    assert article.body_sha256 is None
    no_source = _normalize({**news_item(2, T0), "source": None, "summary": None})
    assert no_source.source is None
    assert no_source.summary is None


def test_html_is_stripped_and_entities_decoded_once():
    article = _normalize(
        news_item(
            3,
            T0,
            headline="<b>Fixture</b> &amp; Co <script>alert('x')</script>beats estimates",
            summary="<p>First&nbsp;line</p><p>second &lt;b&gt;</p><!-- hidden -->",
        )
    )
    assert article.headline == "Fixture & Co beats estimates"
    assert article.summary == "First line second <b>"  # the decoded text, never re-parsed
    assert "script" not in article.headline


def test_control_and_bidi_characters_are_removed():
    assert clean_text("A‮B\x00C​\tD\nE", max_length=100) == "ABC D E"


def test_only_http_urls_without_credentials_are_kept():
    assert clean_url("https://www.example.com/news/1?x=1") == "https://www.example.com/news/1?x=1"
    for bad in (
        "javascript:alert(1)",
        "data:text/html;base64,AAAA",
        "ftp://example.com/a",
        "https://user:pass@example.com/",
        "//example.com/a",
        "https://exa mple.com",
        "",
        None,
        42,
    ):
        assert clean_url(bad) is None, bad
    article = _normalize(news_item(4, T0, url="javascript:alert(1)"))
    assert article.url is None
    assert article.provider_payload["url"] == "javascript:alert(1)"  # provenance kept verbatim


def test_body_is_kept_only_when_permitted_but_always_hashed():
    item = news_item(5, T0, content="<p>Full <i>article</i> text</p>")
    withheld = _normalize(item)
    assert withheld.has_body is True
    assert withheld.content is None
    assert withheld.body_sha256 is not None
    assert "content" not in withheld.provider_payload  # the body never enters provenance
    assert withheld.provider_payload["content_length"] == len("<p>Full <i>article</i> text</p>")
    kept = _normalize(item, store_content=True)
    assert kept.content == "Full article text"
    assert kept.body_sha256 == withheld.body_sha256


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"id": None}, "id"),
        ({"id": True}, "id"),
        ({"id": -5}, "id"),
        ({"headline": ""}, "headline"),
        ({"headline": "<p> </p>"}, "headline"),
        ({"headline": 7}, "headline"),
        ({"created_at": None}, "created_at"),
        ({"created_at": "yesterday"}, "created_at"),
        ({"symbols": "AAPL"}, "symbols"),
    ],
)
def test_malformed_items_are_rejected_with_a_reason(change, reason):
    with pytest.raises(NewsFormatError, match=reason):
        _normalize({**news_item(6, T0), **change})


def test_string_ids_and_images_are_normalized():
    article = _normalize(
        {
            **news_item(7, T0),
            "id": "0007",
            "images": [
                {"size": "thumb", "url": "https://img.example.com/t.jpg"},
                {"size": "huge", "url": "https://img.example.com/h.jpg"},
                {"size": "small", "url": "javascript:x"},
                "nonsense",
            ],
        }
    )
    assert article.provider_article_id == "7"
    assert [(i.size, i.url) for i in article.images] == [("thumb", "https://img.example.com/t.jpg")]


def test_content_hash_covers_stored_fields_and_ignores_symbol_order():
    base: dict[str, Any] = {
        "headline": "H",
        "summary": None,
        "author": None,
        "source": "s",
        "url": None,
    }
    assert content_hash(**base, symbols=["A", "B"]) == content_hash(**base, symbols=["B", "A"])
    assert content_hash(**base, symbols=["A"]) != content_hash(
        **{**base, "headline": "H2"}, symbols=["A"]
    )


def test_strip_html_handles_plain_text():
    assert strip_html("no markup") == "no markup"
