"""Reference data parity, settings and price freshness (Phase 6A)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.accounting.chart import CHART
from app.accounting.config import AccountingSettings, get_accounting_settings
from app.accounting.enums import PriceStatus
from app.accounting.money import CURRENCIES
from app.core.config import InvalidSettingsError, UnknownSettingError
from app.database.models import AssetClass
from app.portfolio.pricing import freshness

MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "0013_portfolio_accounting.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0013", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chart_of_accounts_matches_the_migration_seed():
    seeded = {row[0]: row for row in _migration().CHART}
    assert set(seeded) == set(CHART)
    for code, definition in CHART.items():
        _, name, kind, side, category, rule, amount, functional_only = seeded[code]
        assert (name, kind, side, category, rule, amount, functional_only) == (
            definition.name,
            definition.account_type.value,
            definition.normal_side.value,
            definition.category.value,
            definition.asset_rule.value,
            definition.allows_amount,
            definition.allows_functional_only,
        ), code
        assert _migration().DESCRIPTIONS[code] == definition.description, code


def test_currencies_match_the_migration_seed():
    assert {c: (n, m) for c, n, m in _migration().CURRENCIES} == {
        spec.code: (spec.name, spec.minor_units) for spec in CURRENCIES.values()
    }


def test_settings_defaults_are_the_documented_paper_account(clean_env):
    settings = AccountingSettings(_env_file=None)
    assert settings.opening_funding == Decimal("100000.00")
    assert settings.functional_currency == "USD"


def test_settings_reject_unknown_keys_and_bad_values(clean_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clean_env.setenv("ACCOUNTING_OPENING_FUNDNG", "5")
    with pytest.raises(UnknownSettingError):
        get_accounting_settings()
    get_accounting_settings.cache_clear()
    clean_env.delenv("ACCOUNTING_OPENING_FUNDNG")
    clean_env.setenv("ACCOUNTING_OPENING_FUNDING", "-1")
    with pytest.raises(InvalidSettingsError, match="opening_funding"):
        get_accounting_settings()
    get_accounting_settings.cache_clear()
    clean_env.setenv("ACCOUNTING_OPENING_FUNDING", "250000.00")
    clean_env.setenv("ACCOUNTING_FUNCTIONAL_CURRENCY", "xau")
    with pytest.raises(InvalidSettingsError):
        get_accounting_settings()


SETTINGS = AccountingSettings(_env_file=None)
# Wednesday 2026-11-04: NYSE regular session 14:30-21:00 UTC.
SESSION = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
EVENING = datetime(2026, 11, 4, 23, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("asset_class", "observed", "as_of", "expected"),
    [
        (AssetClass.EQUITY, SESSION - timedelta(minutes=10), SESSION, PriceStatus.FRESH),
        (AssetClass.EQUITY, SESSION - timedelta(minutes=31), SESSION, PriceStatus.STALE),
        # Yesterday's daily bar during today's session is stale.
        (AssetClass.ETF, datetime(2026, 11, 3, 5, 0, tzinfo=UTC), SESSION, PriceStatus.STALE),
        # After the close, today's bar (timestamped at the session date) is fresh.
        (AssetClass.EQUITY, datetime(2026, 11, 4, 5, 0, tzinfo=UTC), EVENING, PriceStatus.FRESH),
        # On Saturday, Friday's bar is the latest session's.
        (AssetClass.EQUITY, datetime(2026, 11, 6, 5, 0, tzinfo=UTC), SATURDAY, PriceStatus.FRESH),
        (AssetClass.EQUITY, datetime(2026, 11, 5, 5, 0, tzinfo=UTC), SATURDAY, PriceStatus.STALE),
        (AssetClass.CRYPTO, SATURDAY - timedelta(minutes=59), SATURDAY, PriceStatus.FRESH),
        (AssetClass.CRYPTO, SATURDAY - timedelta(minutes=61), SATURDAY, PriceStatus.STALE),
        (AssetClass.FOREX, SATURDAY, SATURDAY, PriceStatus.STALE),
    ],
)
def test_price_freshness_rules(asset_class, observed, as_of, expected):
    status, reason = freshness(asset_class, observed, as_of, SETTINGS)
    assert status is expected
    assert (reason is None) == (expected is PriceStatus.FRESH)
