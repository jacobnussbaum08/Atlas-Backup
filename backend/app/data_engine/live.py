"""Live observation persistence (Deliverable 2C). Provider-independent.

The stream worker hands over decoded frames (``StreamBatch``) in small groups; each
group is written in **one transaction**:

    raw frame (verbatim) -> per item:
      malformed / unknown symbol / unmapped        -> rejected (quality event, not stored)
      bar   -> quality assessment -> dedupe/revision against stored bars (series lock)
      quote -> quality assessment -> insert, identical repeat = duplicate
      trade -> quality assessment -> insert, identical repeat = duplicate
      notice (trade correction / cancel / unhandled) -> quality event, observation untouched

Rules shared with historical ingestion (2B): bars live in ``market_bars`` with the same
series identity (asset, timeframe, provider, feed, adjustment) and the same revision
semantics, so a live bar and a REST bar for the same minute are either a duplicate or a
new revision; earlier revisions are never modified.

Timestamps on stored rows:
* ``received_at``: when the frame arrived from the provider.
* ``processed_at``: when this transaction validated the record.
* ``available_at``: when the validated record was handed to the database for downstream
  use (the start of the write). Rows become visible at commit, milliseconds later;
  point-in-time reads use ``ingested_at``.
* ``original_available_at``: the provider's delivery time (= ``received_at``).

Deliverable 2D additions:
* Every raw frame carries its 1-based ``frame_seq`` in the stream session; a group whose
  first frame is already stored was committed before (a lost commit acknowledgement) and
  is not written again.
* A quote/trade delivered more than ``stale_after`` after its event is stored ``STALE``.
* A new bar delivered more than ``stale_after`` after its interval ended is flagged
  ``LATE_ARRIVAL`` (kept: late is not wrong).
* A bar whose close moved more than ``PRICE_CHANGE_WARNING`` from the previous bar of the
  series is flagged ``UNEXPECTED_PRICE_CHANGE`` for review and kept ``VALID``: genuine
  gaps and fast markets are not data errors.
* Trade ids that have been arriving as a dense sequence (+1 each) and then skip values
  raise ``SEQUENCE_GAP`` evidence (the provider's history can then be reconciled).
* Duplicates are reported per symbol with the interval and raw frames involved.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clocks import Clock
from app.core.logging import get_logger
from app.data_engine.enums import (
    BarObservationKind,
    DataQualityEventType,
    DataQualityStatus,
    DataSource,
    DataType,
    Severity,
)
from app.data_engine.providers.base import StreamBatch, StreamNotice
from app.data_engine.schemas import (
    ProviderBar,
    ProviderQuote,
    ProviderTrade,
    epoch_microseconds,
)
from app.data_engine.timeframes import bar_interval_end
from app.data_engine.validation import (
    QualityIssue,
    assess_bar,
    assess_quote,
    assess_trade,
    same_observation,
)
from app.database.models import (
    Asset,
    AssetStatus,
    DataQualityEventRecord,
    MappingStatus,
    RawMarketDataEventRecord,
)
from app.database.repositories import (
    AssetMappingRepository,
    LiveObservationRepository,
    MarketBarRepository,
    MarketDataEventRepository,
    SeriesKey,
)
from app.database.sessions import session_scope

logger = get_logger(__name__)


class UnknownStreamAssetError(LookupError):
    """A configured symbol has no registered active asset or provider mapping."""


@dataclass(frozen=True)
class LiveTarget:
    """One subscribed instrument: Atlas asset + provider symbol + mapping validity."""

    symbol: str
    provider_symbol: str
    asset_id: uuid.UUID
    mapping_periods: tuple[tuple[datetime, datetime | None, uuid.UUID], ...]

    def mapped_at(self, at: datetime) -> bool:
        return any(
            start <= at and (end is None or at < end) and asset_id == self.asset_id
            for start, end, asset_id in self.mapping_periods
        )


@dataclass
class PersistResult:
    """Counts for one persisted group of frames (keys as in ``COUNTER_NAMES``)."""

    counts: Counter[str] = field(default_factory=Counter)
    latest_event_at: datetime | None = None
    # received_at - event_time of quotes/trades, in seconds (provider + network latency).
    event_lag_seconds: list[float] = field(default_factory=list)
    # Newest bar start persisted (the last completed candle Atlas holds).
    latest_bar_at: datetime | None = None


COUNTER_NAMES = (
    "frames",
    "items",
    "bars_stored",
    "bars_revised",
    "bars_duplicate",
    "quotes_stored",
    "quotes_duplicate",
    "trades_stored",
    "trades_duplicate",
    "records_quarantined",
    "records_rejected",
    "notices",
    "out_of_order",
    # Deliverable 2D
    "stale",
    "late_bars",
    "price_change_flags",
    "sequence_gaps",
    "replays_suppressed",
)

# Close-to-close move between consecutive bars of a series that is flagged for review.
PRICE_CHANGE_WARNING = Decimal("0.10")
# Consecutive +1 trade ids before a provider's ids are treated as a dense sequence.
DENSE_SEQUENCE_RUN = 20


@dataclass
class _Latest:
    """Latest known revision of a bar: a stored row or a row written in this transaction."""

    values: Any
    revision: int
    quality_status: DataQualityStatus
    source: DataSource
    observation_kind: BarObservationKind


def _sha256(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def quote_content(quote: ProviderQuote) -> dict[str, Any]:
    """Provider content of a quote: with ``event_time_ns`` it identifies a repeat, whether
    it arrived live or from the provider's history."""
    return {
        "symbol": quote.provider_symbol,
        "bid_price": _text(quote.bid_price),
        "ask_price": _text(quote.ask_price),
        "bid_size": _text(quote.bid_size),
        "ask_size": _text(quote.ask_size),
        "bid_exchange": quote.bid_exchange,
        "ask_exchange": quote.ask_exchange,
        "conditions": list(quote.conditions),
        "tape": quote.tape,
    }


def trade_content(trade: ProviderTrade) -> dict[str, Any]:
    return {
        "symbol": trade.provider_symbol,
        "price": str(trade.price),
        "size": _text(trade.size),
        "trade_id": trade.provider_trade_id,
        "exchange": trade.exchange,
        "conditions": list(trade.conditions),
        "tape": trade.tape,
    }


def _observation_row(
    observation: ProviderQuote | ProviderTrade,
    asset_id: uuid.UUID,
    *,
    provider: str,
    feed: str,
    status: DataQualityStatus,
    content: dict[str, Any],
    now: datetime,
    raw_id: int,
) -> dict[str, Any]:
    exact = observation.event_time_ns
    if exact is None:
        raise ValueError("quotes and trades need a nanosecond event time")
    return {
        "asset_id": asset_id,
        "provider": provider,
        "feed": feed,
        "provider_symbol": observation.provider_symbol,
        "event_time": observation.event_time,
        "event_time_ns": exact,
        "conditions": list(observation.conditions),
        "tape": observation.tape,
        "source": observation.source,
        "quality_status": status,
        "content_sha256": _sha256(content),
        "received_at": observation.received_at,
        "available_at": max(observation.received_at, now),
        "original_available_at": observation.original_available_at,
        "processed_at": now,
        "raw_event_id": raw_id,
    }


def quote_row(
    quote: ProviderQuote,
    asset_id: uuid.UUID,
    *,
    provider: str,
    feed: str,
    status: DataQualityStatus,
    now: datetime,
    raw_id: int,
) -> dict[str, Any]:
    row = _observation_row(
        quote,
        asset_id,
        provider=provider,
        feed=feed,
        status=status,
        content=quote_content(quote),
        now=now,
        raw_id=raw_id,
    )
    row.update(
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        bid_size=quote.bid_size,
        ask_size=quote.ask_size,
        bid_exchange=quote.bid_exchange,
        ask_exchange=quote.ask_exchange,
    )
    return row


def trade_row(
    trade: ProviderTrade,
    asset_id: uuid.UUID,
    *,
    provider: str,
    feed: str,
    status: DataQualityStatus,
    now: datetime,
    raw_id: int,
) -> dict[str, Any]:
    row = _observation_row(
        trade,
        asset_id,
        provider=provider,
        feed=feed,
        status=status,
        content=trade_content(trade),
        now=now,
        raw_id=raw_id,
    )
    row.update(
        price=trade.price,
        size=trade.size,
        provider_trade_id=trade.provider_trade_id,
        exchange=trade.exchange,
    )
    return row


def price_change(previous_close: Decimal | None, close: Decimal) -> Decimal | None:
    """Relative close-to-close move if it exceeds ``PRICE_CHANGE_WARNING``, else None."""
    if previous_close is None or previous_close <= 0:
        return None
    move = abs(close / previous_close - 1)
    return move if move > PRICE_CHANGE_WARNING else None


def _bar_values(bar: Any) -> dict[str, str | None]:
    return {
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": _text(bar.volume),
        "vwap": _text(bar.vwap),
        "trade_count": None if bar.trade_count is None else str(bar.trade_count),
        "is_complete": str(bar.is_complete),
    }


class LiveIngestionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        provider: str,
        feed: str,
        clock: Clock,
        stale_after: timedelta | None = None,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._feed = feed
        self._clock = clock
        self._stale_after = stale_after
        self._targets: dict[str, LiveTarget] = {}
        # Last event time seen per (provider symbol, stream), for out-of-order detection.
        self._last_seen: dict[tuple[str, str], int] = {}
        # Last close per (provider symbol, timeframe), for price-change review flags.
        self._last_close: dict[tuple[str, str], Decimal] = {}
        # Per provider symbol: (last trade id, consecutive +1 steps) for sequence evidence.
        self._trade_ids: dict[str, tuple[int, int]] = {}

    @property
    def targets(self) -> dict[str, LiveTarget]:
        return dict(self._targets)

    # ------------------------------------------------------------ universe
    def load_targets(self, symbols: Iterable[str]) -> dict[str, LiveTarget]:
        """Resolve configured Atlas symbols to assets and current provider mappings.

        Uses the same registered assets and ``asset_identifiers`` mappings as historical
        ingestion; nothing about the universe is hardcoded here.
        """
        targets: dict[str, LiveTarget] = {}
        with session_scope(self._sessions) as session:
            mappings = AssetMappingRepository(session)
            for symbol in symbols:
                found = session.scalars(
                    select(Asset).where(Asset.symbol == symbol, Asset.status == AssetStatus.ACTIVE)
                ).all()
                if len(found) != 1:
                    problem = "is not a registered active asset" if not found else "is ambiguous"
                    raise UnknownStreamAssetError(
                        f"'{symbol}' {problem}; register it from reference data first"
                    )
                asset = found[0]
                mapping = mappings.current_for_asset(asset.id, self._provider)
                if mapping is None or mapping.status is not MappingStatus.ACTIVE:
                    raise UnknownStreamAssetError(
                        f"'{symbol}' has no active {self._provider} symbol mapping"
                    )
                history = mappings.history(self._provider, mapping.provider_symbol)
                targets[mapping.provider_symbol] = LiveTarget(
                    symbol=asset.symbol,
                    provider_symbol=mapping.provider_symbol,
                    asset_id=asset.id,
                    mapping_periods=tuple((m.valid_from, m.valid_to, m.asset_id) for m in history),
                )
        self._targets = targets
        return dict(targets)

    # ------------------------------------------------------------ persistence
    def persist(
        self,
        batches: Sequence[StreamBatch],
        stream_session_id: uuid.UUID | None,
        frame_seqs: Sequence[int] | None = None,
    ) -> PersistResult:
        """Write a group of frames atomically. Raises ``SQLAlchemyError`` on database failure
        (nothing of the group is written; the caller retries the same group).

        ``frame_seqs`` are the frames' 1-based positions in the stream session. If the
        group's first frame is already stored, an earlier attempt committed (its
        acknowledgement was lost) and nothing is written again.
        """
        result = PersistResult()
        if not batches:
            return result
        if frame_seqs is not None and len(frame_seqs) != len(batches):
            raise ValueError("one frame sequence number per batch")
        state = (dict(self._last_seen), dict(self._last_close), dict(self._trade_ids))
        try:
            with session_scope(self._sessions) as session:
                if (
                    stream_session_id is not None
                    and frame_seqs
                    and self._frame_stored(session, stream_session_id, frame_seqs[0])
                ):
                    result.counts["replays_suppressed"] += len(batches)
                    logger.warning(
                        "live.replay_suppressed",
                        frames=len(batches),
                        first_frame_seq=frame_seqs[0],
                        reason="group already committed (lost commit acknowledgement)",
                    )
                    return result
                writer = _GroupWriter(self, session, stream_session_id, result)
                for index, batch in enumerate(batches):
                    writer.add(batch, None if frame_seqs is None else frame_seqs[index])
                writer.flush()
        except BaseException:
            # The group will be retried as if never seen.
            self._last_seen, self._last_close, self._trade_ids = state
            raise
        return result

    @staticmethod
    def _frame_stored(session: Session, stream_session_id: uuid.UUID, frame_seq: int) -> bool:
        return bool(
            session.scalar(
                select(
                    exists().where(
                        RawMarketDataEventRecord.stream_session_id == stream_session_id,
                        RawMarketDataEventRecord.frame_seq == frame_seq,
                    )
                )
            )
        )

    def is_stale(self, received_at: datetime, event_time: datetime) -> bool:
        return self._stale_after is not None and received_at - event_time > self._stale_after

    def check_price_change(
        self, provider_symbol: str, timeframe: str, close: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        """(previous close, move) if the move from the previous bar exceeds the threshold."""
        key = (provider_symbol, timeframe)
        previous = self._last_close.get(key)
        self._last_close[key] = close
        move = price_change(previous, close)
        return (previous, move) if move is not None else (None, None)

    def check_trade_sequence(
        self, provider_symbol: str, trade_id: str | None
    ) -> tuple[int, int] | None:
        """Skipped id range ``(first_missing, last_missing)`` if a dense sequence jumped."""
        if trade_id is None or not trade_id.isdigit():
            return None
        current = int(trade_id)
        previous = self._trade_ids.get(provider_symbol)
        if previous is None:
            self._trade_ids[provider_symbol] = (current, 0)
            return None
        last, run = previous
        if current <= last:
            return None  # out of order or repeated; handled elsewhere
        if current == last + 1:
            self._trade_ids[provider_symbol] = (current, run + 1)
            return None
        dense = run >= DENSE_SEQUENCE_RUN
        # A jump keeps the sequence "dense" so consecutive skips are all reported.
        self._trade_ids[provider_symbol] = (current, run if dense else 0)
        return (last + 1, current - 1) if dense else None

    def record_events(self, events: Sequence[DataQualityEventRecord]) -> None:
        with session_scope(self._sessions) as session:
            MarketDataEventRepository(session).add_quality_events(events)

    def event(
        self,
        issue: QualityIssue,
        *,
        target: LiveTarget | None = None,
        provider_symbol: str | None = None,
        data_type: DataType | None = None,
        timeframe: str | None = None,
        interval: tuple[datetime, datetime] | None = None,
        raw_id: int | None = None,
        status: DataQualityStatus | None = None,
    ) -> DataQualityEventRecord:
        return DataQualityEventRecord(
            event_type=issue.event_type,
            severity=issue.severity,
            provider=self._provider,
            feed=self._feed,
            asset_id=target.asset_id if target else None,
            provider_symbol=target.provider_symbol if target else provider_symbol,
            data_type=data_type,
            timeframe=timeframe,
            interval_start=interval[0] if interval else None,
            interval_end=interval[1] if interval else None,
            detected_at=self._clock.now(),
            resulting_status=status,
            message=issue.message,
            details=_jsonable(issue.details),
            raw_event_id=raw_id,
        )

    def check_order(self, provider_symbol: str, stream: str, at_ns: int) -> bool:
        """Remember the newest event per symbol/stream; False if ``at_ns`` is older."""
        key = (provider_symbol, stream)
        last = self._last_seen.get(key)
        if last is not None and at_ns < last:
            return False
        self._last_seen[key] = at_ns
        return True


class _GroupWriter:
    """Accumulates one transaction's rows and quality events."""

    def __init__(
        self,
        service: LiveIngestionService,
        session: Session,
        stream_session_id: uuid.UUID | None,
        result: PersistResult,
    ) -> None:
        self.s = service
        self.session = session
        self.stream_session_id = stream_session_id
        self.result = result
        self.counts = result.counts
        self.now = service._clock.now()
        self.events: list[DataQualityEventRecord] = []
        self.bars: dict[tuple[uuid.UUID, str], list[tuple[ProviderBar, LiveTarget, int]]] = (
            defaultdict(list)
        )
        self.quotes: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []
        self.observation_targets: dict[str, LiveTarget] = {}
        # (target, data type, event type, message, severity, status) -> [count, first, last]
        self.aggregated: dict[tuple[Any, ...], list[Any]] = {}

    # ------------------------------------------------------------ intake
    def add(self, batch: StreamBatch, frame_seq: int | None = None) -> None:
        raw = batch.raw_event
        body = raw.raw_body or ""
        raw_id = MarketDataEventRepository(self.session).add_raw(
            RawMarketDataEventRecord(
                provider=self.s._provider,
                feed=self.s._feed,
                source=raw.source,
                message_type=raw.message_type,
                provider_symbol=raw.provider_symbol,
                stream_session_id=self.stream_session_id,
                frame_seq=frame_seq if self.stream_session_id is not None else None,
                request_params={},
                received_at=raw.received_at,
                payload_text=body,
                payload_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
        self.counts["frames"] += 1
        self.counts["items"] += batch.item_count

        for item in batch.malformed:
            event_type = (
                DataQualityEventType.MISSING_FIELD
                if "missing required field" in item.reason
                else DataQualityEventType.REJECTED_MESSAGE
            )
            self._reject(
                QualityIssue(
                    event_type,
                    Severity.ERROR,
                    f"unparseable stream message rejected: {item.reason}",
                    {"item": _jsonable(item.item)},
                ),
                provider_symbol=item.provider_symbol,
                raw_id=raw_id,
            )
        for notice in batch.notices:
            self._notice(notice, raw_id)
        for bar in batch.bars:
            target = self._target(bar.provider_symbol, bar.event_time, DataType.BAR, raw_id)
            if target is not None:
                self.bars[(target.asset_id, str(bar.timeframe))].append((bar, target, raw_id))
        for quote in batch.quotes:
            target = self._target(quote.provider_symbol, quote.event_time, DataType.QUOTE, raw_id)
            if target is not None:
                self._quote(quote, target, raw_id)
        for trade in batch.trades:
            target = self._target(trade.provider_symbol, trade.event_time, DataType.TRADE, raw_id)
            if target is not None:
                self._trade(trade, target, raw_id)

    def _target(
        self, provider_symbol: str, at: datetime, data_type: DataType, raw_id: int
    ) -> LiveTarget | None:
        target = self.s._targets.get(provider_symbol)
        if target is None:
            self._reject(
                QualityIssue(
                    DataQualityEventType.UNEXPECTED_SYMBOL,
                    Severity.ERROR,
                    f"stream message for '{provider_symbol}', which is not subscribed",
                ),
                provider_symbol=provider_symbol,
                data_type=data_type,
                raw_id=raw_id,
            )
            return None
        if not target.mapped_at(at):
            self._reject(
                QualityIssue(
                    DataQualityEventType.UNEXPECTED_SYMBOL,
                    Severity.ERROR,
                    f"'{provider_symbol}' was not mapped to this asset at {at.isoformat()}",
                ),
                target=target,
                data_type=data_type,
                raw_id=raw_id,
            )
            return None
        return target

    def _reject(
        self,
        issue: QualityIssue,
        *,
        target: LiveTarget | None = None,
        provider_symbol: str | None = None,
        data_type: DataType | None = None,
        raw_id: int,
    ) -> None:
        self.counts["records_rejected"] += 1
        self.events.append(
            self.s.event(
                issue,
                target=target,
                provider_symbol=provider_symbol,
                data_type=data_type,
                raw_id=raw_id,
            )
        )

    def _notice(self, notice: StreamNotice, raw_id: int) -> None:
        self.counts["notices"] += 1
        if notice.kind.startswith("control:"):
            if notice.kind == "control:error":
                issue = QualityIssue(
                    DataQualityEventType.PROVIDER_ERROR,
                    Severity.ERROR,
                    "provider reported an error on the stream",
                    notice.details,
                )
            else:
                return  # acknowledgements; kept verbatim in the raw frame
        elif notice.kind == "trade_correction":
            issue = QualityIssue(
                DataQualityEventType.CORRECTION,
                Severity.WARNING,
                "provider corrected an earlier trade; stored trade left unchanged, "
                "correction recorded",
                notice.details,
            )
        elif notice.kind == "trade_cancel":
            issue = QualityIssue(
                DataQualityEventType.TRADE_CANCEL,
                Severity.WARNING,
                "provider cancelled or flagged an earlier trade as an error; stored trade left "
                "unchanged, cancellation recorded",
                notice.details,
            )
        else:
            issue = QualityIssue(
                DataQualityEventType.REJECTED_MESSAGE,
                Severity.INFO,
                f"stream message not consumed by Atlas ({notice.kind})",
                notice.details,
            )
        target = self.s._targets.get(notice.provider_symbol or "")
        interval = (notice.event_time, notice.event_time) if notice.event_time else None
        self.events.append(
            self.s.event(
                issue,
                target=target,
                provider_symbol=notice.provider_symbol,
                data_type=notice.data_type,
                interval=interval,
                raw_id=raw_id,
            )
        )

    def _aggregate(
        self,
        issue: QualityIssue,
        target: LiveTarget,
        data_type: DataType,
        at: datetime,
        status: DataQualityStatus | None,
    ) -> None:
        """Quote/trade findings are summarized per transaction instead of one event per tick."""
        key = (
            target.provider_symbol,
            data_type,
            issue.event_type,
            issue.message,
            issue.severity,
            status,
        )
        entry = self.aggregated.get(key)
        if entry is None:
            self.aggregated[key] = [1, at, at, target, issue.details]
        else:
            entry[0] += 1
            entry[1] = min(entry[1], at)
            entry[2] = max(entry[2], at)

    def _lag(self, received_at: datetime, event_time: datetime) -> None:
        self.result.event_lag_seconds.append((received_at - event_time).total_seconds())
        latest = self.result.latest_event_at
        self.result.latest_event_at = event_time if latest is None else max(latest, event_time)

    def _quote(self, quote: ProviderQuote, target: LiveTarget, raw_id: int) -> None:
        assessment = assess_quote(quote)
        for issue in assessment.issues:
            self._aggregate(issue, target, DataType.QUOTE, quote.event_time, assessment.status)
        exact = quote.event_time_ns
        assert exact is not None  # noqa: S101  # live quotes always carry nanoseconds
        if not self.s.check_order(target.provider_symbol, "quote", exact):
            self.counts["out_of_order"] += 1
            self._aggregate(
                QualityIssue(
                    DataQualityEventType.OUT_OF_ORDER_EVENT,
                    Severity.WARNING,
                    "quote older than one already received; stored in arrival order",
                ),
                target,
                DataType.QUOTE,
                quote.event_time,
                None,
            )
        status = self._status(assessment.status, quote, target, DataType.QUOTE)
        self._lag(quote.received_at, quote.event_time)
        self.observation_targets[target.provider_symbol] = target
        self.quotes.append(
            quote_row(
                quote,
                target.asset_id,
                provider=self.s._provider,
                feed=self.s._feed,
                status=status,
                now=self.now,
                raw_id=raw_id,
            )
        )

    def _status(
        self,
        status: DataQualityStatus,
        observation: ProviderQuote | ProviderTrade,
        target: LiveTarget,
        data_type: DataType,
    ) -> DataQualityStatus:
        if status is DataQualityStatus.QUARANTINED:
            self.counts["records_quarantined"] += 1
            return status
        if self.s.is_stale(observation.received_at, observation.event_time):
            self.counts["stale"] += 1
            self._aggregate(
                QualityIssue(
                    DataQualityEventType.STALE_DATA,
                    Severity.WARNING,
                    f"{data_type.value.lower()} delivered more than "
                    f"{self.s._stale_after} after its event; stored STALE",
                ),
                target,
                data_type,
                observation.event_time,
                DataQualityStatus.STALE,
            )
            return DataQualityStatus.STALE
        return status

    def _trade(self, trade: ProviderTrade, target: LiveTarget, raw_id: int) -> None:
        assessment = assess_trade(trade)
        for issue in assessment.issues:
            self._aggregate(issue, target, DataType.TRADE, trade.event_time, assessment.status)
        exact = trade.event_time_ns
        assert exact is not None  # noqa: S101  # live trades always carry nanoseconds
        if not self.s.check_order(target.provider_symbol, "trade", exact):
            self.counts["out_of_order"] += 1
            self._aggregate(
                QualityIssue(
                    DataQualityEventType.OUT_OF_ORDER_EVENT,
                    Severity.WARNING,
                    "trade older than one already received; stored in arrival order",
                ),
                target,
                DataType.TRADE,
                trade.event_time,
                None,
            )
        skipped = self.s.check_trade_sequence(target.provider_symbol, trade.provider_trade_id)
        if skipped is not None:
            missing = skipped[1] - skipped[0] + 1
            self.counts["sequence_gaps"] += 1
            self.events.append(
                self.s.event(
                    QualityIssue(
                        DataQualityEventType.SEQUENCE_GAP,
                        Severity.WARNING,
                        f"trade ids skipped {missing} value(s) ({skipped[0]}..{skipped[1]}): "
                        "trades may not have been delivered; reconcile with provider history",
                        {"first_missing": skipped[0], "last_missing": skipped[1]},
                    ),
                    target=target,
                    data_type=DataType.TRADE,
                    interval=(trade.event_time, trade.event_time),
                    raw_id=raw_id,
                )
            )
        status = self._status(assessment.status, trade, target, DataType.TRADE)
        self._lag(trade.received_at, trade.event_time)
        self.observation_targets[target.provider_symbol] = target
        self.trades.append(
            trade_row(
                trade,
                target.asset_id,
                provider=self.s._provider,
                feed=self.s._feed,
                status=status,
                now=self.now,
                raw_id=raw_id,
            )
        )

    # ------------------------------------------------------------ write
    def flush(self) -> None:
        for (_, timeframe), items in sorted(
            self.bars.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
        ):
            self._store_bars(timeframe, items)
        repo = LiveObservationRepository(self.session)
        new = repo.insert_quotes(self.quotes)
        self.counts["quotes_stored"] += len(new)
        self._duplicates("quotes", self.quotes, new, DataType.QUOTE)
        new = repo.insert_trades(self.trades)
        self.counts["trades_stored"] += len(new)
        self._duplicates("trades", self.trades, new, DataType.TRADE)
        for (_, data_type, event_type, message, severity, status), entry in self.aggregated.items():
            count, first, last, target, details = entry
            suffix = f" ({count} occurrences)" if count > 1 else ""
            self.events.append(
                self.s.event(
                    QualityIssue(
                        event_type, severity, f"{message}{suffix}", {**details, "count": count}
                    ),
                    target=target,
                    data_type=data_type,
                    interval=(first, last),
                    status=status,
                )
            )
        MarketDataEventRepository(self.session).add_quality_events(self.events)

    def _duplicates(
        self, kind: str, rows: list[dict[str, Any]], new: list[int], data_type: DataType
    ) -> None:
        """One INFO event per symbol: identical repeats are suppressed, never stored twice."""
        stored = set(new)
        repeats: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for index, row in enumerate(rows):
            if index not in stored:
                repeats[row["provider_symbol"]].append(row)
        for provider_symbol, items in repeats.items():
            self.counts[f"{kind}_duplicate"] += len(items)
            times = [row["event_time"] for row in items]
            self.events.append(
                self.s.event(
                    QualityIssue(
                        DataQualityEventType.DUPLICATE_EVENT,
                        Severity.INFO,
                        f"{len(items)} {kind} identical to stored observations were not "
                        "stored again",
                        {
                            "count": len(items),
                            "event_time_ns": [row["event_time_ns"] for row in items[:20]],
                            "raw_event_ids": sorted({row["raw_event_id"] for row in items})[:20],
                        },
                    ),
                    target=self.observation_targets.get(provider_symbol),
                    provider_symbol=provider_symbol,
                    data_type=data_type,
                    interval=(min(times), max(times)),
                    raw_id=items[0]["raw_event_id"],
                )
            )

    def _store_bars(self, timeframe: str, items: list[tuple[ProviderBar, LiveTarget, int]]) -> None:
        target = items[0][1]
        repo = MarketBarRepository(self.session)
        adjustment = items[0][0].adjustment
        key = SeriesKey(target.asset_id, timeframe, self.s._provider, self.s._feed, adjustment)
        repo.lock_series(key)
        latest: dict[datetime, _Latest] = {
            at: _Latest(row, row.revision, row.quality_status, row.source, row.observation_kind)
            for at, row in repo.latest_revisions(
                key, (bar.event_time for bar, _, _ in items)
            ).items()
        }
        rows: list[dict[str, Any]] = []
        for bar, _, raw_id in items:
            interval_end = bar_interval_end(bar.event_time, bar.timeframe) or bar.event_time
            interval = (bar.event_time, interval_end)
            if bar.observation_kind is BarObservationKind.LIVE_INITIAL:
                at_ns = epoch_microseconds(bar.event_time) * 1000
                if not self.s.check_order(target.provider_symbol, f"bar:{timeframe}", at_ns):
                    self.counts["out_of_order"] += 1
                    self.events.append(
                        self.s.event(
                            QualityIssue(
                                DataQualityEventType.OUT_OF_ORDER_EVENT,
                                Severity.WARNING,
                                "new bar older than one already received",
                            ),
                            target=target,
                            data_type=DataType.BAR,
                            timeframe=timeframe,
                            interval=interval,
                            raw_id=raw_id,
                        )
                    )
            assessment = assess_bar(bar)
            previous = latest.get(bar.event_time)
            if (
                previous is not None
                and same_observation(bar, previous.values)
                and previous.quality_status is assessment.status
            ):
                self.counts["bars_duplicate"] += 1
                continue
            for issue in assessment.issues:
                self.events.append(
                    self.s.event(
                        issue,
                        target=target,
                        data_type=DataType.BAR,
                        timeframe=timeframe,
                        interval=interval,
                        raw_id=raw_id,
                        status=assessment.status,
                    )
                )
            self._review_bar(bar, target, timeframe, interval, raw_id)
            revision = 0
            if previous is not None:
                revision = previous.revision + 1
                self.counts["bars_revised"] += 1
                expected = (
                    bar.observation_kind
                    in (BarObservationKind.LIVE_UPDATED, BarObservationKind.LIVE_RUNNING)
                    or previous.quality_status is DataQualityStatus.INCOMPLETE
                )
                self.events.append(
                    self.s.event(
                        QualityIssue(
                            DataQualityEventType.CORRECTION,
                            Severity.INFO if expected else Severity.WARNING,
                            f"bar changed since revision {previous.revision} "
                            f"({previous.source.value}/{previous.observation_kind.value}); stored "
                            f"as revision {revision} ({bar.observation_kind.value}), earlier "
                            "revision kept",
                            {"previous": _bar_values(previous.values), "current": _bar_values(bar)},
                        ),
                        target=target,
                        data_type=DataType.BAR,
                        timeframe=timeframe,
                        interval=interval,
                        raw_id=raw_id,
                        status=assessment.status,
                    )
                )
            if assessment.status is DataQualityStatus.QUARANTINED:
                self.counts["records_quarantined"] += 1
            latest[bar.event_time] = _Latest(
                bar, revision, assessment.status, bar.source, bar.observation_kind
            )
            latest_event = self.result.latest_event_at
            self.result.latest_event_at = (
                bar.event_time if latest_event is None else max(latest_event, bar.event_time)
            )
            if bar.observation_kind is not BarObservationKind.LIVE_RUNNING:
                latest_bar = self.result.latest_bar_at
                self.result.latest_bar_at = (
                    bar.event_time if latest_bar is None else max(latest_bar, bar.event_time)
                )
            rows.append(
                {
                    "asset_id": target.asset_id,
                    "provider": self.s._provider,
                    "feed": self.s._feed,
                    "provider_symbol": bar.provider_symbol,
                    "timeframe": timeframe,
                    "adjustment": bar.adjustment,
                    "event_time": bar.event_time,
                    "revision": revision,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "vwap": bar.vwap,
                    "trade_count": bar.trade_count,
                    "is_complete": bar.is_complete,
                    "source": bar.source,
                    "observation_kind": bar.observation_kind,
                    "quality_status": assessment.status,
                    "received_at": bar.received_at,
                    "available_at": max(bar.received_at, self.now),
                    "original_available_at": bar.original_available_at,
                    "processed_at": self.now,
                    "ingestion_job_id": None,
                    "raw_event_id": raw_id,
                }
            )
        self.counts["bars_stored"] += repo.insert(rows)

    def _review_bar(
        self,
        bar: ProviderBar,
        target: LiveTarget,
        timeframe: str,
        interval: tuple[datetime, datetime],
        raw_id: int,
    ) -> None:
        """Flags for review; neither changes the stored quality status."""
        if bar.observation_kind is BarObservationKind.LIVE_RUNNING:
            return
        if bar.observation_kind is BarObservationKind.LIVE_INITIAL and self.s.is_stale(
            bar.received_at, interval[1]
        ):
            self.counts["late_bars"] += 1
            self.events.append(
                self.s.event(
                    QualityIssue(
                        DataQualityEventType.LATE_ARRIVAL,
                        Severity.INFO,
                        f"bar delivered {(bar.received_at - interval[1]).total_seconds():.0f}s "
                        "after its interval ended",
                    ),
                    target=target,
                    data_type=DataType.BAR,
                    timeframe=timeframe,
                    interval=interval,
                    raw_id=raw_id,
                )
            )
        previous_close, move = self.s.check_price_change(
            target.provider_symbol, timeframe, bar.close
        )
        if move is not None:
            self.counts["price_change_flags"] += 1
            self.events.append(
                self.s.event(
                    QualityIssue(
                        DataQualityEventType.UNEXPECTED_PRICE_CHANGE,
                        Severity.WARNING,
                        f"close moved {move:.1%} from the previous bar; kept as reported "
                        "(large moves can be genuine), flagged for review",
                        {"previous_close": str(previous_close), "close": str(bar.close)},
                    ),
                    target=target,
                    data_type=DataType.BAR,
                    timeframe=timeframe,
                    interval=interval,
                    raw_id=raw_id,
                )
            )


__all__ = [
    "COUNTER_NAMES",
    "PRICE_CHANGE_WARNING",
    "LiveIngestionService",
    "LiveTarget",
    "PersistResult",
    "UnknownStreamAssetError",
    "price_change",
    "quote_content",
    "quote_row",
    "trade_content",
    "trade_row",
]
