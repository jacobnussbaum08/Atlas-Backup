"""News settings (``NEWS_*``) and the economic-calendar status (Phase 3C)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import UnknownSettingError
from app.news.config import NewsSettings, get_news_settings
from app.news.economic_calendar import CALENDAR_PROVIDERS, calendar_status
from app.news.enums import CalendarAvailability


def test_defaults_are_conservative():
    settings = NewsSettings(_env_file=None)
    assert settings.provider == "alpaca"
    assert settings.store_content is False  # licence terms unconfirmed (D-074)
    assert settings.stream_enabled is False
    assert settings.stream_symbols == ("*",)
    assert settings.stream_all_news
    assert settings.historical_page_size == 50
    assert settings.stream_recovery_overlap_seconds == 300


def test_symbols_parse_from_a_comma_list(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("NEWS_STREAM_SYMBOLS", " aapl, MSFT ,aapl")
    settings = get_news_settings()
    assert settings.stream_symbols == ("AAPL", "MSFT")
    assert not settings.stream_all_news


@pytest.mark.parametrize("value", ["*,AAPL", "", "WAY-TOO-LONG-SYMBOL-NAME"])
def test_invalid_symbol_lists_are_rejected(value):
    with pytest.raises(ValidationError):
        NewsSettings(_env_file=None, stream_symbols=value)


def test_page_size_cannot_exceed_the_provider_maximum():
    with pytest.raises(ValidationError):
        NewsSettings(_env_file=None, historical_page_size=51)


def test_unknown_news_keys_are_typos(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("NEWS_STREAM_ENABLE", "true")  # missing D
    with pytest.raises(UnknownSettingError, match="NEWS_STREAM_ENABLE"):
        get_news_settings()


def test_economic_calendar_is_reported_as_not_configured():
    assert CALENDAR_PROVIDERS == {}
    status = calendar_status()
    assert status.availability is CalendarAvailability.NOT_CONFIGURED
    assert status.provider is None
    assert any("not an economic calendar" in note for note in status.notes)


def test_env_example_documents_and_loads_every_setting(clean_env, tmp_path, monkeypatch):
    from pathlib import Path

    from app.news.config import ENV_PREFIX

    example = Path(__file__).resolve().parents[4] / ".env.example"
    text = example.read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in text.splitlines() if line.startswith(ENV_PREFIX)}
    assert keys == {f"{ENV_PREFIX}{name}".upper() for name in NewsSettings.model_fields}
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_news_settings() == NewsSettings(_env_file=None)  # the example states the defaults
