"""Qualification rules (Phase 3A): versioned configuration and a pure evaluator.

A rule set is immutable configuration identified by ``code`` and ``version``. It is
specific to an asset-class group **and to the feed whose volumes it measures**: on a
single-venue feed (IEX) volumes are that venue's share of trading only, so thresholds
calibrated for consolidated (SIP) volume would be wrong there. Nothing here assumes all
markets behave alike; other markets get their own rule sets.

Mandatory rules decide qualification. A mandatory rule whose input does not exist is
``UNAVAILABLE`` (outcome ``INSUFFICIENT_DATA``), never a failure. Optional rules (spread,
quote quality, volatility cap) are applied only when their data exists: a missing quote
history never rejects an otherwise eligible instrument.

The defaults are starting points chosen for this deliverable, **not empirically validated
thresholds**; they are documented in the Phase 3A report and changed only by creating a
new, audited version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.asset_discovery.enums import (
    InstrumentType,
    MetricStatus,
    ProviderAssetStatus,
    QualificationOutcome,
    RuleStatus,
)
from app.asset_discovery.metrics import AssetMeasurements, Measurement, MetricWindows
from app.data_engine.reference import canonical_json, sha256_text
from app.database.models import (
    ActorType,
    AssetClass,
    QualificationRuleSetRecord,
    ScoringVersionRecord,
)
from app.database.repositories import AuditLogRepository


class RuleSetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The feed whose volumes and prices the thresholds are calibrated for.
    feed: str
    asset_classes: tuple[AssetClass, ...]
    instrument_types: tuple[InstrumentType, ...]
    exchanges: tuple[str, ...]  # MICs
    require_tradable: bool = True
    history_sessions: int = Field(default=60, ge=20, le=500)
    liquidity_sessions: int = Field(default=20, ge=5, le=250)
    min_history_coverage: Decimal = Field(default=Decimal("0.90"), ge=0, le=1)
    max_missing_session_ratio: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    max_invalid_bar_ratio: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    max_sessions_since_last_bar: int = Field(default=2, ge=0, le=60)
    min_price: Decimal = Field(default=Decimal("5"), ge=0)
    min_avg_daily_volume: Decimal = Field(ge=0)
    min_median_dollar_volume: Decimal = Field(ge=0)
    max_zero_volume_ratio: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    # Optional rules: None disables.
    max_median_spread_bps: Decimal | None = Field(default=Decimal("50"), ge=0)
    min_quotes_for_spread: int = Field(default=500, ge=1)
    spread_sessions: int = Field(default=5, ge=1, le=20)
    max_invalid_quote_ratio: Decimal | None = Field(default=Decimal("0.01"), ge=0, le=1)
    max_annualized_volatility: Decimal | None = Field(default=None, ge=0)

    def windows(self) -> MetricWindows:
        return MetricWindows(history=self.history_sessions, liquidity=self.liquidity_sessions)


_US_EXCHANGES = ("XNYS", "XNAS", "ARCX", "XASE", "BATS")
_COMMON = {
    "asset_classes": (AssetClass.EQUITY, AssetClass.ETF),
    "instrument_types": (InstrumentType.STOCK, InstrumentType.ETF, InstrumentType.UNCLASSIFIED),
    "exchanges": _US_EXCHANGES,
}
DEFAULT_RULE_SETS: dict[str, tuple[str, RuleSetConfig]] = {
    # IEX volume is IEX's own share of trading (single venue).
    "us-equity-iex": (
        "US exchange-listed equities and ETFs, liquidity measured on IEX volume "
        "(single venue). Initial defaults, not empirically validated.",
        RuleSetConfig(
            feed="iex",
            min_avg_daily_volume=Decimal("20000"),
            min_median_dollar_volume=Decimal("1000000"),
            **_COMMON,
        ),
    ),
    "us-equity-sip": (
        "US exchange-listed equities and ETFs, liquidity measured on consolidated (SIP) "
        "volume. Initial defaults, not empirically validated.",
        RuleSetConfig(
            feed="sip",
            min_avg_daily_volume=Decimal("500000"),
            min_median_dollar_volume=Decimal("20000000"),
            **_COMMON,
        ),
    ),
}


SCORING_FACTORS = (
    "liquidity",
    "activity",
    "volatility_band",
    "momentum_magnitude",
    "relative_strength_magnitude",
    "spread_quality",
    "data_reliability",
)


class ScoringConfig(BaseModel):
    """Active Watchlist ranking. Ranks analytical priority only: never a trade signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weights: dict[str, Decimal]
    # Realized volatility band that gets full marks; scores fall off linearly outside it.
    volatility_band: tuple[Decimal, Decimal] = (Decimal("0.15"), Decimal("0.60"))
    volatility_zero_at: tuple[Decimal, Decimal] = (Decimal("0"), Decimal("1.50"))
    # At least this share of the total weight must be computable for an asset to be ranked.
    min_weight_coverage: Decimal = Field(default=Decimal("0.60"), gt=0, le=1)
    watchlist_size: int = Field(default=25, ge=1, le=500)
    # Current members stay while ranked within size + buffer (limits churn).
    retention_buffer: int = Field(default=5, ge=0, le=100)
    min_score: Decimal = Field(default=Decimal("0"), ge=0, le=100)

    @field_validator("weights")
    @classmethod
    def _known_factors(cls, value: dict[str, Decimal]) -> dict[str, Decimal]:
        unknown = sorted(set(value) - set(SCORING_FACTORS))
        if unknown:
            raise ValueError(f"unknown scoring factor(s): {', '.join(unknown)}")
        if any(w < 0 for w in value.values()) or sum(value.values(), Decimal(0)) <= 0:
            raise ValueError("weights must be non-negative and sum to more than zero")
        return value


DEFAULT_SCORING: dict[str, tuple[str, ScoringConfig]] = {
    "watchlist-default": (
        "Balanced attention ranking across liquidity, activity, a volatility band, the "
        "magnitude of recent moves (either direction), spread quality and data "
        "reliability. Initial weights, not empirically validated.",
        ScoringConfig(
            weights={
                "liquidity": Decimal("0.25"),
                "activity": Decimal("0.15"),
                "volatility_band": Decimal("0.15"),
                "momentum_magnitude": Decimal("0.10"),
                "relative_strength_magnitude": Decimal("0.10"),
                "spread_quality": Decimal("0.10"),
                "data_reliability": Decimal("0.15"),
            }
        ),
    )
}


# ------------------------------------------------------------------ versioned storage
Model = type[QualificationRuleSetRecord] | type[ScoringVersionRecord]


def _config_json(config: BaseModel) -> dict[str, Any]:
    return config.model_dump(mode="json")


def latest_version(
    session: Session, model: Model, code: str
) -> QualificationRuleSetRecord | ScoringVersionRecord | None:
    found = session.scalars(
        select(model).where(model.code == code).order_by(model.version.desc()).limit(1)
    ).one_or_none()
    return cast(QualificationRuleSetRecord | ScoringVersionRecord | None, found)


def create_version(
    session: Session,
    model: Model,
    *,
    code: str,
    config: BaseModel,
    description: str,
    created_by: str,
    actor_type: ActorType,
    reason: str,
) -> QualificationRuleSetRecord | ScoringVersionRecord:
    """Store ``config`` as the next version of ``code`` (or return the identical one).

    Safe under concurrency: if another transaction creates the same content first, its
    version is returned; a different concurrent version raises ``IntegrityError``.
    """
    data = _config_json(config)
    digest = sha256_text(canonical_json(data))

    def identical() -> QualificationRuleSetRecord | ScoringVersionRecord | None:
        found = session.scalars(
            select(model).where(model.code == code, model.content_sha256 == digest)
        ).one_or_none()
        return cast(QualificationRuleSetRecord | ScoringVersionRecord | None, found)

    existing = identical()
    if existing is not None:
        return existing
    latest = latest_version(session, model, code)
    record = model(
        id=uuid.uuid4(),
        code=code,
        version=1 if latest is None else latest.version + 1,
        description=description,
        config=data,
        content_sha256=digest,
        created_by=created_by,
    )
    try:
        with session.begin_nested():
            session.add(record)
            session.flush()
    except IntegrityError:
        existing = identical()
        if existing is None:
            raise
        return existing
    AuditLogRepository(session).append(
        actor_type=actor_type,
        actor_id=created_by,
        action=f"{model.__tablename__}.version_created",
        entity_type=model.__tablename__,
        entity_id=str(record.id),
        reason=reason,
        previous_state=None
        if latest is None
        else {"version": latest.version, "config": latest.config},
        new_state={"code": code, "version": record.version, "config": data},
    )
    return record


def resolve_rule_set(
    session: Session, code: str
) -> tuple[QualificationRuleSetRecord, RuleSetConfig]:
    record = latest_version(session, QualificationRuleSetRecord, code)
    if record is None:
        if code not in DEFAULT_RULE_SETS:
            raise LookupError(f"qualification rule set '{code}' does not exist")
        description, config = DEFAULT_RULE_SETS[code]
        record = create_version(
            session,
            QualificationRuleSetRecord,
            code=code,
            config=config,
            description=description,
            created_by="asset_discovery",
            actor_type=ActorType.SYSTEM,
            reason="initial default rule set",
        )
    assert isinstance(record, QualificationRuleSetRecord)  # noqa: S101  # model selects type
    return record, RuleSetConfig.model_validate(record.config)


def resolve_scoring(session: Session, code: str) -> tuple[ScoringVersionRecord, ScoringConfig]:
    record = latest_version(session, ScoringVersionRecord, code)
    if record is None:
        if code not in DEFAULT_SCORING:
            raise LookupError(f"scoring version '{code}' does not exist")
        description, config = DEFAULT_SCORING[code]
        record = create_version(
            session,
            ScoringVersionRecord,
            code=code,
            config=config,
            description=description,
            created_by="asset_discovery",
            actor_type=ActorType.SYSTEM,
            reason="initial default scoring version",
        )
    assert isinstance(record, ScoringVersionRecord)  # noqa: S101  # model selects type
    return record, ScoringConfig.model_validate(record.config)


# ------------------------------------------------------------------ evaluation
@dataclass(frozen=True)
class InstrumentFacts:
    """What Atlas knew about an instrument at the evaluation's as-of time."""

    asset_id: uuid.UUID
    symbol: str
    asset_class: AssetClass
    instrument_type: InstrumentType
    exchange_mic: str | None
    calendar_code: str | None
    status: ProviderAssetStatus | None  # None: no reference version known at as_of
    tradable: bool | None
    mapped: bool
    mapping_valid_from: str | None
    provider_supports_class: bool
    feed_has_history: bool


@dataclass(frozen=True)
class RuleResult:
    rule: str
    category: Literal["instrument", "data_reliability", "price", "liquidity"]
    mandatory: bool
    status: RuleStatus
    message: str
    value: Any = None
    threshold: Any = None

    def as_dict(self) -> dict[str, Any]:
        def plain(v: Any) -> Any:
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, float):
                return round(v, 6)
            if isinstance(v, tuple | list):
                return [plain(x) for x in v]
            return v

        return {
            "rule": self.rule,
            "category": self.category,
            "mandatory": self.mandatory,
            "status": self.status.value,
            "message": self.message,
            "value": plain(self.value),
            "threshold": plain(self.threshold),
        }


@dataclass(frozen=True)
class Evaluation:
    outcome: QualificationOutcome
    results: tuple[RuleResult, ...]

    @property
    def failed(self) -> list[str]:
        return [r.rule for r in self.results if r.mandatory and r.status is RuleStatus.FAIL]

    @property
    def unavailable(self) -> list[str]:
        return [r.rule for r in self.results if r.mandatory and r.status is RuleStatus.UNAVAILABLE]

    @property
    def instrument_eligible(self) -> bool:
        return all(r.status is RuleStatus.PASS for r in self.results if r.category == "instrument")


def _check(
    rule: str,
    category: Literal["instrument", "data_reliability", "price", "liquidity"],
    ok: bool,
    pass_message: str,
    fail_message: str,
    *,
    mandatory: bool = True,
    value: Any = None,
    threshold: Any = None,
) -> RuleResult:
    return RuleResult(
        rule,
        category,
        mandatory,
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        pass_message if ok else fail_message,
        value,
        threshold,
    )


def instrument_rules(facts: InstrumentFacts, config: RuleSetConfig) -> list[RuleResult]:
    """Metadata-only eligibility. Cheap: evaluated for the whole discovery pool."""
    results = [
        _check(
            "supported_asset_class",
            "instrument",
            facts.asset_class in config.asset_classes,
            f"{facts.asset_class.value} is supported",
            f"{facts.asset_class.value} is not a supported asset class",
            value=facts.asset_class.value,
            threshold=[c.value for c in config.asset_classes],
        ),
        _check(
            "supported_instrument_type",
            "instrument",
            facts.instrument_type in config.instrument_types,
            f"instrument type {facts.instrument_type.value} is supported"
            + (
                " (the provider does not classify ETFs or share types)"
                if facts.instrument_type is InstrumentType.UNCLASSIFIED
                else ""
            ),
            f"instrument type {facts.instrument_type.value} is not supported",
            value=facts.instrument_type.value,
        ),
        _check(
            "supported_exchange",
            "instrument",
            facts.exchange_mic in config.exchanges,
            f"listed on {facts.exchange_mic}",
            "listing exchange "
            + (facts.exchange_mic or "unknown (provider code without a verified MIC)")
            + " is not supported",
            value=facts.exchange_mic,
            threshold=list(config.exchanges),
        ),
        _check(
            "exchange_calendar",
            "instrument",
            facts.calendar_code is not None,
            f"session calendar {facts.calendar_code} available",
            "no verified session calendar for the listing venue",
            value=facts.calendar_code,
        ),
        _check(
            "active_status",
            "instrument",
            facts.status is ProviderAssetStatus.ACTIVE,
            "active at the provider",
            "not active at the provider"
            if facts.status
            else "no reference data known at this time",
            value=None if facts.status is None else facts.status.value,
        ),
        _check(
            "tradable",
            "instrument",
            facts.tradable is True or not config.require_tradable,
            "tradable according to provider reference data",
            "the provider does not mark it tradable"
            if facts.tradable is False
            else "tradability not reported by the provider",
            value=facts.tradable,
        ),
        _check(
            "provider_mapping",
            "instrument",
            facts.mapped,
            f"current provider symbol mapping (valid from {facts.mapping_valid_from})",
            "no valid provider symbol mapping at this time",
        ),
        _check(
            "market_data_capability",
            "instrument",
            facts.provider_supports_class and facts.feed_has_history,
            f"historical bars available on feed {config.feed}",
            "the configured provider/feed cannot serve this instrument's market data",
        ),
    ]
    return results


def _metric(measured: AssetMeasurements | None, name: str) -> Measurement | None:
    return None if measured is None else measured.values.get(name)


def _threshold_rule(
    rule: str,
    category: Literal["instrument", "data_reliability", "price", "liquidity"],
    measurement: Measurement | None,
    threshold: Decimal | int | None,
    *,
    at_least: bool,
    describe: str,
    unit: str = "",
    mandatory: bool = True,
    unavailable_message: str | None = None,
) -> RuleResult:
    if threshold is None:
        value = None if measurement is None else measurement.value
        return RuleResult(
            rule,
            category,
            mandatory,
            RuleStatus.NOT_APPLICABLE,
            f"{describe}: no limit configured",
            value,
            None,
        )
    if (
        measurement is None
        or measurement.status is not MetricStatus.OK
        or measurement.value is None
    ):
        detail = unavailable_message or (
            f"{describe} not computable"
            + (
                ""
                if measurement is None
                else f" ({measurement.observations} of "
                f"{measurement.expected} required observations)"
            )
        )
        return RuleResult(
            rule, category, mandatory, RuleStatus.UNAVAILABLE, detail, None, threshold
        )
    value = measurement.value
    ok = value >= float(threshold) if at_least else value <= float(threshold)
    sign = ">=" if at_least else "<="
    return RuleResult(
        rule,
        category,
        mandatory,
        RuleStatus.PASS if ok else RuleStatus.FAIL,
        f"{describe} {value:,.4g}{unit} ({'meets' if ok else 'fails'} {sign} {threshold}{unit})",
        value,
        threshold,
    )


def data_rules(
    measured: AssetMeasurements | None,
    spread: list[Measurement],
    config: RuleSetConfig,
) -> list[RuleResult]:
    """Rules on stored observations. UNAVAILABLE (never FAIL) when no history exists."""
    counts = measured.status_counts() if measured else {}
    no_history = not measured or not (
        counts.get("TRADED", 0)
        + counts.get("ZERO_VOLUME", 0)
        + counts.get("VOLUME_MISSING", 0)
        + counts.get("NO_REPORTED_ACTIVITY", 0)
    )
    history_message = "no daily history stored for the window yet" if no_history else None
    h = config.history_sessions

    def m(name: str) -> Measurement | None:
        return None if no_history else _metric(measured, name)

    results = [
        _threshold_rule(
            "history_coverage",
            "data_reliability",
            m("history_coverage"),
            config.min_history_coverage,
            at_least=True,
            describe=f"valid daily bars in the last {h} sessions (share)",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "missing_sessions",
            "data_reliability",
            m("missing_session_ratio"),
            config.max_missing_session_ratio,
            at_least=False,
            describe="sessions with neither a bar nor evidence of no activity (share)",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "invalid_observations",
            "data_reliability",
            m("invalid_bar_ratio"),
            config.max_invalid_bar_ratio,
            at_least=False,
            describe="quarantined bars (share)",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "stale_data",
            "data_reliability",
            m("sessions_since_last_bar"),
            config.max_sessions_since_last_bar,
            at_least=False,
            describe="completed sessions since the last valid bar",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "min_price",
            "price",
            m("last_close"),
            config.min_price,
            at_least=True,
            describe="last valid close",
            unit=" USD",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "max_volatility",
            "price",
            m("realized_volatility"),
            config.max_annualized_volatility,
            at_least=False,
            describe="annualized realized volatility",
            mandatory=False,
        ),
        _threshold_rule(
            "min_avg_daily_volume",
            "liquidity",
            m("avg_daily_volume"),
            config.min_avg_daily_volume,
            at_least=True,
            describe=f"average daily {config.feed.upper()} volume",
            unit=" shares",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "min_median_dollar_volume",
            "liquidity",
            m("median_dollar_volume"),
            config.min_median_dollar_volume,
            at_least=True,
            describe=f"median daily {config.feed.upper()} dollar volume",
            unit=" USD",
            unavailable_message=history_message,
        ),
        _threshold_rule(
            "volume_consistency",
            "liquidity",
            m("zero_volume_ratio"),
            config.max_zero_volume_ratio,
            at_least=False,
            describe="sessions without volume (share)",
            unavailable_message=history_message,
        ),
    ]
    by_name = {s.metric: s for s in spread}
    quotes = by_name.get("median_spread_bps")
    enough = quotes is not None and quotes.observations >= config.min_quotes_for_spread
    no_quotes = (
        f"fewer than {config.min_quotes_for_spread} stored quotes in the last "
        f"{config.spread_sessions} sessions; optional rule not applied"
    )
    results.append(
        _threshold_rule(
            "max_spread",
            "liquidity",
            by_name.get("median_spread_bps") if enough else None,
            config.max_median_spread_bps,
            at_least=False,
            describe=f"median quoted {config.feed.upper()} spread",
            unit=" bps",
            mandatory=False,
            unavailable_message=no_quotes,
        )
    )
    results.append(
        _threshold_rule(
            "quote_quality",
            "liquidity",
            by_name.get("invalid_quote_ratio") if enough else None,
            config.max_invalid_quote_ratio,
            at_least=False,
            describe="quarantined quotes (share)",
            mandatory=False,
            unavailable_message=no_quotes,
        )
    )
    return results


def evaluate(
    facts: InstrumentFacts,
    measured: AssetMeasurements | None,
    spread: list[Measurement],
    config: RuleSetConfig,
    *,
    data_evaluated: bool = True,
) -> Evaluation:
    results = instrument_rules(facts, config)
    eligible = all(r.status is RuleStatus.PASS for r in results)
    if eligible and data_evaluated:
        results += data_rules(measured, spread, config)
    elif not eligible:
        results.append(
            RuleResult(
                "data_rules",
                "data_reliability",
                False,
                RuleStatus.NOT_APPLICABLE,
                "data rules skipped: the instrument fails eligibility",
                None,
                None,
            )
        )
    mandatory = [r for r in results if r.mandatory]
    if any(r.status is RuleStatus.FAIL for r in mandatory):
        outcome = QualificationOutcome.DISQUALIFIED
    elif any(r.status is RuleStatus.UNAVAILABLE for r in mandatory) or not data_evaluated:
        outcome = QualificationOutcome.INSUFFICIENT_DATA
    else:
        outcome = QualificationOutcome.QUALIFIED
    return Evaluation(outcome, tuple(results))


__all__ = [
    "DEFAULT_RULE_SETS",
    "DEFAULT_SCORING",
    "SCORING_FACTORS",
    "Evaluation",
    "InstrumentFacts",
    "RuleResult",
    "RuleSetConfig",
    "ScoringConfig",
    "create_version",
    "data_rules",
    "evaluate",
    "instrument_rules",
    "latest_version",
    "resolve_rule_set",
    "resolve_scoring",
]
