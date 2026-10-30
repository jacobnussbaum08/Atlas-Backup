"""Data-access repositories. Callers own the transaction; repositories never commit."""

from app.database.repositories.asset_mapping import AssetMappingRepository, MappingError
from app.database.repositories.audit_log import AuditLogRepository
from app.database.repositories.live_market_data import (
    LiveObservationRepository,
    StreamRepository,
)
from app.database.repositories.market_data import (
    CoverageRow,
    IngestionJobRepository,
    MarketBarRepository,
    MarketDataEventRepository,
    SeriesKey,
)
from app.database.repositories.recovery import (
    CompletenessRepository,
    RecoveryJobRepository,
    RecoveryStatusRepository,
    Series,
)

__all__ = [
    "AssetMappingRepository",
    "AuditLogRepository",
    "CompletenessRepository",
    "CoverageRow",
    "IngestionJobRepository",
    "LiveObservationRepository",
    "MappingError",
    "MarketBarRepository",
    "MarketDataEventRepository",
    "RecoveryJobRepository",
    "RecoveryStatusRepository",
    "Series",
    "SeriesKey",
    "StreamRepository",
]
