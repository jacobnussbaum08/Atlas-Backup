"""Event driven (spec 11.6): validated company events confirmed by the price reaction.

It consumes Phase 3C news events through registry features. The direction comes **only
from the price and volume reaction**, never from article wording: Phase 3C categories are
keyword estimates (unverified) and Atlas stores no sentiment. An article alone never
produces a proposal. Methodology (logic ``event-driven-1``), LONG shown; SHORT mirrors:

1. **Validated event**: at least ``min_events`` articles directly about the asset in one of
   ``categories`` (relevance at least ``min_relevance``), published between the close of
   the session ``event_sessions`` before the newest completed one and the newest one's
   close (``period_news_count``). The event therefore precedes the reaction measured on
   the newest period(s); news published after that close waits for the next period. Only
   events available by the knowledge time count (``available_at``), and missing news
   coverage makes the count UNAVAILABLE, which blocks the evaluation: it is never "no news".
2. **Price reaction**: the close moved at least ``min_reaction_atr`` ATR over
   ``confirmation_return_periods`` periods.
3. **Volume reaction**: the newest period's volume at least ``min_volume_ratio`` x its mean.
4. **Close confirmation**: close location at least ``min_close_location``.

**Missing dependency**: macroeconomic releases and central-bank decisions need an economic
calendar, which is NOT_CONFIGURED (Phase 3C); they are not evaluated, and every
evaluation records that. Market-wide news categories count only articles directly
tagged to the asset.

Levels: entry = close; invalidation = entry - ``stop_atr_multiple`` x ATR; target = entry +
``target_atr_multiple`` x ATR; short default horizon (``holding_horizon_periods`` 3).

Strength (uncalibrated): mean of the reaction in ATR, the volume ratio against twice its
threshold and the close location.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import ClassVar

from pydantic import Field, field_validator

from app.database.models import AssetClass
from app.feature_engine.registry import FeatureRequest, FeatureValue
from app.news.economic_calendar import calendar_status
from app.news.enums import EventCategory
from app.strategies.base import (
    EvaluationContext,
    EvaluationSchedule,
    Strategy,
    StrategyOutcome,
    StrategyParameters,
    typed,
)
from app.strategies.enums import Direction, StrategyFamily, TriggerKind
from app.strategies.signals import (
    Assessment,
    conclude,
    draft,
    eligible_directions,
    fmt,
    invalid_data,
    make_levels,
    number,
    optional_number,
    pct,
    sign,
    strength,
)

DEFAULT_CATEGORIES = (
    EventCategory.EARNINGS,
    EventCategory.ANALYST_RATINGS,
    EventCategory.MERGERS_ACQUISITIONS,
    EventCategory.REGULATORY,
)


class EventDrivenParameters(StrategyParameters):
    holding_horizon_periods: int = Field(
        3, ge=1, le=260, description="expected holding horizon, in periods"
    )
    categories: tuple[EventCategory, ...] = Field(
        DEFAULT_CATEGORIES, min_length=1, max_length=8, description="event categories considered"
    )
    lookback_hours: int = Field(
        24, ge=1, le=168, description="window of the latest-article context feature"
    )
    min_relevance: Decimal = Field(
        Decimal("0.5"), ge=0, le=1, description="minimum relevance-v1 of the article to the asset"
    )
    confirmation_return_periods: int = Field(
        1, ge=1, le=5, description="price move that must confirm the event"
    )
    atr_length: int = Field(14, ge=2, le=50)
    volume_long: int = Field(20, ge=2, le=260)
    event_sessions: int = Field(
        1, ge=1, le=5, description="sessions before the newest close in which events count"
    )
    min_events: int = Field(1, ge=1, le=50, description="qualifying articles required")
    min_reaction_atr: Decimal = Field(
        Decimal("1.0"), gt=0, le=20, description="price reaction, in ATR"
    )
    min_volume_ratio: Decimal = Field(
        Decimal("1.5"), gt=0, le=50, description="reaction-period volume over its mean"
    )
    min_close_location: Decimal = Field(
        Decimal("0.6"), ge=Decimal("0.5"), le=1, description="close within the range (LONG)"
    )
    stop_atr_multiple: Decimal = Field(Decimal("1.5"), gt=0, le=20)
    target_atr_multiple: Decimal = Field(Decimal("2.5"), gt=0, le=40)

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, value: tuple[EventCategory, ...]) -> tuple[EventCategory, ...]:
        if len(set(value)) != len(value):
            raise ValueError("categories must be unique")
        return value


class EventDrivenStrategy(Strategy):
    strategy_id = "event_driven"
    family = StrategyFamily.EVENT_DRIVEN
    name = "Event driven"
    description = (
        "Considers validated company news events (Phase 3C) that precede a confirming price "
        "and volume reaction; the direction comes from the reaction, never from article "
        "wording. Missing news coverage blocks an evaluation rather than counting as no "
        "news; macroeconomic releases need an economic calendar that is not configured."
    )
    logic_version = "event-driven-1"
    implemented = True
    supported_asset_classes = (AssetClass.EQUITY, AssetClass.ETF)
    supported_timeframes = ("1Day",)
    schedule = EvaluationSchedule(
        (TriggerKind.BAR_CLOSE, TriggerKind.NEWS_EVENT),
        "after every completed daily period and whenever a news event about the asset "
        "becomes available",
    )
    Parameters: ClassVar[type[StrategyParameters]] = EventDrivenParameters

    @staticmethod
    def _news(p: EventDrivenParameters, category: str) -> dict[str, object]:
        return {
            "sessions": p.event_sessions,
            "min_relevance": float(p.min_relevance),
            "category": category,
        }

    @staticmethod
    def _count(ctx: EvaluationContext, p: EventDrivenParameters, category: str) -> FeatureValue:
        return ctx.feature(
            "period_news_count",
            sessions=p.event_sessions,
            min_relevance=float(p.min_relevance),
            category=category,
        )

    def required_features(
        self, params: StrategyParameters, timeframe: str
    ) -> Sequence[FeatureRequest]:
        p = typed(params, EventDrivenParameters)
        return [
            FeatureRequest("close", timeframe),
            FeatureRequest("return", timeframe, {"periods": p.confirmation_return_periods}),
            FeatureRequest("close_location", timeframe),
            FeatureRequest("atr", timeframe, {"length": p.atr_length}),
            FeatureRequest("volume_ratio", timeframe, {"short": 1, "long": p.volume_long}),
            FeatureRequest("period_news_count", timeframe, self._news(p, "ANY")),
            *(
                FeatureRequest("period_news_count", timeframe, self._news(p, c.value))
                for c in p.categories
            ),
            FeatureRequest(
                "news_hours_since_latest",
                timeframe,
                {
                    "lookback_hours": p.lookback_hours,
                    "min_relevance": float(p.min_relevance),
                    "category": "ANY",
                },
                optional=True,
            ),
        ]

    def evaluate(self, ctx: EvaluationContext, params: StrategyParameters) -> StrategyOutcome:
        p = typed(params, EventDrivenParameters)
        close_f = ctx.feature("close")
        ret_f = ctx.feature("return", periods=p.confirmation_return_periods)
        loc_f = ctx.feature("close_location")
        atr_f = ctx.feature("atr", length=p.atr_length)
        vol_f = ctx.feature("volume_ratio", short=1, long=p.volume_long)
        any_f = self._count(ctx, p, "ANY")
        category_f = {c: self._count(ctx, p, c.value) for c in p.categories}
        latest_f = ctx.feature(
            "news_hours_since_latest",
            lookback_hours=p.lookback_hours,
            min_relevance=float(p.min_relevance),
            category="ANY",
        )

        close, atr = number(close_f), number(atr_f)
        problem = invalid_data(close=close, atr=atr)
        if problem is not None:
            return StrategyOutcome(invalid_data=problem)
        ret, location, volume = number(ret_f), number(loc_f), number(vol_f)
        counts = {c: int(number(f)) for c, f in category_f.items()}
        qualifying = sum(counts.values())
        previous = close / (1 + ret)
        reaction_atr = (close - previous) / atr
        latest = optional_number(latest_f)
        calendar = calendar_status()
        notes = (
            f"economic calendar {calendar.availability.value}: macroeconomic releases and "
            "central-bank decisions are not evaluated (missing Phase 3C dependency)",
        )

        directions, blocked = eligible_directions(ctx, p)
        found = [f"{c.value} {n}" for c, n in counts.items() if n]

        def assess(direction: Direction) -> Assessment:
            s = sign(direction)
            a = Assessment(direction)
            a.require(
                "validated_event",
                qualifying >= p.min_events,
                f"{qualifying} qualifying article(s) since the close {p.event_sessions} "
                f"session(s) before the newest ({', '.join(found) or 'none in the categories'}; "
                f"{int(number(any_f))} about the asset in any category)",
                *(f for c, f in category_f.items() if counts[c]),
                role="trigger",
            )
            a.require(
                "price_reaction",
                s * reaction_atr >= float(p.min_reaction_atr),
                f"close moved {fmt(reaction_atr)} ATR ({pct(ret)}) over "
                f"{p.confirmation_return_periods} period(s) (min {p.min_reaction_atr})",
                ret_f,
                atr_f,
            )
            a.require(
                "volume_reaction",
                volume >= float(p.min_volume_ratio),
                f"volume {fmt(volume)} x its {p.volume_long}-period mean "
                f"(min {p.min_volume_ratio})",
                vol_f,
            )
            closes_strong = (
                location >= float(p.min_close_location)
                if s > 0
                else location <= 1 - float(p.min_close_location)
            )
            a.require("close_confirmation", closes_strong, f"close location {fmt(location)}", loc_f)
            a.inform(
                "macro_events",
                f"not evaluated: economic calendar {calendar.availability.value}",
            )
            if latest is not None:
                a.inform(
                    "latest_article", f"newest article {fmt(latest)} h before the cutoff", latest_f
                )
            return a

        assessments = [assess(d) for d in directions]
        drafts = []
        for a in assessments:
            if not a.passed:
                continue
            s = sign(a.direction)
            levels = make_levels(
                a.direction,
                entry=close,
                invalidation=close - s * float(p.stop_atr_multiple) * atr,
                target=close + s * float(p.target_atr_multiple) * atr,
                methods={
                    "entry": "newest completed close (after the reaction)",
                    "invalidation": f"entry -/+ {p.stop_atr_multiple} x ATR{p.atr_length}",
                    "target": f"entry +/- {p.target_atr_multiple} x ATR{p.atr_length}",
                },
                atr=atr,
                horizon_periods=p.holding_horizon_periods,
                setup="EVENT_REACTION",
            )
            if isinstance(levels, str):
                a.require("levels", False, levels)
                continue
            score = strength(
                s * reaction_atr / (2 * float(p.min_reaction_atr)),
                volume / (2 * float(p.min_volume_ratio)),
                location if s > 0 else 1 - location,
            )
            drafts.append(draft(a, levels, score, "Event driven"))
        return conclude(assessments, drafts, checks=blocked, notes=notes)


STRATEGY = EventDrivenStrategy()
