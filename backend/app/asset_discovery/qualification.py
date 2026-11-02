"""Qualification evaluation (Phase 3A): which discovered instruments are eligible.

One evaluation run, at one ``as_of`` time:

1. **Facts at as_of.** Reference version observed by ``as_of`` (status, tradability,
   exchange), provider mapping valid at ``as_of``, venue calendar. Never today's metadata
   for a past ``as_of``.
2. **Instrument rules** for every candidate (cheap, metadata only).
3. **History preparation** (live runs only, bounded): instruments that pass the metadata
   rules but lack daily history get it through the ordinary 2B ingestion path, at most
   ``ASSET_DISCOVERY_HISTORY_PREP_MAX_ASSETS`` per run. Instruments whose mapping only
   starts at discovery time (``OBSERVED`` basis) are skipped: earlier bars could not be
   attributed to them.
4. **Measurements** (Polars) from bars ingested and available by ``as_of``, completeness
   evidence and gaps recorded by then, and quotes available by then.
5. **Data rules**, then one immutable evaluation per candidate with every rule's result.

Membership is changed by a separate job (``membership_update``) from these evaluations.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery import membership
from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.enums import (
    MetricStatus,
    ProviderAssetStatus,
    UniverseLayer,
    ValidFromBasis,
)
from app.asset_discovery.metrics import (
    FEATURE_VERSION,
    AssetMeasurements,
    Measurement,
    compute,
    spread_measurements,
)
from app.asset_discovery.rules import (
    Evaluation,
    InstrumentFacts,
    RuleSetConfig,
    evaluate,
    resolve_rule_set,
)
from app.asset_discovery.sessions import (
    CALENDARS,
    ExchangeCalendar,
    calendar_for,
    completed_sessions,
)
from app.core.clocks import Clock
from app.core.config import MarketDataSettings
from app.core.logging import get_logger
from app.data_engine.errors import MarketDataError, UnsupportedCapabilityError
from app.data_engine.historical import (
    MAX_SYMBOLS_PER_JOB,
    HistoricalFetchRequest,
    HistoricalIngestionService,
)
from app.data_engine.providers.base import MarketDataProvider
from app.data_engine.timeframes import DAY_1
from app.database.models import (
    Asset,
    AssetIdentifier,
    AssetMetricValueRecord,
    AssetReferenceVersionRecord,
    AssetStatus,
    QualificationEvaluationRecord,
)
from app.database.repositories import universe as reads
from app.database.sessions import session_scope

logger = get_logger(__name__)

WRITE_CHUNK = 1000
# Decimal places used when a float measurement is persisted.
_PLACES = {
    "avg_daily_volume": 2,
    "avg_dollar_volume": 2,
    "median_dollar_volume": 2,
    "last_close": 6,
    "atr": 6,
    "median_spread_bps": 4,
    "sessions_since_last_bar": 0,
}
_DEFAULT_PLACES = 8
_PRIORITY_ATTRIBUTES = ("has_options",)


def to_decimal(metric: str, value: float | None) -> Decimal | None:
    if value is None or not math.isfinite(value):
        return None
    places = _PLACES.get(metric, _DEFAULT_PLACES)
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


@dataclass
class _Candidate:
    asset: Asset
    version: AssetReferenceVersionRecord | None
    mapping: AssetIdentifier | None
    facts: InstrumentFacts


def short_eligibility(version: AssetReferenceVersionRecord | None) -> dict[str, Any]:
    if version is None:
        return {}
    return {
        "shortable": version.shortable,
        "easy_to_borrow": version.easy_to_borrow,
        "borrow_status": version.borrow_status,
        "marginable": version.marginable,
        "margin_requirement_short": None
        if version.margin_requirement_short is None
        else str(version.margin_requirement_short),
        "as_of_reference_version": version.version,
        "reference_observed_at": version.observed_at.isoformat(),
        "note": (
            "Provider reference data at the time shown. Borrow availability changes: it "
            "must be revalidated when a short is actually proposed."
        ),
    }


class QualificationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        market_settings: MarketDataSettings,
        settings: AssetDiscoverySettings,
        clock: Clock,
        ingestion: HistoricalIngestionService | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._market = market_settings
        self._settings = settings
        self._clock = clock
        self._ingestion = ingestion

    def rule_set(self, session: Session) -> tuple[uuid.UUID, RuleSetConfig]:
        code = self._settings.rule_set_code(self._provider.feed)
        record, config = resolve_rule_set(session, code)
        if config.feed != self._provider.feed:
            raise UnsupportedCapabilityError(
                f"rule set '{code}' v{record.version} is calibrated for feed '{config.feed}', "
                f"but the configured feed is '{self._provider.feed}'"
            )
        return record.id, config

    # ------------------------------------------------------------ facts
    def candidates(self, session: Session, as_of: datetime, *, live: bool) -> list[_Candidate]:
        provider = self._provider.provider_id
        versions = reads.reference_versions_at(session, provider, as_of)
        ids = {a for a, v in versions.items() if v.status is ProviderAssetStatus.ACTIVE}
        if live:
            # Current members are always re-evaluated, so a failing one can be removed.
            ids |= set(membership.current_members(session, UniverseLayer.QUALIFIED_UNIVERSE))
        mappings = reads.mappings_at(session, provider, as_of, ids)
        calendars = reads.exchange_calendars(session)
        caps = self._provider.capabilities()
        has_history = self._provider.feed_access().historical
        assets = {a.id: a for a in session.scalars(select(Asset).where(Asset.id.in_(ids)))}
        result = []
        for asset_id in sorted(ids):
            asset = assets[asset_id]
            version = versions.get(asset_id)
            mapping = mappings.get(asset_id)
            mic = version.exchange_mic if version is not None else asset.exchange_code
            calendar = calendars.get(mic) if mic else None
            facts = InstrumentFacts(
                asset_id=asset_id,
                symbol=version.symbol if version is not None else asset.symbol,
                asset_class=asset.asset_class,
                instrument_type=asset.instrument_type,
                exchange_mic=mic,
                calendar_code=calendar if calendar in CALENDARS else None,
                status=None if version is None else version.status,
                tradable=None if version is None else version.tradable,
                mapped=mapping is not None and asset.status is not AssetStatus.DELISTED,
                mapping_valid_from=None if mapping is None else mapping.valid_from.isoformat(),
                provider_supports_class=asset.asset_class in caps.asset_classes,
                feed_has_history=has_history,
            )
            result.append(_Candidate(asset, version, mapping, facts))
        return result

    # ------------------------------------------------------------ history preparation
    async def prepare_history(self, job_id: uuid.UUID, config: RuleSetConfig) -> dict[str, Any]:
        """Retrieve daily history for a bounded number of eligible instruments lacking it."""
        limit = self._settings.history_prep_max_assets
        if self._ingestion is None or limit == 0:
            return {"enabled": False}
        now = self._clock.now()
        calendar = CALENDARS["XNYS"]
        sessions = completed_sessions(calendar, now, config.history_sessions)
        window_start = _midnight(calendar, sessions[0].day)
        window_end = _midnight(calendar, sessions[-1].day)
        required = math.ceil(config.history_sessions * float(config.min_history_coverage))
        with session_scope(self._sessions) as session:
            candidates = self.candidates(session, now, live=True)
            eligible = [
                c
                for c in candidates
                if evaluate(c.facts, None, [], config, data_evaluated=False).instrument_eligible
            ]
            counts = reads.valid_bar_counts(
                session,
                [c.asset.id for c in eligible],
                provider=self._provider.provider_id,
                feed=self._provider.feed,
                adjustment=self._market.historical_data_adjustment,
                start=window_start,
                end=window_end,
                as_of=now,
            )
            lacking = [c for c in eligible if counts.get(c.asset.id, 0) < required]
            attributable = [
                c
                for c in lacking
                if c.mapping is not None
                and (
                    c.mapping.valid_from_basis is not ValidFromBasis.OBSERVED
                    or c.mapping.valid_from <= window_start
                )
            ]
        lacking_ids = {c.asset.id for c in lacking}
        skipped_unattributable = len(lacking_ids) - len(attributable)

        benchmark = self._settings.benchmark_symbol

        def priority(c: _Candidate) -> tuple[int, int, str]:
            # Benchmark first (relative strength needs it), then instruments with listed
            # options, then a stable hash of the asset ID: never alphabetical, which would
            # bias the early universe toward one end of the alphabet.
            attrs = set(c.version.attributes if c.version is not None else [])
            return (
                0 if c.facts.symbol == benchmark else 1,
                0 if attrs & set(_PRIORITY_ATTRIBUTES) else 1,
                hashlib.sha256(c.asset.id.bytes).hexdigest(),
            )

        chosen = sorted(attributable, key=priority)[:limit]
        start = _midnight(
            calendar, (now - timedelta(days=self._settings.history_prep_lookback_days)).date()
        )
        jobs: list[dict[str, Any]] = []
        for offset in range(0, len(chosen), MAX_SYMBOLS_PER_JOB):
            group = chosen[offset : offset + MAX_SYMBOLS_PER_JOB]
            try:
                created = self._ingestion.create_job(
                    HistoricalFetchRequest(
                        asset_ids=tuple(c.asset.id for c in group),
                        timeframe=DAY_1,
                        start=start,
                        end=window_end,
                    ),
                    requested_by=f"asset_discovery:{job_id}",
                )
                finished = await self._ingestion.run_job(created.job_id)
                jobs.append(
                    {
                        "job_id": str(finished.job_id),
                        "status": finished.status.value,
                        "assets": len(group),
                    }
                )
            except MarketDataError as exc:
                jobs.append({"error": exc.code, "message": exc.message, "assets": len(group)})
                if exc.code in ("provider_authentication_failed", "credentials_not_configured"):
                    raise
        logger.info(
            "qualification.history_prepared",
            job_id=str(job_id),
            lacking=len(lacking_ids),
            requested=len(chosen),
            unattributable=skipped_unattributable,
        )
        return {
            "enabled": True,
            "eligible_lacking_history": len(lacking_ids),
            "requested": len(chosen),
            "deferred": max(0, len(attributable) - len(chosen)),
            "skipped_mapping_starts_at_discovery": skipped_unattributable,
            "ingestion_jobs": jobs,
            "priority": "benchmark, then instruments with listed options (a fetch-order "
            "heuristic only), then a stable hash of the asset ID",
        }

    # ------------------------------------------------------------ evaluation
    def evaluate_all(self, job_id: uuid.UUID, as_of: datetime, *, live: bool) -> dict[str, Any]:
        provider, feed = self._provider.provider_id, self._provider.feed
        adjustment = self._market.historical_data_adjustment
        with session_scope(self._sessions) as session:
            rule_set_id, config = self.rule_set(session)
            candidates = self.candidates(session, as_of, live=live)
            stage_a = {
                c.asset.id: evaluate(c.facts, None, [], config, data_evaluated=False)
                for c in candidates
            }
            eligible = [c for c in candidates if stage_a[c.asset.id].instrument_eligible]
            measured: dict[uuid.UUID, AssetMeasurements] = {}
            spreads: dict[uuid.UUID, list[Measurement]] = {}
            windows: dict[str, tuple[datetime, datetime]] = {}
            by_calendar: dict[str, list[_Candidate]] = {}
            for c in eligible:
                assert c.facts.calendar_code is not None  # noqa: S101  # eligibility requires it
                by_calendar.setdefault(c.facts.calendar_code, []).append(c)
            benchmark = self._benchmark(session)
            for code, group in by_calendar.items():
                calendar = calendar_for(code)
                sessions = completed_sessions(calendar, as_of, config.history_sessions)
                ids = [c.asset.id for c in group]
                compute_ids = ids + ([benchmark] if benchmark and benchmark not in ids else [])
                bars = reads.daily_bars(
                    session,
                    compute_ids,
                    provider=provider,
                    feed=feed,
                    adjustment=adjustment,
                    sessions=sessions,
                    calendar=calendar,
                    as_of=as_of,
                )
                covered = reads.covered_sessions(
                    session,
                    compute_ids,
                    provider=provider,
                    feed=feed,
                    adjustment=adjustment,
                    sessions=sessions,
                    calendar=calendar,
                    as_of=as_of,
                )
                gaps = reads.gap_sessions(
                    session,
                    compute_ids,
                    provider=provider,
                    feed=feed,
                    sessions=sessions,
                    calendar=calendar,
                    as_of=as_of,
                )
                result = compute(
                    bars,
                    compute_ids,
                    sessions,
                    covered=covered,
                    gaps=gaps,
                    windows=config.windows(),
                    benchmark=benchmark,
                )
                measured.update({k: v for k, v in result.items() if k in ids})
                recent = sessions[-config.spread_sessions :]
                summaries = reads.spread_summaries(
                    session, ids, provider=provider, feed=feed, sessions=recent, as_of=as_of
                )
                for asset_id in ids:
                    spreads[asset_id] = spread_measurements(
                        asset_id, summaries.get(asset_id), config.spread_sessions
                    )
                windows[code] = (sessions[0].regular_open, sessions[-1].regular_close)

        evaluations: dict[uuid.UUID, Evaluation] = {}
        for c in candidates:
            if c.asset.id in measured or c.asset.id in spreads:
                evaluations[c.asset.id] = evaluate(
                    c.facts, measured.get(c.asset.id), spreads.get(c.asset.id, []), config
                )
            else:
                evaluations[c.asset.id] = evaluate(c.facts, None, [], config)

        metric_rows = self._metric_rows(job_id, as_of, measured, spreads)
        eval_rows = [
            {
                "id": uuid.uuid4(),
                "job_id": job_id,
                "asset_id": c.asset.id,
                "rule_set_id": rule_set_id,
                "as_of": as_of,
                "outcome": evaluations[c.asset.id].outcome,
                "rule_results": [r.as_dict() for r in evaluations[c.asset.id].results],
                "failed_rules": evaluations[c.asset.id].failed,
                "unavailable_rules": evaluations[c.asset.id].unavailable,
                "short_eligibility": short_eligibility(c.version),
                "reference_version_id": None if c.version is None else c.version.id,
            }
            for c in candidates
        ]
        # One transaction: an interrupted job leaves nothing behind, so its retry (which
        # evaluates at a new as-of time) never mixes with a partial earlier attempt.
        with session_scope(self._sessions) as session:
            for rows, model in (
                (metric_rows, AssetMetricValueRecord),
                (eval_rows, QualificationEvaluationRecord),
            ):
                for offset in range(0, len(rows), WRITE_CHUNK):
                    session.execute(insert(model), rows[offset : offset + WRITE_CHUNK])

        outcomes = Counter(e.outcome.value for e in evaluations.values())
        reasons = Counter(rule for e in evaluations.values() for rule in e.failed)
        unavailable = Counter(rule for e in evaluations.values() for rule in e.unavailable)
        return {
            "as_of": as_of.isoformat(),
            "live": live,
            "rule_set_id": str(rule_set_id),
            "feature_version": FEATURE_VERSION,
            "candidates": len(candidates),
            "instrument_eligible": len(eligible),
            "measured": len(measured),
            "outcomes": dict(outcomes),
            "failed_rules": dict(reasons.most_common()),
            "unavailable_rules": dict(unavailable.most_common()),
            "benchmark_asset_id": None if benchmark is None else str(benchmark),
            "windows": {k: [v[0].isoformat(), v[1].isoformat()] for k, v in windows.items()},
        }

    def _benchmark(self, session: Session) -> uuid.UUID | None:
        return session.scalar(
            select(Asset.id)
            .where(
                Asset.symbol == self._settings.benchmark_symbol, Asset.status == AssetStatus.ACTIVE
            )
            .limit(1)
        )

    def _metric_rows(
        self,
        job_id: uuid.UUID,
        as_of: datetime,
        measured: dict[uuid.UUID, AssetMeasurements],
        spreads: dict[uuid.UUID, list[Measurement]],
    ) -> list[dict[str, Any]]:
        rows = []
        # Instruments with no stored observations at all get no metric rows: their
        # evaluation already says the inputs are unavailable.
        observed = {
            a for a, m in measured.items() if any(s.value != "MISSING" for s in m.sessions.values())
        } | {a for a, s in spreads.items() if any(x.observations for x in s)}
        for asset_id in sorted(observed):
            values: Sequence[Measurement] = [
                *(measured[asset_id].values.values() if asset_id in measured else ()),
                *spreads.get(asset_id, ()),
            ]
            if asset_id in measured:
                counts = measured[asset_id].status_counts()
                values = [
                    *values,
                    Measurement(
                        "session_status_counts",
                        len(measured[asset_id].sessions),
                        float(sum(counts.values())),
                        MetricStatus.OK,
                        sum(counts.values()),
                        len(measured[asset_id].sessions),
                        details={"counts": counts},
                    ),
                ]
            for m in values:
                value = to_decimal(m.metric, m.value) if m.status is MetricStatus.OK else None
                status = (
                    m.status
                    if value is not None or m.status is not MetricStatus.OK
                    else MetricStatus.NO_DATA
                )
                rows.append(
                    {
                        "asset_id": asset_id,
                        "metric": m.metric,
                        "window": f"{m.window} sessions",
                        "timeframe": "1Day"
                        if m.metric not in ("median_spread_bps", "invalid_quote_ratio")
                        else "quote",
                        "provider": self._provider.provider_id,
                        "feed": self._provider.feed,
                        "feature_version": FEATURE_VERSION,
                        "as_of": as_of,
                        "source_start": m.source_start,
                        "source_end": None if m.source_end is None else min(m.source_end, as_of),
                        "observations": m.observations,
                        "expected_observations": m.expected,
                        "value": value,
                        "status": status,
                        "details": _plain(m.details),
                        "job_id": job_id,
                    }
                )
        return rows


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, float):
        return round(value, 8) if math.isfinite(value) else None
    if isinstance(value, Decimal | datetime):
        return str(value)
    return value


def _midnight(calendar: ExchangeCalendar, day: Any) -> datetime:
    return datetime.combine(day, time(0), tzinfo=calendar.zone).astimezone(UTC)


__all__ = ["QualificationService", "short_eligibility", "to_decimal"]
