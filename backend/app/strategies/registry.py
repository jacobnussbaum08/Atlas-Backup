"""Strategy discovery and validation (Phase 4A).

Every module in ``app.strategies.families`` that defines ``STRATEGY`` is registered. A
strategy is checked when it is registered: identifier format, metadata, default
parameters, timeframes the data layer can serve, and that every feature it requires on
every supported timeframe resolves in the feature registry. A broken strategy fails here,
at start-up and in the test suite, not in the middle of an evaluation.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Iterator

from app.feature_engine.registry import (
    BAR_TIMEFRAMES,
    REGISTRY,
    FeatureRegistry,
    FeatureRegistryError,
)
from app.strategies import families
from app.strategies.base import STRATEGY_ID_PATTERN, Strategy, StrategyDefinitionError


def validate_strategy(strategy: Strategy, features: FeatureRegistry = REGISTRY) -> None:
    sid = getattr(strategy, "strategy_id", "")
    if not STRATEGY_ID_PATTERN.match(sid):
        raise StrategyDefinitionError(f"invalid strategy id {sid!r}")
    for attribute in ("name", "description", "logic_version"):
        if not str(getattr(strategy, attribute, "")).strip():
            raise StrategyDefinitionError(f"{sid}: {attribute} is required")
    if not strategy.supported_asset_classes:
        raise StrategyDefinitionError(f"{sid}: no supported asset classes")
    if not strategy.supported_timeframes:
        raise StrategyDefinitionError(f"{sid}: no supported timeframes")
    unsupported = [t for t in strategy.supported_timeframes if t not in BAR_TIMEFRAMES]
    if unsupported:
        raise StrategyDefinitionError(f"{sid}: no data for timeframe(s) {', '.join(unsupported)}")
    if not strategy.schedule.triggers:
        raise StrategyDefinitionError(f"{sid}: the schedule has no trigger")
    defaults = strategy.default_parameters()
    for timeframe in strategy.supported_timeframes:
        requests = list(strategy.required_features(defaults, timeframe))
        if not requests:
            raise StrategyDefinitionError(f"{sid}: requires no features on {timeframe}")
        try:
            features.resolve_all(requests)
        except FeatureRegistryError as exc:
            raise StrategyDefinitionError(f"{sid} ({timeframe}): {exc}") from exc


class StrategyRegistry:
    def __init__(self, strategies: Iterable[Strategy] = (), features: FeatureRegistry = REGISTRY):
        self.features = features
        self._strategies: dict[str, Strategy] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: Strategy) -> None:
        validate_strategy(strategy, self.features)
        if strategy.strategy_id in self._strategies:
            raise StrategyDefinitionError(f"duplicate strategy id {strategy.strategy_id}")
        self._strategies[strategy.strategy_id] = strategy

    def get(self, strategy_id: str) -> Strategy:
        try:
            return self._strategies[strategy_id]
        except KeyError:
            raise StrategyDefinitionError(f"unknown strategy {strategy_id}") from None

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies

    def __iter__(self) -> Iterator[Strategy]:
        return iter(sorted(self._strategies.values(), key=lambda s: s.strategy_id))

    def __len__(self) -> int:
        return len(self._strategies)


def discover() -> list[Strategy]:
    found: list[Strategy] = []
    for module in pkgutil.iter_modules(families.__path__):
        loaded = importlib.import_module(f"{families.__name__}.{module.name}")
        strategy = getattr(loaded, "STRATEGY", None)
        if isinstance(strategy, Strategy):
            found.append(strategy)
    return found


def default_registry() -> StrategyRegistry:
    return StrategyRegistry(discover())


__all__ = ["StrategyRegistry", "default_registry", "discover", "validate_strategy"]
