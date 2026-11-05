"""Discovery pool maintenance (Phase 3A): apply provider directories to Atlas's catalog.

Input is the provider-independent ``InstrumentDirectory`` / ``ReferenceInstrument``; no
provider payload structure is known here.

Identity
  An instrument is identified by the provider's permanent ID (``provider_assets``), never by
  its symbol. The first time a provider ID is seen it is linked to an Atlas asset: the asset
  an operator already registered under the same current symbol (adoption), or a new one.
  The link never changes afterwards, so a renamed ticker stays the same asset and a reused
  ticker becomes a different one.

Versions and lifecycle
  A record whose content (hash of the verbatim provider record) differs from the current
  version becomes version N+1 (append-only); the differences become lifecycle events:
  deactivation/delisting, reactivation, symbol, exchange and name changes, tradability
  changes, other metadata changes. Identical records only refresh ``last_seen_at``.

Mappings
  Active instruments hold the current provider symbol mapping. Deactivation retires it,
  which frees the symbol for a successor; inactive instruments are mapped from their
  history only. Inactive records are applied before active ones within a run, so a
  successor that took over a symbol in the same listing finds it free.

Pool membership
  Active instruments are DISCOVERY_POOL members; deactivation removes them from every layer.
  Being outside a trading session never changes anything here: status comes from the
  provider's directory only.

Safety
  Each chunk of records is one transaction under a provider-wide advisory lock, so
  concurrent discovery jobs (or a discovery job and a metadata refresh) serialize instead
  of racing; every step is idempotent, so a crashed job re-run stores nothing twice.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.asset_discovery import membership
from app.asset_discovery.config import AssetDiscoverySettings
from app.asset_discovery.enums import (
    DiscoveryMode,
    DiscoveryOutcome,
    LifecycleEventType,
    MembershipAction,
    ProviderAssetStatus,
    ReferenceSource,
    UniverseLayer,
    ValidFromBasis,
)
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.errors import UnsupportedCapabilityError
from app.data_engine.providers.base import MalformedRecord, MarketDataProvider
from app.data_engine.reference import (
    InstrumentDirectory,
    InstrumentDirectoryRequest,
    ReferenceInstrument,
)
from app.database.models import (
    Asset,
    AssetIdentifier,
    AssetLifecycleEventRecord,
    AssetReferenceVersionRecord,
    AssetStatus,
    DiscoveryResultRecord,
    MappingStatus,
    ProviderAssetRecord,
    ProviderExchangeCodeRecord,
)
from app.database.repositories import AssetMappingRepository
from app.database.sessions import session_scope

logger = get_logger(__name__)

ONE_MICROSECOND = timedelta(microseconds=1)
METADATA_SOURCE = "{provider}:reference-directory"
# Fields whose change is reported as METADATA_CHANGE (the rest have their own event types).
_METADATA_FIELDS = (
    "marginable",
    "shortable",
    "easy_to_borrow",
    "borrow_status",
    "fractionable",
    "margin_requirement_long",
    "margin_requirement_short",
    "min_order_size",
    "min_trade_increment",
    "price_increment",
    "attributes",
)


def lock_key(provider: str) -> str:
    return f"asset_discovery:{provider}"


def asset_status(instrument: ReferenceInstrument) -> AssetStatus:
    if instrument.status is ProviderAssetStatus.ACTIVE:
        return AssetStatus.ACTIVE
    return AssetStatus.DELISTED if instrument.delisting_evidence else AssetStatus.INACTIVE


@dataclass
class DirectoryCounters:
    received: int = 0
    malformed: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    conflicts: int = 0
    missing: int = 0
    reappeared: int = 0
    verified: int = 0
    verification_deferred: int = 0
    unmapped_exchange: int = 0
    lifecycle: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {k: v for k, v in vars(self).items() if k != "lifecycle"}
        values["lifecycle_events"] = dict(sorted(self.lifecycle.items()))
        return values


@dataclass(frozen=True)
class _Context:
    job_id: uuid.UUID
    provider: str
    observed_at: datetime
    source: ReferenceSource
    exchange_mics: dict[str, str | None]
    initial_catalog: bool
    shared_symbols: frozenset[str]
    assumed_start: datetime


def _snapshot(instrument: ReferenceInstrument, mic: str | None) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "provider_symbol": instrument.provider_symbol,
        "name": instrument.name,
        "exchange": mic,
        "provider_exchange": instrument.provider_exchange,
        "status": instrument.status.value,
        "tradable": instrument.tradable,
    }


def _version_snapshot(version: AssetReferenceVersionRecord) -> dict[str, Any]:
    return {
        "symbol": version.symbol,
        "provider_symbol": version.provider_symbol,
        "name": version.name,
        "exchange": version.exchange_mic,
        "provider_exchange": version.provider_exchange,
        "status": version.status.value,
        "tradable": version.tradable,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return list(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    return str(value)


class DiscoveryEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: MarketDataProvider,
        settings: AssetDiscoverySettings,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._settings = settings
        self._clock = clock

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    # ------------------------------------------------------------ directory discovery
    async def discover(self, job_id: uuid.UUID, mode: DiscoveryMode) -> DirectoryCounters:
        """List the provider directory for every supported asset class and apply it."""
        capabilities = self._provider.reference_capabilities()
        if capabilities is None:
            raise UnsupportedCapabilityError(
                f"{self._provider.display_name} has no asset directory"
            )
        market_classes = self._provider.capabilities().asset_classes
        counters = DirectoryCounters()
        # Only classes the provider can both list and serve market data for.
        for asset_class in sorted(capabilities.asset_classes & market_classes):
            status = ProviderAssetStatus.ACTIVE if mode is DiscoveryMode.INCREMENTAL else None
            directory = await self._provider.list_instruments(
                InstrumentDirectoryRequest(asset_class=asset_class, status=status)
            )
            logger.info(
                "discovery.directory_received",
                job_id=str(job_id),
                asset_class=asset_class.value,
                mode=mode.value,
                records=directory.record_count,
                malformed=len(directory.malformed),
                bytes=directory.response_bytes,
            )
            source = (
                ReferenceSource.DIRECTORY_FULL
                if mode is DiscoveryMode.FULL
                else ReferenceSource.DIRECTORY_ACTIVE
            )
            # REDACTED_FOR_GITHUB
            # job's lease heartbeat keeps running on the event loop.
            seen = await asyncio.to_thread(
                self.apply_directory, job_id, directory, source, counters
            )
            if mode is DiscoveryMode.FULL:
                await asyncio.to_thread(self._mark_missing, job_id, directory, seen, counters)
            else:
                await self._verify_absent_active(job_id, directory, seen, counters)
        return counters

    def apply_directory(
        self,
        job_id: uuid.UUID,
        directory: InstrumentDirectory,
        source: ReferenceSource,
        counters: DirectoryCounters,
    ) -> set[str]:
        """Apply every record (chunked transactions). Returns the provider IDs present
        in the response, including malformed records whose ID could be read."""
        counters.received += directory.record_count
        seen = {i.provider_asset_id for i in directory.instruments}
        seen |= {
            str(m.item["id"]) for m in directory.malformed if isinstance(m.item.get("id"), str)
        }
        self._record_malformed(job_id, directory.malformed, counters)
        context = self._context(job_id, directory.instruments, source, directory.received_at)
        counters.unmapped_exchange += sum(
            1
            for i in directory.instruments
            if i.provider_exchange is not None
            and context.exchange_mics.get(i.provider_exchange) is None
        )
        # Inactive first: a deactivated predecessor releases its symbol before a successor
        # that appears in the same listing claims it.
        ordered = sorted(
            directory.instruments,
            key=lambda i: (i.status is ProviderAssetStatus.ACTIVE, i.provider_asset_id),
        )
        size = self._settings.chunk_size
        for start in range(0, len(ordered), size):
            self._apply_chunk(context, ordered[start : start + size], counters)
        return seen

    def _context(
        self,
        job_id: uuid.UUID,
        instruments: Sequence[ReferenceInstrument],
        source: ReferenceSource,
        observed_at: datetime,
    ) -> _Context:
        provider = self.provider_id
        with session_scope(self._sessions) as session:
            mics = {
                row.provider_exchange: row.mic
                for row in session.scalars(
                    select(ProviderExchangeCodeRecord).where(
                        ProviderExchangeCodeRecord.provider == provider
                    )
                )
            }
            initial = (
                session.scalar(
                    select(func.count())
                    .select_from(ProviderAssetRecord)
                    .where(ProviderAssetRecord.provider == provider)
                )
                == 0
            )
            owners: dict[str, set[str]] = defaultdict(set)
            for symbol, provider_asset_id in session.execute(
                select(
                    ProviderAssetRecord.current_symbol, ProviderAssetRecord.provider_asset_id
                ).where(ProviderAssetRecord.provider == provider)
            ):
                owners[symbol].add(provider_asset_id)
            for symbol, asset_id in session.execute(
                select(AssetIdentifier.provider_symbol, AssetIdentifier.asset_id).where(
                    AssetIdentifier.provider == provider
                )
            ):
                owners[symbol].add(f"asset:{asset_id}")
        for instrument in instruments:
            owners[instrument.provider_symbol].add(instrument.provider_asset_id)
            owners[instrument.symbol].add(instrument.provider_asset_id)
        shared = frozenset(symbol for symbol, ids in owners.items() if len(ids) > 1)
        return _Context(
            job_id=job_id,
            provider=provider,
            observed_at=observed_at,
            source=source,
            exchange_mics=mics,
            initial_catalog=initial,
            shared_symbols=shared,
            assumed_start=self._settings.assumed_history_start_at,
        )

    def _apply_chunk(
        self,
        ctx: _Context,
        instruments: Sequence[ReferenceInstrument],
        counters: DirectoryCounters,
    ) -> None:
        with session_scope(self._sessions) as session:
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key(ctx.provider), 0)))
            )
            ids = [i.provider_asset_id for i in instruments]
            existing = {
                row.provider_asset_id: row
                for row in session.scalars(
                    select(ProviderAssetRecord).where(
                        ProviderAssetRecord.provider == ctx.provider,
                        ProviderAssetRecord.provider_asset_id.in_(ids),
                    )
                )
            }
            local = DirectoryCounters()
            verified: list[uuid.UUID] = []
            new: list[ReferenceInstrument] = []
            for instrument in instruments:
                row = existing.get(instrument.provider_asset_id)
                if row is None:
                    new.append(instrument)
                else:
                    self._update(session, ctx, row, instrument, local)
                    verified.append(row.asset_id)
            if new:
                batch = DirectoryCounters()
                try:
                    with session.begin_nested():
                        self._create_batch(session, ctx, new, batch)
                        session.flush()
                    _merge(local, batch)
                except IntegrityError:
                    # Something changed under us (e.g. an operator registration): redo the
                    # chunk's new instruments one by one, each guarded by its own savepoint.
                    logger.warning(
                        "discovery.batch_fallback", job_id=str(ctx.job_id), records=len(new)
                    )
                    for instrument in new:
                        self._create(session, ctx, instrument, local)
            if verified:
                session.execute(
                    update(Asset)
                    .where(Asset.id.in_(verified))
                    .values(last_verified_at=ctx.observed_at)
                )
        # Only counted once the chunk committed.
        _merge(counters, local)

    def _create_batch(
        self,
        session: Session,
        ctx: _Context,
        instruments: Sequence[ReferenceInstrument],
        counters: DirectoryCounters,
    ) -> None:
        """First sight of many instruments with one round of reads and one flush.

        Same decisions as ``_create`` (adoption, listing conflicts, mapping start), but the
        checks run against state read once for the whole chunk and kept current in memory.
        The caller holds the provider lock; anything that still violates a constraint
        aborts the batch and the caller falls back to ``_create``.
        """
        active = [i for i in instruments if i.status is ProviderAssetStatus.ACTIVE]
        symbols = {i.provider_symbol for i in active}
        periods: dict[str, list[tuple[datetime, datetime | None]]] = defaultdict(list)
        current: dict[str, AssetIdentifier] = {}
        if symbols:
            for m in session.scalars(
                select(AssetIdentifier).where(
                    AssetIdentifier.provider == ctx.provider,
                    AssetIdentifier.provider_symbol.in_(symbols),
                )
            ):
                periods[m.provider_symbol].append((m.valid_from, m.valid_to))
                if m.valid_to is None and m.valid_from <= ctx.observed_at:
                    current[m.provider_symbol] = m
        mapped_ids = {m.asset_id for m in current.values()}
        linked: set[uuid.UUID] = set()
        if mapped_ids:
            linked = set(
                session.scalars(
                    select(ProviderAssetRecord.asset_id).where(
                        ProviderAssetRecord.provider == ctx.provider,
                        ProviderAssetRecord.asset_id.in_(mapped_ids),
                    )
                )
            )
        adoptable: dict[uuid.UUID, Asset] = {}
        if mapped_ids - linked:
            adoptable = {
                a.id: a
                for a in session.scalars(select(Asset).where(Asset.id.in_(mapped_ids - linked)))
            }
        holders: dict[tuple[str, str, str | None], uuid.UUID] = {}
        if active:
            for asset_id, symbol, asset_class, mic in session.execute(
                select(Asset.id, Asset.symbol, Asset.asset_class, Asset.exchange_code).where(
                    Asset.status == AssetStatus.ACTIVE,
                    Asset.symbol.in_({i.symbol for i in active}),
                )
            ):
                holders[(symbol, asset_class.value, mic)] = asset_id

        planned: list[
            tuple[ReferenceInstrument, Asset, AssetReferenceVersionRecord, str | None, bool]
        ] = []
        # Inserted table by table: without ORM relationships the unit of work does not
        # order inserts by foreign key.
        new_assets: list[Asset] = []
        new_rows: list[ProviderAssetRecord] = []
        for instrument in instruments:
            mic = self._mic(ctx, instrument, counters)
            is_active = instrument.status is ProviderAssetStatus.ACTIVE
            asset: Asset | None = None
            adopted = False
            if is_active:
                mapping = current.get(instrument.provider_symbol)
                candidate = None if mapping is None else adoptable.pop(mapping.asset_id, None)
                if (
                    candidate is not None
                    and candidate.status is AssetStatus.ACTIVE
                    and candidate.asset_class.value in ("EQUITY", "ETF")
                ):
                    asset, adopted = candidate, True
                    asset.first_discovered_at = asset.first_discovered_at or ctx.observed_at
                    asset.last_verified_at = ctx.observed_at
                    asset.metadata_source = METADATA_SOURCE.format(provider=ctx.provider)
                    if instrument.name and not asset.name:
                        asset.name = instrument.name
                    if mic is not None and asset.exchange_code != mic:
                        asset.exchange_code = mic
            key = (instrument.symbol, instrument.asset_class.value, mic)
            if asset is None and is_active and key in holders:
                self._result(
                    session,
                    ctx,
                    instrument,
                    DiscoveryOutcome.CONFLICT,
                    f"another active asset ({holders[key]}) already lists {instrument.symbol} "
                    f"on {mic or 'an unmapped exchange'}; not catalogued until resolved",
                    asset_id=holders[key],
                )
                counters.conflicts += 1
                continue
            if asset is None:
                asset = Asset(
                    id=uuid.uuid4(),
                    symbol=instrument.symbol,
                    name=instrument.name,
                    asset_class=instrument.asset_class,
                    exchange_code=mic,
                    quote_currency=instrument.quote_currency,
                    status=asset_status(instrument),
                    instrument_type=instrument.instrument_type,
                    first_discovered_at=ctx.observed_at,
                    last_verified_at=ctx.observed_at,
                    metadata_source=METADATA_SOURCE.format(provider=ctx.provider),
                )
                new_assets.append(asset)
            if is_active:
                holders[key] = asset.id
            row = ProviderAssetRecord(
                id=uuid.uuid4(),
                provider=ctx.provider,
                provider_asset_id=instrument.provider_asset_id,
                asset_id=asset.id,
                provider_asset_class=instrument.provider_asset_class,
                provider_status=instrument.status,
                current_symbol=instrument.provider_symbol,
                current_version=1,
                current_content_sha256=instrument.content_sha256,
                first_seen_at=ctx.observed_at,
                last_seen_at=ctx.observed_at,
                last_seen_job_id=ctx.job_id,
            )
            new_rows.append(row)
            planned.append(
                (instrument, asset, self._version(ctx, row, instrument, mic, 1), mic, adopted)
            )
        for batch in (new_assets, new_rows, [p[2] for p in planned]):
            session.add_all(batch)
            session.flush()  # one batched INSERT per table; versions receive their IDs

        recorded_at = max(ctx.observed_at, self._clock.now())
        event_type = (
            LifecycleEventType.FIRST_DISCOVERED
            if ctx.initial_catalog
            else LifecycleEventType.NEW_LISTING
        )
        for instrument, asset, version, mic, adopted in planned:
            is_active = instrument.status is ProviderAssetStatus.ACTIVE
            if is_active and not adopted:
                options: list[tuple[datetime, ValidFromBasis]] = []
                if ctx.initial_catalog and instrument.provider_symbol not in ctx.shared_symbols:
                    options.append((ctx.assumed_start, ValidFromBasis.PROVIDER_HISTORY_ASSUMED))
                options.append((ctx.observed_at, ValidFromBasis.OBSERVED))
                taken = periods[instrument.provider_symbol]
                choice = next(
                    (
                        (start, basis)
                        for start, basis in options
                        if all(end is not None and end <= start for _, end in taken)
                    ),
                    None,
                )
                if choice is None:
                    self._mapping_conflict(
                        session, ctx, asset.id, instrument, version.id, counters, None
                    )
                else:
                    session.add(
                        AssetIdentifier(
                            id=uuid.uuid4(),
                            asset_id=asset.id,
                            provider=ctx.provider,
                            provider_symbol=instrument.provider_symbol,
                            provider_exchange=instrument.provider_exchange,
                            status=MappingStatus.ACTIVE,
                            valid_from=choice[0],
                            valid_from_basis=choice[1],
                            created_at=recorded_at,
                        )
                    )
                    taken.append((choice[0], None))
            reason = _creation_reason(ctx, adopted)
            self._lifecycle(
                session,
                ctx,
                asset.id,
                event_type,
                None,
                _snapshot(instrument, mic),
                reason,
                version.id,
            )
            counters.lifecycle[event_type.value] += 1
            if instrument.delisting_evidence:
                self._lifecycle(
                    session,
                    ctx,
                    asset.id,
                    LifecycleEventType.DELISTED,
                    None,
                    {"status": AssetStatus.DELISTED.value},
                    instrument.delisting_evidence,
                    version.id,
                )
                counters.lifecycle[LifecycleEventType.DELISTED.value] += 1
            if is_active:
                membership.transition(
                    session,
                    asset_id=asset.id,
                    layer=UniverseLayer.DISCOVERY_POOL,
                    action=MembershipAction.ADDED,
                    effective_at=ctx.observed_at,
                    recorded_at=recorded_at,
                    reason_code=event_type.value,
                    reason="active in the provider directory",
                    job_id=ctx.job_id,
                    previous=True if adopted else None,
                    flush=adopted,
                )
            counters.new += 1
            if not ctx.initial_catalog:
                self._result(
                    session,
                    ctx,
                    instrument,
                    DiscoveryOutcome.NEW,
                    "adopted operator-registered asset" if adopted else reason,
                    asset_id=asset.id,
                )

    # ------------------------------------------------------------ new instruments
    def _mic(
        self, ctx: _Context, instrument: ReferenceInstrument, counters: DirectoryCounters
    ) -> str | None:
        code = instrument.provider_exchange
        if code is None:
            return None
        return ctx.exchange_mics.get(code)

    def _create(
        self,
        session: Session,
        ctx: _Context,
        instrument: ReferenceInstrument,
        counters: DirectoryCounters,
    ) -> None:
        mic = self._mic(ctx, instrument, counters)
        active = instrument.status is ProviderAssetStatus.ACTIVE
        mappings = AssetMappingRepository(session)
        asset: Asset | None = None
        adopted = False
        if active:
            mapping = mappings.resolve(ctx.provider, instrument.provider_symbol, ctx.observed_at)
            if mapping is not None and mapping.valid_to is None:
                linked = session.scalar(
                    select(ProviderAssetRecord.id).where(
                        ProviderAssetRecord.provider == ctx.provider,
                        ProviderAssetRecord.asset_id == mapping.asset_id,
                    )
                )
                candidate = session.get(Asset, mapping.asset_id)
                if (
                    linked is None
                    and candidate is not None
                    and candidate.status is AssetStatus.ACTIVE
                    and candidate.asset_class.value in ("EQUITY", "ETF")
                ):
                    asset, adopted = candidate, True
        if asset is None and active:
            taken = self._active_listing_holder(session, instrument.symbol, instrument, mic, None)
            if taken is not None:
                self._result(
                    session,
                    ctx,
                    instrument,
                    DiscoveryOutcome.CONFLICT,
                    f"another active asset ({taken}) already lists {instrument.symbol} on "
                    f"{mic or 'an unmapped exchange'}; not catalogued until resolved",
                    asset_id=taken,
                )
                counters.conflicts += 1
                return
        if asset is None:
            asset = Asset(
                id=uuid.uuid4(),
                symbol=instrument.symbol,
                name=instrument.name,
                asset_class=instrument.asset_class,
                exchange_code=mic,
                quote_currency=instrument.quote_currency,
                status=asset_status(instrument),
                instrument_type=instrument.instrument_type,
                first_discovered_at=ctx.observed_at,
                last_verified_at=ctx.observed_at,
                metadata_source=METADATA_SOURCE.format(provider=ctx.provider),
            )
            session.add(asset)
            session.flush()
        else:
            previous_exchange = asset.exchange_code
            asset.first_discovered_at = asset.first_discovered_at or ctx.observed_at
            asset.last_verified_at = ctx.observed_at
            asset.metadata_source = METADATA_SOURCE.format(provider=ctx.provider)
            if instrument.name and not asset.name:
                asset.name = instrument.name
            if mic is not None and previous_exchange != mic:
                asset.exchange_code = mic
        row = ProviderAssetRecord(
            id=uuid.uuid4(),
            provider=ctx.provider,
            provider_asset_id=instrument.provider_asset_id,
            asset_id=asset.id,
            provider_asset_class=instrument.provider_asset_class,
            provider_status=instrument.status,
            current_symbol=instrument.provider_symbol,
            current_version=1,
            current_content_sha256=instrument.content_sha256,
            first_seen_at=ctx.observed_at,
            last_seen_at=ctx.observed_at,
            last_seen_job_id=ctx.job_id,
        )
        session.add(row)
        session.flush()
        version = self._add_version(session, ctx, row, instrument, mic, 1)

        if active and not adopted:
            self._map(session, ctx, asset, instrument, version.id, counters, new=True)
        event_type = (
            LifecycleEventType.FIRST_DISCOVERED
            if ctx.initial_catalog
            else LifecycleEventType.NEW_LISTING
        )
        reason = (
            "in the provider directory when Atlas first catalogued it (not a listing date)"
            if ctx.initial_catalog
            else "appeared in the provider directory after the initial catalog; the "
            "provider reports no listing date"
        )
        if adopted:
            reason = "linked to the operator-registered asset with the same current symbol"
        self._lifecycle(
            session, ctx, asset.id, event_type, None, _snapshot(instrument, mic), reason, version.id
        )
        counters.lifecycle[event_type.value] += 1
        if instrument.delisting_evidence:
            self._lifecycle(
                session,
                ctx,
                asset.id,
                LifecycleEventType.DELISTED,
                None,
                {"status": AssetStatus.DELISTED.value},
                instrument.delisting_evidence,
                version.id,
            )
            counters.lifecycle[LifecycleEventType.DELISTED.value] += 1
        if active:
            membership.transition(
                session,
                asset_id=asset.id,
                layer=UniverseLayer.DISCOVERY_POOL,
                action=MembershipAction.ADDED,
                effective_at=ctx.observed_at,
                recorded_at=max(ctx.observed_at, self._clock.now()),
                reason_code=event_type.value,
                reason="active in the provider directory",
                job_id=ctx.job_id,
                # A new asset has no membership history; an adopted one is read.
                previous=True if adopted else None,
            )
        counters.new += 1
        if not ctx.initial_catalog:
            self._result(
                session,
                ctx,
                instrument,
                DiscoveryOutcome.NEW,
                "adopted operator-registered asset" if adopted else reason,
                asset_id=asset.id,
            )

    def _active_listing_holder(
        self,
        session: Session,
        symbol: str,
        instrument: ReferenceInstrument,
        mic: str | None,
        exclude: uuid.UUID | None,
    ) -> uuid.UUID | None:
        stmt = select(Asset.id).where(
            Asset.symbol == symbol,
            Asset.asset_class == instrument.asset_class,
            Asset.exchange_code.is_not_distinct_from(mic),
            Asset.status == AssetStatus.ACTIVE,
        )
        if exclude is not None:
            stmt = stmt.where(Asset.id != exclude)
        return session.scalar(stmt.limit(1))

    def _map(
        self,
        session: Session,
        ctx: _Context,
        asset: Asset,
        instrument: ReferenceInstrument,
        version_id: int,
        counters: DirectoryCounters,
        *,
        new: bool,
    ) -> AssetIdentifier | None:
        """Give an active instrument its current symbol mapping (or record why not)."""
        mappings = AssetMappingRepository(session)
        attempts: list[tuple[datetime, ValidFromBasis]] = []
        if new and ctx.initial_catalog and instrument.provider_symbol not in ctx.shared_symbols:
            attempts.append((ctx.assumed_start, ValidFromBasis.PROVIDER_HISTORY_ASSUMED))
        attempts.append((ctx.observed_at, ValidFromBasis.OBSERVED))
        for valid_from, basis in attempts:
            try:
                with session.begin_nested():
                    mapping = mappings.register(
                        asset_id=asset.id,
                        provider=ctx.provider,
                        provider_symbol=instrument.provider_symbol,
                        valid_from=valid_from,
                        provider_exchange=instrument.provider_exchange,
                    )
                    mapping.valid_from_basis = basis
                    session.flush()
                return mapping
            except IntegrityError:
                continue
        holder = mappings.resolve(ctx.provider, instrument.provider_symbol, ctx.observed_at)
        self._mapping_conflict(session, ctx, asset.id, instrument, version_id, counters, holder)
        return None

    def _mapping_conflict(
        self,
        session: Session,
        ctx: _Context,
        asset_id: uuid.UUID,
        instrument: ReferenceInstrument,
        version_id: int,
        counters: DirectoryCounters,
        holder: AssetIdentifier | None,
    ) -> None:
        detail = (
            f"symbol {instrument.provider_symbol} is currently mapped to asset {holder.asset_id}"
            if holder
            else f"symbol {instrument.provider_symbol} overlaps an existing mapping period"
        )
        self._lifecycle(
            session,
            ctx,
            asset_id,
            LifecycleEventType.MAPPING_CONFLICT,
            None,
            {"provider_symbol": instrument.provider_symbol},
            f"no current mapping: {detail}",
            version_id,
        )
        counters.lifecycle[LifecycleEventType.MAPPING_CONFLICT.value] += 1
        counters.conflicts += 1
        self._result(
            session,
            ctx,
            instrument,
            DiscoveryOutcome.CONFLICT,
            f"catalogued without a current mapping: {detail}",
            asset_id=asset_id,
        )

    # ------------------------------------------------------------ known instruments
    def _update(
        self,
        session: Session,
        ctx: _Context,
        row: ProviderAssetRecord,
        instrument: ReferenceInstrument,
        counters: DirectoryCounters,
    ) -> None:
        row.last_seen_at = max(row.last_seen_at, ctx.observed_at)
        row.last_seen_job_id = ctx.job_id
        if row.missing_since is not None:
            self._lifecycle(
                session,
                ctx,
                row.asset_id,
                LifecycleEventType.REAPPEARED_IN_DIRECTORY,
                {"missing_since": row.missing_since.isoformat()},
                None,
                "listed by the provider again",
                None,
            )
            counters.lifecycle[LifecycleEventType.REAPPEARED_IN_DIRECTORY.value] += 1
            counters.reappeared += 1
            row.missing_since = None
        if instrument.content_sha256 == row.current_content_sha256:
            counters.unchanged += 1
            return

        previous = session.scalars(
            select(AssetReferenceVersionRecord).where(
                AssetReferenceVersionRecord.provider_asset_ref == row.id,
                AssetReferenceVersionRecord.version == row.current_version,
            )
        ).one()
        if ctx.observed_at < previous.observed_at:
            # An older response (e.g. a slow concurrent job) must not overwrite newer facts.
            counters.unchanged += 1
            return
        asset = session.get(Asset, row.asset_id, with_for_update=True)
        assert asset is not None  # noqa: S101  # FK guarantees existence
        mic = self._mic(ctx, instrument, counters)
        version = self._add_version(session, ctx, row, instrument, mic, row.current_version + 1)
        row.current_version = version.version
        row.current_content_sha256 = instrument.content_sha256
        row.provider_status = instrument.status
        row.current_symbol = instrument.provider_symbol

        before = _version_snapshot(previous)
        after = _snapshot(instrument, mic)
        events: list[LifecycleEventType] = []

        def event(
            kind: LifecycleEventType, reason: str, prev: Any = before, new: Any = after
        ) -> None:
            self._lifecycle(session, ctx, asset.id, kind, prev, new, reason, version.id)
            counters.lifecycle[kind.value] += 1
            events.append(kind)

        was_active = previous.status is ProviderAssetStatus.ACTIVE
        is_active = instrument.status is ProviderAssetStatus.ACTIVE
        mappings = AssetMappingRepository(session)
        conflict: str | None = None

        if was_active and not is_active:
            asset.status = asset_status(instrument)
            current = mappings.current_for_asset(asset.id, ctx.provider)
            if current is not None:
                mappings.retire(
                    current.id, max(ctx.observed_at, current.valid_from + ONE_MICROSECOND)
                )
            event(LifecycleEventType.DEACTIVATED, "the provider reports the instrument inactive")
            if instrument.delisting_evidence:
                event(LifecycleEventType.DELISTED, instrument.delisting_evidence)
            for layer in UniverseLayer:
                membership.transition(
                    session,
                    asset_id=asset.id,
                    layer=layer,
                    action=MembershipAction.REMOVED,
                    effective_at=ctx.observed_at,
                    recorded_at=max(ctx.observed_at, self._clock.now()),
                    reason_code=asset.status.value,
                    reason=f"instrument became {asset.status.value.lower()} at the provider",
                    job_id=ctx.job_id,
                )
        elif not was_active and is_active:
            holder = self._active_listing_holder(
                session, instrument.symbol, instrument, mic, asset.id
            )
            if holder is not None:
                conflict = f"reactivated, but asset {holder} holds the active listing"
            else:
                asset.status = AssetStatus.ACTIVE
                asset.symbol = instrument.symbol
                asset.exchange_code = mic
                event(
                    LifecycleEventType.REACTIVATED,
                    "the provider reports the instrument active again",
                )
                session.flush()
                self._map(session, ctx, asset, instrument, version.id, counters, new=False)
                membership.transition(
                    session,
                    asset_id=asset.id,
                    layer=UniverseLayer.DISCOVERY_POOL,
                    action=MembershipAction.ADDED,
                    effective_at=ctx.observed_at,
                    recorded_at=max(ctx.observed_at, self._clock.now()),
                    reason_code=LifecycleEventType.REACTIVATED.value,
                    reason="active in the provider directory again",
                    job_id=ctx.job_id,
                )
        elif not was_active and not is_active:
            if instrument.delisting_evidence and asset.status is not AssetStatus.DELISTED:
                asset.status = AssetStatus.DELISTED
                event(LifecycleEventType.DELISTED, instrument.delisting_evidence)

        if instrument.symbol != previous.symbol and conflict is None:
            if is_active and was_active:
                holder = self._active_listing_holder(
                    session, instrument.symbol, instrument, mic, asset.id
                )
                if holder is not None:
                    conflict = f"renamed to {instrument.symbol}, held by active asset {holder}"
                else:
                    current = mappings.current_for_asset(asset.id, ctx.provider)
                    if current is not None:
                        mappings.retire(
                            current.id, max(ctx.observed_at, current.valid_from + ONE_MICROSECOND)
                        )
                    asset.symbol = instrument.symbol
                    session.flush()
                    self._map(session, ctx, asset, instrument, version.id, counters, new=False)
                    event(
                        LifecycleEventType.SYMBOL_CHANGE,
                        f"symbol changed from {previous.symbol} to {instrument.symbol}; the "
                        "instrument (provider ID) is unchanged",
                    )
            elif not is_active:
                asset.symbol = instrument.symbol
                event(
                    LifecycleEventType.SYMBOL_CHANGE,
                    f"inactive instrument's symbol changed from {previous.symbol} to "
                    f"{instrument.symbol}",
                )
        if (
            conflict is None
            and mic != previous.exchange_mic
            and (mic is not None or instrument.provider_exchange != previous.provider_exchange)
        ):
            if (
                is_active
                and mic != asset.exchange_code
                and self._active_listing_holder(session, asset.symbol, instrument, mic, asset.id)
            ):
                conflict = f"moved to {mic}, where another active asset lists {asset.symbol}"
            else:
                asset.exchange_code = mic
                event(
                    LifecycleEventType.EXCHANGE_CHANGE,
                    f"listing exchange changed from {previous.provider_exchange} to "
                    f"{instrument.provider_exchange}",
                )
        if instrument.name != previous.name:
            asset.name = instrument.name
            event(LifecycleEventType.NAME_CHANGE, "the provider reports a new name")
        if was_active and is_active and instrument.tradable != previous.tradable:
            if instrument.tradable is False:
                event(
                    LifecycleEventType.TRADING_SUSPENDED,
                    "the provider's tradable flag became false (suspension or broker "
                    "restriction; the provider does not say which)",
                )
            elif instrument.tradable is True:
                event(
                    LifecycleEventType.TRADING_RESUMED, "the provider's tradable flag became true"
                )
        changed = {
            name: {
                "from": _jsonable(getattr(previous, name)),
                "to": _jsonable(getattr(instrument, name)),
            }
            for name in _METADATA_FIELDS
            if _jsonable(getattr(previous, name)) != _jsonable(getattr(instrument, name))
        }
        if changed:
            event(
                LifecycleEventType.METADATA_CHANGE,
                f"reference fields changed: {', '.join(sorted(changed))}",
                prev={k: v["from"] for k, v in changed.items()},
                new={k: v["to"] for k, v in changed.items()},
            )
        asset.metadata_source = METADATA_SOURCE.format(provider=ctx.provider)
        if conflict is not None:
            counters.conflicts += 1
            self._lifecycle(
                session,
                ctx,
                asset.id,
                LifecycleEventType.MAPPING_CONFLICT,
                before,
                after,
                conflict,
                version.id,
            )
            counters.lifecycle[LifecycleEventType.MAPPING_CONFLICT.value] += 1
            self._result(
                session, ctx, instrument, DiscoveryOutcome.CONFLICT, conflict, asset_id=asset.id
            )
        counters.changed += 1
        self._result(
            session,
            ctx,
            instrument,
            DiscoveryOutcome.CHANGED,
            f"version {version.version}: "
            + (", ".join(e.value for e in events) or "provider record changed"),
            asset_id=asset.id,
            data={"version": version.version, "events": [e.value for e in events]},
        )

    # ------------------------------------------------------------ absences
    def _mark_missing(
        self,
        job_id: uuid.UUID,
        directory: InstrumentDirectory,
        seen: set[str],
        counters: DirectoryCounters,
    ) -> None:
        """After a complete listing: known instruments it did not contain are MISSING.

        Absence is recorded, not interpreted: the instrument's status is unchanged until
        the provider reports it (a lookup, or a later listing).
        """
        provider = self.provider_id
        observed_at = directory.received_at
        with session_scope(self._sessions) as session:
            session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key(provider), 0)))
            )
            absent = [
                row
                for row in session.scalars(
                    select(ProviderAssetRecord).where(
                        ProviderAssetRecord.provider == provider,
                        ProviderAssetRecord.missing_since.is_(None),
                        ProviderAssetRecord.first_seen_at <= observed_at,
                    )
                )
                if row.provider_asset_id not in seen
            ]
            ctx = _Context(
                job_id,
                provider,
                observed_at,
                ReferenceSource.DIRECTORY_FULL,
                {},
                False,
                frozenset(),
                self._settings.assumed_history_start_at,
            )
            for row in absent:
                row.missing_since = observed_at
                self._lifecycle(
                    session,
                    ctx,
                    row.asset_id,
                    LifecycleEventType.MISSING_FROM_DIRECTORY,
                    None,
                    {"provider_asset_id": row.provider_asset_id},
                    "absent from a complete directory listing (status unchanged; not "
                    "interpreted as a delisting)",
                    None,
                )
                session.add(
                    DiscoveryResultRecord(
                        job_id=job_id,
                        provider_asset_id=row.provider_asset_id,
                        provider_symbol=row.current_symbol,
                        asset_id=row.asset_id,
                        outcome=DiscoveryOutcome.MISSING,
                        detail="absent from a complete directory listing",
                        data={},
                    )
                )
            counters.missing += len(absent)
            counters.lifecycle[LifecycleEventType.MISSING_FROM_DIRECTORY.value] += len(absent)

    async def _verify_absent_active(
        self,
        job_id: uuid.UUID,
        directory: InstrumentDirectory,
        seen: set[str],
        counters: DirectoryCounters,
    ) -> None:
        """Active-only listing: known-active instruments absent from it are looked up one
        by one (bounded), because the listing alone cannot say why they are absent."""
        with session_scope(self._sessions) as session:
            candidates = [
                provider_asset_id
                for provider_asset_id in session.scalars(
                    select(ProviderAssetRecord.provider_asset_id)
                    .where(
                        ProviderAssetRecord.provider == self.provider_id,
                        ProviderAssetRecord.provider_status == ProviderAssetStatus.ACTIVE,
                        ProviderAssetRecord.first_seen_at <= directory.received_at,
                    )
                    .order_by(ProviderAssetRecord.last_seen_at)
                )
                if provider_asset_id not in seen
            ]
        limit = self._settings.verify_missing_max
        counters.verification_deferred += max(0, len(candidates) - limit)
        await self.lookup(job_id, candidates[:limit], counters)

    async def lookup(
        self, job_id: uuid.UUID, provider_asset_ids: Iterable[str], counters: DirectoryCounters
    ) -> None:
        """Single-instrument lookups (metadata refresh, absence verification)."""
        for provider_asset_id in provider_asset_ids:
            instrument = await self._provider.get_instrument(provider_asset_id)
            now = self._clock.now()
            if instrument is None:
                self._record_not_found(job_id, provider_asset_id, now, counters)
                continue
            counters.received += 1
            counters.verified += 1
            ctx = self._context(job_id, [instrument], ReferenceSource.SINGLE_LOOKUP, now)
            self._apply_chunk(ctx, [instrument], counters)

    def _record_not_found(
        self, job_id: uuid.UUID, provider_asset_id: str, now: datetime, counters: DirectoryCounters
    ) -> None:
        provider = self.provider_id
        with session_scope(self._sessions) as session:
            row = session.scalars(
                select(ProviderAssetRecord)
                .where(
                    ProviderAssetRecord.provider == provider,
                    ProviderAssetRecord.provider_asset_id == provider_asset_id,
                )
                .with_for_update()
            ).one_or_none()
            if row is None or row.missing_since is not None:
                return
            row.missing_since = now
            ctx = _Context(
                job_id,
                provider,
                now,
                ReferenceSource.SINGLE_LOOKUP,
                {},
                False,
                frozenset(),
                self._settings.assumed_history_start_at,
            )
            self._lifecycle(
                session,
                ctx,
                row.asset_id,
                LifecycleEventType.MISSING_FROM_DIRECTORY,
                None,
                {"provider_asset_id": provider_asset_id},
                "the provider answered 'not found' to a lookup by its own ID (status unchanged)",
                None,
            )
            session.add(
                DiscoveryResultRecord(
                    job_id=job_id,
                    provider_asset_id=provider_asset_id,
                    provider_symbol=row.current_symbol,
                    asset_id=row.asset_id,
                    outcome=DiscoveryOutcome.MISSING,
                    detail="lookup by provider ID returned not found",
                    data={},
                )
            )
            counters.missing += 1
            counters.lifecycle[LifecycleEventType.MISSING_FROM_DIRECTORY.value] += 1

    # ------------------------------------------------------------ records
    def _add_version(
        self,
        session: Session,
        ctx: _Context,
        row: ProviderAssetRecord,
        instrument: ReferenceInstrument,
        mic: str | None,
        number: int,
    ) -> AssetReferenceVersionRecord:
        version = self._version(ctx, row, instrument, mic, number)
        session.add(version)
        session.flush()
        return version

    def _version(
        self,
        ctx: _Context,
        row: ProviderAssetRecord,
        instrument: ReferenceInstrument,
        mic: str | None,
        number: int,
    ) -> AssetReferenceVersionRecord:
        return AssetReferenceVersionRecord(
            provider_asset_ref=row.id,
            asset_id=row.asset_id,
            provider=ctx.provider,
            provider_asset_id=instrument.provider_asset_id,
            version=number,
            observed_at=ctx.observed_at,
            source=ctx.source,
            discovery_job_id=ctx.job_id,
            symbol=instrument.symbol,
            provider_symbol=instrument.provider_symbol,
            name=instrument.name,
            provider_asset_class=instrument.provider_asset_class,
            provider_exchange=instrument.provider_exchange,
            exchange_mic=mic,
            status=instrument.status,
            tradable=instrument.tradable,
            marginable=instrument.marginable,
            shortable=instrument.shortable,
            easy_to_borrow=instrument.easy_to_borrow,
            borrow_status=instrument.borrow_status,
            fractionable=instrument.fractionable,
            margin_requirement_long=instrument.margin_requirement_long,
            margin_requirement_short=instrument.margin_requirement_short,
            min_order_size=instrument.min_order_size,
            min_trade_increment=instrument.min_trade_increment,
            price_increment=instrument.price_increment,
            attributes=list(instrument.attributes),
            delisting_evidence=instrument.delisting_evidence,
            raw_json=instrument.raw_json,
            content_sha256=instrument.content_sha256,
        )

    def _lifecycle(
        self,
        session: Session,
        ctx: _Context,
        asset_id: uuid.UUID,
        kind: LifecycleEventType,
        previous: dict[str, Any] | None,
        new: dict[str, Any] | None,
        reason: str,
        version_id: int | None,
    ) -> None:
        session.add(
            AssetLifecycleEventRecord(
                asset_id=asset_id,
                provider=ctx.provider,
                event_type=kind,
                effective_at=ctx.observed_at,
                observed_at=ctx.observed_at,
                previous_state=previous,
                new_state=new,
                reason=reason,
                discovery_job_id=ctx.job_id,
                reference_version_id=version_id,
            )
        )

    def _result(
        self,
        session: Session,
        ctx: _Context,
        instrument: ReferenceInstrument,
        outcome: DiscoveryOutcome,
        detail: str,
        *,
        asset_id: uuid.UUID | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            DiscoveryResultRecord(
                job_id=ctx.job_id,
                provider_asset_id=instrument.provider_asset_id,
                provider_symbol=instrument.provider_symbol,
                asset_id=asset_id,
                outcome=outcome,
                detail=detail,
                data=data or {},
            )
        )

    def _record_malformed(
        self,
        job_id: uuid.UUID,
        malformed: Sequence[MalformedRecord],
        counters: DirectoryCounters,
    ) -> None:
        if not malformed:
            return
        with session_scope(self._sessions) as session:
            for record in malformed:
                session.add(
                    DiscoveryResultRecord(
                        job_id=job_id,
                        provider_asset_id=str(record.item.get("id"))[:64]
                        if record.item.get("id") is not None
                        else None,
                        provider_symbol=(record.provider_symbol or "")[:128] or None,
                        outcome=DiscoveryOutcome.REJECTED,
                        detail=record.reason,
                        data={"item": record.item},
                    )
                )
        counters.malformed += len(malformed)


def _merge(into: DirectoryCounters, part: DirectoryCounters) -> None:
    for name, value in vars(part).items():
        if name == "lifecycle":
            into.lifecycle.update(value)
        elif name != "received":
            setattr(into, name, getattr(into, name) + value)


def _creation_reason(ctx: _Context, adopted: bool) -> str:
    if adopted:
        return "linked to the operator-registered asset with the same current symbol"
    if ctx.initial_catalog:
        return "in the provider directory when Atlas first catalogued it (not a listing date)"
    return (
        "appeared in the provider directory after the initial catalog; the provider reports "
        "no listing date"
    )


__all__ = ["DirectoryCounters", "DiscoveryEngine", "asset_status", "lock_key"]
