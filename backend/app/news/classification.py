"""Deterministic, versioned event classification of news (Phase 3C, D-076).

Taxonomy ``news-taxonomy-v1`` (``EventCategory``) and rules ``keyword-rules-v2`` (v2 added
rules for misses observed on real headlines on 2026-11-02: fiscal-year/financial results,
executive departures, trading halts, divestitures, staff cuts, share issuance; D-076):

* Each category has named regular-expression rules (word-bounded, mostly
  case-insensitive). A rule matching the **headline** scores 2, matching the **summary**
  scores 1; each rule counts at most once per field. A category is assigned when it scores
  at least 1, so an article can have several categories.
* The **primary** category is a provider-supplied one where the provider supplies a known
  label (none does today: Alpaca sends no category metadata); otherwise the highest-scoring
  Atlas category; ties go to the earlier category in the taxonomy order (more specific
  categories come first). Nothing matched -> ``OTHER`` with confidence ``NONE``.
* Crypto pairs among the article's provider symbols (e.g. ``BTCUSD``) add a
  ``CRYPTOCURRENCY`` label by symbol rule.
* Confidence describes the evidence, not a probability: ``MEDIUM`` for a headline match
  (or two or more summary rules), ``LOW`` for a single summary match. Rule-based labels are
  ``UNVERIFIED`` keyword estimates; the database refuses any other verification status for
  Atlas labels. Provider labels are ``PROVIDER_ASSERTED``.

Classification says what an article is *about*. It does not rate importance, surprise,
sentiment or market impact, and it predicts nothing. Changing any rule is a new version
(the rules' hash is stored with every event).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.news.enums import (
    MARKET_WIDE_CATEGORIES,
    ClassificationConfidence,
    ClassificationMethod,
    ClassificationSource,
    EventCategory,
    EventScope,
    VerificationStatus,
)
from app.news.text import canonical_json, sha256_hex

TAXONOMY_VERSION = "news-taxonomy-v1"
RULES_VERSION = "keyword-rules-v2"
CLASSIFIER_VERSION = f"{TAXONOMY_VERSION}/{RULES_VERSION}"
HEADLINE_WEIGHT = 2
SUMMARY_WEIGHT = 1

CATEGORY_DESCRIPTIONS: dict[EventCategory, str] = {
    EventCategory.EARNINGS: "Earnings releases, results, EPS, guidance",
    EventCategory.ANALYST_RATINGS: "Analyst ratings, upgrades/downgrades and price targets",
    EventCategory.MERGERS_ACQUISITIONS: "Mergers, acquisitions, takeovers and tender offers",
    EventCategory.CORPORATE_ACTIONS: "Dividends, splits, buybacks, offerings and spin-offs",
    EventCategory.MANAGEMENT_CHANGES: "Executive and board appointments and departures",
    EventCategory.PRODUCT_LAUNCHES: "Product and service launches and introductions",
    EventCategory.REGULATORY: "Regulators, approvals, investigations and sanctions",
    EventCategory.LITIGATION: "Lawsuits, court rulings and settlements",
    EventCategory.CENTRAL_BANKS: "Federal Reserve and other central-bank policy",
    EventCategory.INFLATION: "Inflation and price-index releases",
    EventCategory.EMPLOYMENT: "Labour-market releases and conditions",
    EventCategory.MACROECONOMIC: "Growth, activity, fiscal and other macroeconomic news",
    EventCategory.GEOPOLITICAL: "Conflict, elections, sanctions and international relations",
    EventCategory.CRYPTOCURRENCY: "Cryptocurrencies and digital-asset markets",
    EventCategory.INDUSTRY: "Sector and industry-wide developments",
    EventCategory.COMPANY_ANNOUNCEMENTS: "Other company announcements",
    EventCategory.OTHER: "Nothing matched: unclassified",
}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: str
    case_sensitive: bool = False
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        object.__setattr__(self, "regex", re.compile(self.pattern, flags))


_EXEC = (
    r"(?:CEO|CFO|COO|CTO|chief executive|chief financial officer|chief operating officer|"
    r"chairman|chairwoman|chair|president|board|directors?|executives?)"
)
_EXEC_ACTION = (
    r"(?:appoint(?:s|ed)?|names?|named|resign(?:s|ed)?|steps? down|stepping down|retire(?:s|d)?|"
    r"retirement|successor|succeeds?|hires?|hired|depart(?:s|ure)?|ousted|fired)"
)

RULES: dict[EventCategory, tuple[Rule, ...]] = {
    EventCategory.EARNINGS: (
        Rule("earnings", r"\bearnings\b"),
        Rule("eps", r"\bEPS\b", case_sensitive=True),
        Rule(
            "results",
            r"\b(?:reports?|posts?|announces?|delivers?)\b.{0,30}\b(?:Q[1-4]|quarter(?:ly)?|"
            r"full[- ]year|annual|FY\s?\d{2,4})\b.{0,30}\b(?:results|revenue|sales|earnings|"
            r"profit|loss)\b",
        ),
        Rule("period_figures", r"\b(?:Q[1-4]|FY\s?\d{2,4})\b.{0,20}\b(?:revenue|sales|EPS)\b"),
        Rule(
            "vs_estimates",
            r"\b(?:beats?|miss(?:es)?|tops?)\b.{0,20}\b(?:estimates?|expectations|consensus)\b",
        ),
        Rule("guidance", r"\bguidance\b"),
        Rule(
            "financial_results",
            r"\b(?:financial|quarterly|annual|fiscal(?:[- ]year)?(?: \d{4})?) results\b",
        ),
    ),
    EventCategory.ANALYST_RATINGS: (
        Rule("upgrade_downgrade", r"\b(?:upgrade[sd]?|downgrade[sd]?)\b"),
        Rule("price_target", r"\bprice targets?\b|\bPT\b"),
        Rule(
            "rating_action",
            r"\b(?:initiates?|initiated|reiterates?|reiterated|maintains?|maintained)\b.{0,40}"
            r"\b(?:rating|coverage)\b",
        ),
        Rule(
            "rating_terms",
            r"\b(?:overweight|underweight|outperform|underperform|equal[- ]weight|"
            r"(?:buy|sell|hold|neutral) rating)\b",
        ),
        Rule("analyst", r"\banalysts?\b"),
    ),
    EventCategory.MERGERS_ACQUISITIONS: (
        Rule(
            "acquisition",
            r"\b(?:acquires?|acquired|acquisition|to acquire|takeover|buyout|tender offer)\b",
        ),
        Rule("merger", r"\b(?:merger|merge|merging|merges)\b"),
        Rule(
            "deal_to_buy", r"\bdeal to (?:buy|acquire)\b|\bagrees? to (?:buy|acquire|be acquired)\b"
        ),
        Rule("divestiture", r"\bagrees? to sell\b|\bdivest(?:s|ed|iture|ment)?\b"),
    ),
    EventCategory.CORPORATE_ACTIONS: (
        Rule("dividend", r"\bdividends?\b"),
        Rule("split", r"\b(?:stock|reverse|share) split\b"),
        Rule("buyback", r"\b(?:buyback|share repurchase|repurchase program|repurchases?)\b"),
        Rule("spin_off", r"\bspin[- ]?offs?\b"),
        Rule("offering", r"\b(?:public|secondary|follow-on|registered direct|stock) offering\b"),
        Rule("ipo", r"\bIPO\b", case_sensitive=True),
        Rule("share_issue", r"\bissues? (?:new )?shares\b|\bprivate placement\b"),
    ),
    EventCategory.MANAGEMENT_CHANGES: (
        Rule("exec_then_action", rf"\b{_EXEC}\b.{{0,40}}\b{_EXEC_ACTION}\b"),
        Rule("action_then_exec", rf"\b{_EXEC_ACTION}\b.{{0,40}}\b{_EXEC}\b"),
    ),
    EventCategory.PRODUCT_LAUNCHES: (
        Rule(
            "launch",
            r"\b(?:launch(?:es|ed)?|unveil(?:s|ed)?|introduc(?:es|ed)|rolls? out|rolled out|"
            r"debuts?|debuted)\b",
        ),
        Rule("new_product", r"\bnew (?:product|model|service|platform|device|drug|chip)s?\b"),
    ),
    EventCategory.REGULATORY: (
        Rule(
            "agency",
            r"\b(?:SEC|FDA|FTC|DOJ|FCC|EPA|CFTC|FINRA|CMA|EMA|NHTSA|FAA)\b",
            case_sensitive=True,
        ),
        Rule("regulator", r"\b(?:regulators?|regulatory|antitrust|European Commission)\b"),
        Rule("approval", r"\b(?:approv(?:al|es|ed)|clearance|cleared by|authori[sz]ation)\b"),
        Rule(
            "enforcement",
            r"\b(?:probe|investigation|subpoena|fined|fines|penalt(?:y|ies)|sanctioned)\b",
        ),
        Rule("trading_halt", r"\btrading halt(?:ed)?\b|\bhalted\b"),
    ),
    EventCategory.LITIGATION: (
        Rule("lawsuit", r"\b(?:lawsuits?|sues|sued|suing|litigation|class action)\b"),
        Rule("court", r"\b(?:court|judge|jury|verdict|ruling|appeals? court|indict(?:ed|ment))\b"),
        Rule("settlement", r"\b(?:settlement|settles?|settled)\b"),
        Rule("patent", r"\bpatent infringement\b"),
    ),
    EventCategory.CENTRAL_BANKS: (
        Rule("fed", r"\b(?:Fed|FOMC|Powell)\b|\bFed's\b", case_sensitive=True),
        Rule("federal_reserve", r"\bFederal Reserve\b|\bcentral banks?\b|\bmonetary policy\b"),
        Rule(
            "rates",
            r"\b(?:interest[- ]rates?|rate (?:cut|hike|increase|decrease)s?|basis points)\b",
        ),
        Rule(
            "other_central_banks",
            r"\b(?:ECB|BoE|BoJ|PBOC|SNB|RBA|RBI)\b|\bBank of (?:England|Japan|Canada)\b|"
            r"\bPeople's Bank of China\b",
        ),
        Rule("balance_sheet", r"\bquantitative (?:easing|tightening)\b"),
    ),
    EventCategory.INFLATION: (
        Rule("inflation", r"\b(?:inflation|deflation|disinflation)\b"),
        Rule("indices", r"\b(?:CPI|PCE|PPI)\b", case_sensitive=True),
        Rule("index_names", r"\b(?:consumer|producer) price index\b|\bcore prices\b"),
    ),
    EventCategory.EMPLOYMENT: (
        Rule("payrolls", r"\b(?:non-?farm payrolls?|payrolls|jobs report|employment report)\b"),
        Rule(
            "unemployment", r"\b(?:unemployment|jobless claims|initial claims|continuing claims)\b"
        ),
        Rule("labour_market", r"\b(?:labou?r market|job openings|JOLTS|wage growth)\b"),
    ),
    EventCategory.MACROECONOMIC: (
        Rule("growth", r"\b(?:GDP|gross domestic product|recession|economic growth|the economy)\b"),
        Rule(
            "activity",
            r"\b(?:retail sales|ISM|PMI|purchasing managers|consumer confidence|consumer "
            r"sentiment|housing starts|durable goods|industrial production|"
            r"trade (?:deficit|balance))\b",
        ),
        Rule("yields", r"\b(?:treasury|bond|10-year|2-year) yields?\b"),
        Rule(
            "fiscal",
            r"\b(?:budget deficit|debt ceiling|government shutdown|fiscal policy|tariffs?)\b",
        ),
    ),
    EventCategory.GEOPOLITICAL: (
        Rule("conflict", r"\b(?:war|military|missiles?|invasion|ceasefire|airstrikes?|troops)\b"),
        Rule("relations", r"\b(?:geopolitical|NATO|embargo|sanctions|diplomatic|trade war)\b"),
        Rule("politics", r"\b(?:elections?|coup|terror(?:ist|ism)?)\b"),
    ),
    EventCategory.CRYPTOCURRENCY: (
        Rule(
            "crypto",
            r"\b(?:bitcoin|ethereum|crypto(?:currency|currencies)?|blockchain|stablecoins?|"
            r"DeFi|altcoins?|dogecoin|solana)\b",
        ),
        Rule("tickers", r"\b(?:BTC|ETH|XRP)\b", case_sensitive=True),
    ),
    EventCategory.INDUSTRY: (
        Rule("sector", r"\b(?:industry|sector|sectors|industry-wide)\b"),
        Rule(
            "industries",
            r"\b(?:semiconductors?|chipmakers?|automakers?|airlines|biotech|pharmaceuticals?|"
            r"oil prices|crude oil|OPEC|natural gas|supply chains?|shortages?)\b",
        ),
    ),
    EventCategory.COMPANY_ANNOUNCEMENTS: (
        Rule("announces", r"\b(?:announces?|announced|press release)\b"),
        Rule(
            "business",
            r"\b(?:partnership|partners? (?:with|to)|collaboration|contract|awarded|expansion|"
            r"expands|layoffs?|job cuts|staff cuts|severance|restructuring|investor day)\b",
        ),
    ),
}

# Crypto pairs as providers tag them (BTCUSD, BTC/USD, ETH-USD).
_CRYPTO_BASES = (
    "BTC|ETH|SOL|DOGE|XRP|LTC|BCH|AVAX|ADA|DOT|LINK|UNI|SHIB|AAVE|MATIC|POL|USDT|USDC|XLM|"
    "TRX|PEPE|SUSHI|CRV|GRT|MKR|YFI|BAT|XTZ"
)
_CRYPTO_PAIR = re.compile(rf"^(?:{_CRYPTO_BASES})[/-]?(?:USD|USDT|USDC|BTC|EUR)$")

# Provider category labels Atlas knows how to map (lower-case label -> category).
PROVIDER_LABELS: dict[str, EventCategory] = {
    "earnings": EventCategory.EARNINGS,
    "analyst ratings": EventCategory.ANALYST_RATINGS,
    "price target": EventCategory.ANALYST_RATINGS,
    "m&a": EventCategory.MERGERS_ACQUISITIONS,
    "mergers and acquisitions": EventCategory.MERGERS_ACQUISITIONS,
    "dividends": EventCategory.CORPORATE_ACTIONS,
    "management": EventCategory.MANAGEMENT_CHANGES,
    "fda": EventCategory.REGULATORY,
    "legal": EventCategory.LITIGATION,
    "economics": EventCategory.MACROECONOMIC,
    "federal reserve": EventCategory.CENTRAL_BANKS,
    "cryptocurrency": EventCategory.CRYPTOCURRENCY,
}


def rules_fingerprint() -> str:
    """Hash of the exact rule set; stored with every event."""
    return sha256_hex(
        canonical_json(
            {
                "taxonomy": TAXONOMY_VERSION,
                "rules": {
                    c.value: [[r.name, r.pattern, r.case_sensitive] for r in rules]
                    for c, rules in RULES.items()
                },
                "crypto_pair": _CRYPTO_PAIR.pattern,
                "provider_labels": {k: v.value for k, v in sorted(PROVIDER_LABELS.items())},
                "weights": [HEADLINE_WEIGHT, SUMMARY_WEIGHT],
            }
        )
    )


RULES_SHA256 = rules_fingerprint()


@dataclass(frozen=True)
class CategoryAssignment:
    category: EventCategory
    source: ClassificationSource
    method: ClassificationMethod
    confidence: ClassificationConfidence
    verification: VerificationStatus
    score: int
    evidence: dict[str, Any]
    provider_label: str | None = None
    is_primary: bool = False


@dataclass(frozen=True)
class Classification:
    primary: EventCategory
    assignments: tuple[CategoryAssignment, ...]
    taxonomy_version: str = TAXONOMY_VERSION
    classifier_version: str = CLASSIFIER_VERSION
    rules_sha256: str = RULES_SHA256

    def scope(self, mapped_assets: int) -> EventScope:
        if mapped_assets > 0:
            return EventScope.ASSET
        if self.primary in MARKET_WIDE_CATEGORIES:
            return EventScope.MARKET
        return EventScope.UNLINKED


def _matches(rule: Rule, text: str | None) -> str | None:
    if not text:
        return None
    found = rule.regex.search(text)
    return found.group(0)[:80] if found else None


def is_crypto_pair(symbol: str) -> bool:
    return bool(_CRYPTO_PAIR.match(symbol))


def classify(
    *,
    headline: str,
    summary: str | None,
    symbols: Sequence[str] = (),
    provider_categories: Sequence[str] = (),
) -> Classification:
    """Classify one article revision. Pure and deterministic."""
    atlas: list[CategoryAssignment] = []
    for category, rules in RULES.items():
        score = 0
        matches: list[dict[str, str]] = []
        headline_hit = False
        summary_rules = 0
        for rule in rules:
            if (text := _matches(rule, headline)) is not None:
                score += HEADLINE_WEIGHT
                headline_hit = True
                matches.append({"field": "headline", "rule": rule.name, "text": text})
            if (text := _matches(rule, summary)) is not None:
                score += SUMMARY_WEIGHT
                summary_rules += 1
                matches.append({"field": "summary", "rule": rule.name, "text": text})
        crypto_symbols: list[str] = []
        if category is EventCategory.CRYPTOCURRENCY:
            crypto_symbols = [s for s in symbols if is_crypto_pair(s)]
        if score == 0 and not crypto_symbols:
            continue
        method = ClassificationMethod.KEYWORD_RULES if score else ClassificationMethod.SYMBOL_RULES
        if crypto_symbols:
            score += HEADLINE_WEIGHT
            matches.append(
                {"field": "symbols", "rule": "crypto_pair", "text": ",".join(crypto_symbols)}
            )
        confidence = (
            ClassificationConfidence.MEDIUM
            if headline_hit or summary_rules >= 2 or crypto_symbols
            else ClassificationConfidence.LOW
        )
        atlas.append(
            CategoryAssignment(
                category=category,
                source=ClassificationSource.ATLAS,
                method=method,
                confidence=confidence,
                verification=VerificationStatus.UNVERIFIED,
                score=score,
                evidence={"matches": matches, "rules_version": RULES_VERSION},
            )
        )

    provider = _provider_assignments(provider_categories)
    order = list(EventCategory)
    best_atlas = sorted(atlas, key=lambda a: (-a.score, order.index(a.category)))
    known_provider = [p for p in provider if p.category is not EventCategory.OTHER]
    if known_provider:
        primary = known_provider[0].category
        primary_source = ClassificationSource.PROVIDER
    elif best_atlas:
        primary = best_atlas[0].category
        primary_source = ClassificationSource.ATLAS
    else:
        primary = EventCategory.OTHER
        primary_source = ClassificationSource.ATLAS
        best_atlas = [
            CategoryAssignment(
                category=EventCategory.OTHER,
                source=ClassificationSource.ATLAS,
                method=ClassificationMethod.DEFAULT,
                confidence=ClassificationConfidence.NONE,
                verification=VerificationStatus.UNVERIFIED,
                score=0,
                evidence={"matches": [], "rules_version": RULES_VERSION, "note": "no rule matched"},
            )
        ]

    assignments = tuple(
        CategoryAssignment(
            **{
                **a.__dict__,
                "is_primary": a.category is primary and a.source is primary_source,
            }
        )
        for a in [*provider, *best_atlas]
    )
    return Classification(primary=primary, assignments=assignments)


def _provider_assignments(labels: Sequence[str]) -> list[CategoryAssignment]:
    grouped: dict[EventCategory, list[str]] = {}
    for label in labels:
        cleaned = " ".join(label.split())[:128]
        if not cleaned:
            continue
        category = PROVIDER_LABELS.get(cleaned.lower(), EventCategory.OTHER)
        grouped.setdefault(category, []).append(cleaned)
    return [
        CategoryAssignment(
            category=category,
            source=ClassificationSource.PROVIDER,
            method=ClassificationMethod.PROVIDER_METADATA,
            confidence=ClassificationConfidence.MEDIUM,
            verification=VerificationStatus.PROVIDER_ASSERTED,
            score=0,
            evidence={"provider_labels": names},
            provider_label=names[0],
        )
        for category, names in grouped.items()
    ]


def taxonomy() -> list[dict[str, Any]]:
    """The taxonomy with its rules, for the API (explainability)."""
    return [
        {
            "category": category.value,
            "description": CATEGORY_DESCRIPTIONS[category],
            "market_wide": category in MARKET_WIDE_CATEGORIES,
            "rules": [
                {"name": r.name, "pattern": r.pattern, "case_sensitive": r.case_sensitive}
                for r in RULES.get(category, ())
            ],
        }
        for category in EventCategory
    ]


__all__ = [
    "CLASSIFIER_VERSION",
    "RULES",
    "RULES_SHA256",
    "RULES_VERSION",
    "TAXONOMY_VERSION",
    "CategoryAssignment",
    "Classification",
    "classify",
    "is_crypto_pair",
    "taxonomy",
]
