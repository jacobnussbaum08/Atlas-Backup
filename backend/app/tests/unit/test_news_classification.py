"""Deterministic event classification (Phase 3C). Headlines are invented TEST FIXTURES."""

from __future__ import annotations

import pytest

from app.news.classification import (
    CLASSIFIER_VERSION,
    RULES_SHA256,
    classify,
    is_crypto_pair,
    rules_fingerprint,
    taxonomy,
)
from app.news.enums import (
    ClassificationConfidence,
    ClassificationMethod,
    ClassificationSource,
    EventCategory,
    EventScope,
    VerificationStatus,
)


def _categories(result):
    return {a.category for a in result.assignments}


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Fixture Corp Q3 EPS $1.20 Beats $1.10 Estimate", EventCategory.EARNINGS),
        (
            "Analyst Upgrades Fixture Corp To Buy, Raises Price Target To $50",
            EventCategory.ANALYST_RATINGS,
        ),
        ("Fixture Corp To Acquire Example Inc For $2B", EventCategory.MERGERS_ACQUISITIONS),
        ("Fixture Corp Declares Quarterly Dividend Of $0.25", EventCategory.CORPORATE_ACTIONS),
        ("Fixture Corp Names Jane Doe As CEO", EventCategory.MANAGEMENT_CHANGES),
        ("Fixture Corp Unveils New Device Lineup", EventCategory.PRODUCT_LAUNCHES),
        ("FDA Grants Approval For Fixture Therapy", EventCategory.REGULATORY),
        ("Shareholders File Class Action Lawsuit Against Fixture Corp", EventCategory.LITIGATION),
        ("Fed Holds Interest Rates Steady, Powell Signals Patience", EventCategory.CENTRAL_BANKS),
        ("US CPI Rises 0.3% In September; Core Inflation Cools", EventCategory.INFLATION),
        ("Nonfarm Payrolls Increase By 150K; Unemployment Rate At 4.1%", EventCategory.EMPLOYMENT),
        ("US GDP Grows 2.1% In Third Quarter", EventCategory.MACROECONOMIC),
        ("Ceasefire Talks Stall As Military Tensions Rise", EventCategory.GEOPOLITICAL),
        ("Bitcoin Climbs Above $100,000 As Crypto Rally Extends", EventCategory.CRYPTOCURRENCY),
        ("Semiconductor Shortage Hits Automakers Again", EventCategory.INDUSTRY),
        (
            "Fixture Corp Announces Partnership With Example Inc",
            EventCategory.COMPANY_ANNOUNCEMENTS,
        ),
        # keyword-rules-v2: forms missed by v1 on real headlines (2026-11-02, D-076)
        ("Fixture Corp Reports Fiscal Year 2026 Financial Results", EventCategory.EARNINGS),
        ("Executive Departure At Fixture Corp", EventCategory.MANAGEMENT_CHANGES),
        ("Trading Halt: Fixture Corp Halted, News Pending", EventCategory.REGULATORY),
        ("Fixture Bank Agrees To Sell Its Card Division", EventCategory.MERGERS_ACQUISITIONS),
        ("Fixture Corp Issues Shares For Employee Plan", EventCategory.CORPORATE_ACTIONS),
        ("Fixture Corp Executes Staff Cuts", EventCategory.COMPANY_ANNOUNCEMENTS),
    ],
)
def test_each_category_has_working_rules(headline, expected):
    result = classify(headline=headline, summary=None)
    assert result.primary is expected
    primary = next(a for a in result.assignments if a.is_primary)
    assert primary.category is expected
    assert primary.source is ClassificationSource.ATLAS
    assert primary.method is ClassificationMethod.KEYWORD_RULES
    assert primary.verification is VerificationStatus.UNVERIFIED  # never "verified"
    assert primary.confidence is ClassificationConfidence.MEDIUM  # a headline match
    assert primary.evidence["matches"][0]["field"] == "headline"


def test_unmatched_articles_are_other_with_no_confidence():
    result = classify(headline="Something Happened Somewhere", summary="Nothing specific.")
    assert result.primary is EventCategory.OTHER
    [only] = result.assignments
    assert only.method is ClassificationMethod.DEFAULT
    assert only.confidence is ClassificationConfidence.NONE
    assert only.score == 0
    assert only.is_primary


def test_summary_only_evidence_is_low_confidence():
    result = classify(headline="Fixture Corp Update", summary="The board declared a dividend.")
    corp = next(a for a in result.assignments if a.category is EventCategory.CORPORATE_ACTIONS)
    assert corp.score == 1
    assert corp.confidence is ClassificationConfidence.LOW


def test_multiple_categories_and_primary_by_score_then_taxonomy_order():
    result = classify(
        headline="Fixture Corp Beats Earnings Estimates, Raises Dividend",
        summary="Analysts expect guidance to improve.",
    )
    assert {EventCategory.EARNINGS, EventCategory.CORPORATE_ACTIONS} <= _categories(result)
    assert result.primary is EventCategory.EARNINGS
    assert sum(a.is_primary for a in result.assignments) == 1
    # Equal scores: the earlier category in the taxonomy wins (deterministic tie-break).
    tie = classify(headline="Dividend And Lawsuit", summary=None)
    assert tie.primary is EventCategory.CORPORATE_ACTIONS


def test_case_sensitive_acronyms_do_not_match_ordinary_words():
    assert EventCategory.CENTRAL_BANKS not in _categories(
        classify(headline="Customers fed up with delays", summary=None)
    )
    assert EventCategory.EARNINGS not in _categories(
        classify(headline="Fixture Corp steps up output", summary="eps and reps")
    )
    assert EventCategory.REGULATORY not in _categories(
        classify(headline="The second plant opens", summary=None)
    )
    # Word boundaries: "warehouse" is not "war", "acquired taste" still mentions acquired.
    assert EventCategory.GEOPOLITICAL not in _categories(
        classify(headline="Warehouse expansion", summary=None)
    )


def test_crypto_pairs_among_symbols_add_a_symbol_rule_label():
    assert is_crypto_pair("BTCUSD")
    assert is_crypto_pair("ETH/USD")
    assert not is_crypto_pair("AAPL")
    result = classify(headline="Market Update", summary=None, symbols=["BTCUSD"])
    crypto = next(a for a in result.assignments if a.category is EventCategory.CRYPTOCURRENCY)
    assert crypto.method is ClassificationMethod.SYMBOL_RULES
    assert crypto.evidence["matches"][-1] == {
        "field": "symbols",
        "rule": "crypto_pair",
        "text": "BTCUSD",
    }


def test_provider_labels_are_distinguished_and_preferred_for_primary():
    result = classify(
        headline="Fixture Corp Announces Partnership",
        summary=None,
        provider_categories=["Earnings", "Some Unknown Label"],
    )
    provider = [a for a in result.assignments if a.source is ClassificationSource.PROVIDER]
    assert {a.category for a in provider} == {EventCategory.EARNINGS, EventCategory.OTHER}
    assert all(a.verification is VerificationStatus.PROVIDER_ASSERTED for a in provider)
    assert all(a.method is ClassificationMethod.PROVIDER_METADATA for a in provider)
    assert result.primary is EventCategory.EARNINGS
    primary = next(a for a in result.assignments if a.is_primary)
    assert primary.source is ClassificationSource.PROVIDER
    other = next(a for a in provider if a.category is EventCategory.OTHER)
    assert other.provider_label == "Some Unknown Label"


def test_scope_depends_on_mapped_assets_and_category():
    company = classify(headline="Fixture Corp Announces Partnership", summary=None)
    macro = classify(headline="US CPI Rises 0.3%", summary=None)
    assert company.scope(mapped_assets=2) is EventScope.ASSET
    assert company.scope(mapped_assets=0) is EventScope.UNLINKED
    assert macro.scope(mapped_assets=0) is EventScope.MARKET


def test_classification_is_deterministic_and_versioned():
    args = {"headline": "Fixture Corp Beats Estimates", "summary": "EPS rose."}
    assert classify(**args) == classify(**args)
    assert (
        classify(**args).classifier_version
        == CLASSIFIER_VERSION
        == "news-taxonomy-v1/keyword-rules-v2"
    )
    assert rules_fingerprint() == RULES_SHA256
    assert len(RULES_SHA256) == 64


def test_taxonomy_lists_every_category_with_its_rules():
    listed = taxonomy()
    assert [c["category"] for c in listed] == [c.value for c in EventCategory]
    assert all(c["rules"] for c in listed if c["category"] != "OTHER")
    assert next(c for c in listed if c["category"] == "INFLATION")["market_wide"] is True
