"""Feature registry, the Phase 4A indicators and bar features (pure; no database).

Price series here are constructed numbers, not market data."""

from __future__ import annotations

import itertools
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from app.feature_engine import indicators as ind
from app.feature_engine.bar_features import BarHistory, compute_bar_feature
from app.feature_engine.features import Bar, _volume_ratio
from app.feature_engine.registry import (
    REGISTRY,
    FeatureRegistry,
    FeatureRegistryError,
    FeatureRequest,
    FeatureSource,
    FeatureStatus,
    FeatureValue,
    ValueKind,
)

T0 = datetime(2026, 1, 5, 5, tzinfo=UTC)


# --- indicators ---------------------------------------------------------------------------
def test_rsi_matches_wilders_definition():
    closes = [44.0, 44.3, 44.1, 43.6, 44.3, 44.8, 45.1, 45.4, 45.8, 46.1, 45.9, 46.3]
    n = 5
    gains = [max(b - a, 0.0) for a, b in itertools.pairwise(closes)]
    losses = [max(a - b, 0.0) for a, b in itertools.pairwise(closes)]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    expected: list[float | None] = [None] * len(closes)
    expected[n] = 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n, len(gains)):
        avg_g += (gains[i] - avg_g) / n
        avg_l += (losses[i] - avg_l) / n
        expected[i + 1] = 100 - 100 / (1 + avg_g / avg_l)
    got = ind.rsi(closes, n)
    assert got[:n] == [None] * n
    assert got[n:] == pytest.approx(expected[n:])


def test_rsi_edge_cases():
    assert ind.rsi([1.0, 2.0, 3.0, 4.0], 2)[-1] == 100.0  # no losses
    assert ind.rsi([5.0] * 6, 2)[-1] is None  # no change at all: undefined, not 50


def test_macd_line_signal_and_histogram():
    closes = [float(10 + (i % 7) + i * 0.3) for i in range(60)]
    line, signal, hist = ind.macd(closes, 12, 26, 9)
    fast, slow = ind.ema(closes, 12), ind.ema(closes, 26)
    assert fast[25] is not None
    assert slow[25] is not None
    assert line[25] == pytest.approx(fast[25] - slow[25])
    assert line[24] is None
    assert signal[25 + 8 - 1] is None
    assert signal[25 + 8] is not None
    defined = [v for v in line if v is not None]
    assert signal[-1] == pytest.approx(ind.ema(defined, 9)[-1])
    assert line[-1] is not None
    assert signal[-1] is not None
    assert hist[-1] == pytest.approx(line[-1] - signal[-1])
    with pytest.raises(ValueError, match="shorter"):
        ind.macd(closes, 26, 12, 9)


def test_bollinger_bands_agree_with_width():
    closes = [float(20 + math.sin(i)) for i in range(30)]
    middle, upper, lower = ind.bollinger_bands(closes, 20, 2.0)
    width = ind.bollinger_width(closes, 20, 2.0)
    assert middle[-1] == pytest.approx(sum(closes[-20:]) / 20)
    assert upper[-1] is not None
    assert lower[-1] is not None
    assert middle[-1] is not None
    assert (upper[-1] - lower[-1]) / middle[-1] == pytest.approx(width[-1])
    assert middle[18] is None


def test_volume_ratio_and_features_v1_compatibility():
    volumes = [100.0, 120.0, 80.0, 200.0, 220.0]
    assert ind.volume_ratio(volumes, 2, 5) == pytest.approx((200 + 220) / 2 / (720 / 5))
    assert ind.volume_ratio([*volumes[:-1], None], 2, 5) is None  # missing never zero
    assert ind.volume_ratio(volumes, 2, 6) is None  # not enough history
    assert ind.volume_ratio([0.0] * 5, 2, 5) is None
    # Phase 3B's feature set delegates to it: same value as its original definition.
    assert _volume_ratio(volumes[-2:], volumes) == ind.volume_ratio(volumes, 2, 5)
    with pytest.raises(ValueError, match="exceed"):
        ind.volume_ratio(volumes, 6, 5)


def test_prior_extreme_excludes_the_newest_value():
    highs = [1.0, 5.0, 3.0, 9.0]
    assert ind.prior_extreme(highs, 3, highest=True) == 5.0
    assert ind.prior_extreme(highs, 3, highest=False) == 1.0
    assert ind.prior_extreme(highs, 4, highest=True) is None


# --- registry -----------------------------------------------------------------------------
def test_registry_covers_the_required_measurements():
    names = {d.name for d in REGISTRY.definitions()}
    required = {"ema", "sma", "rsi", "atr", "adx", "macd_line", "macd_signal", "macd_histogram"}
    required |= {"bollinger_upper", "bollinger_lower", "bollinger_middle", "volume_ratio"}
    required |= {"return", "log_return", "news_event_count", "regime_label"}
    assert required <= names
    for d in REGISTRY.definitions():
        described = d.describe()
        for field in ("name", "version", "method", "timeframes", "required_data", "availability"):
            assert described[field], (d.name, field)
        assert len(d.definition_sha256) == 64
        if d.source is FeatureSource.BARS:
            assert d.window_rule


def test_resolution_gives_canonical_keys_and_validates_parameters():
    a = REGISTRY.resolve(FeatureRequest("ema", "1Day", {"length": 20}))
    b = REGISTRY.resolve(FeatureRequest("ema", "1Day", {}))
    assert a.key == b.key == "ema.v1(length=20)@1Day"
    assert a.window == 60
    k = REGISTRY.resolve(FeatureRequest("bollinger_upper", "1Week", {"k": 2}))
    assert k.key == "bollinger_upper.v1(k=2,length=20)@1Week"
    errors = [
        (FeatureRequest("ema", "1Day", {"span": 3}), "unknown parameter"),
        (FeatureRequest("ema", "1Day", {"length": 0}), "at least 1"),
        (FeatureRequest("ema", "1Day", {"length": 2.5}), "integer"),
        (FeatureRequest("ema", "1Day", {"length": True}), "integer"),
        (FeatureRequest("ema", "1Hour", {}), "does not support timeframe"),
        (FeatureRequest("macd_line", "1Day", {"fast": 30, "slow": 26}), "shorter"),
        (FeatureRequest("news_event_count", "1Day", {"category": "GOSSIP"}), "one of"),
        (FeatureRequest("nonexistent", "1Day", {}), "unknown feature"),
    ]
    for request, message in errors:
        with pytest.raises(FeatureRegistryError, match=message):
            REGISTRY.resolve(request)


def test_resolve_all_deduplicates_and_required_wins():
    resolved = REGISTRY.resolve_all(
        [
            FeatureRequest("rsi", "1Day", {"length": 14}, optional=True),
            FeatureRequest("rsi", "1Day", {}),
            FeatureRequest("close", "1Day"),
        ]
    )
    assert [(f.key, f.optional) for f in resolved] == [
        ("close.v1()@1Day", False),
        ("rsi.v1(length=14)@1Day", False),
    ]


def test_definition_hash_changes_with_the_definition():
    rsi = REGISTRY.get("rsi")
    assert replace(rsi, method="different").definition_sha256 != rsi.definition_sha256
    with pytest.raises(FeatureRegistryError, match="registered"):
        FeatureRegistry([rsi, rsi])


def test_feature_values_round_trip_exactly():
    value = FeatureValue(
        key="k",
        name="rsi",
        version="v1",
        source=FeatureSource.BARS,
        timeframe="1Day",
        params={"length": 14},
        value=0.1 + 0.2,
        status=FeatureStatus.VALID,
        reason=None,
        available_at=T0,
    )
    data = value.to_json()
    assert data["value"] == repr(0.1 + 0.2)
    assert FeatureValue.from_json("k", data, ValueKind.NUMBER) == value


# --- bar features ---------------------------------------------------------------------------
def bars(count: int, *, start: float = 100.0) -> dict[date, Bar]:
    out = {}
    for i in range(count):
        day = date(2026, 1, 5) + timedelta(days=i)
        close = start + i + (0.25 if i % 2 else -0.25)
        out[day] = Bar(
            event_time=T0 + timedelta(days=i),
            open=close - 0.5,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000.0 + i,
            available_at=T0 + timedelta(days=i, hours=17),
        )
    return out


def history(data: dict[date, Bar], periods=None) -> BarHistory:
    keys = periods if periods is not None else sorted(data)
    return BarHistory("1Day", keys, dict(data.items()))


def feature(name: str, **params):
    return REGISTRY.resolve(FeatureRequest(name, "1Day", params))


def test_bar_feature_uses_exactly_its_window_and_records_availability():
    data = bars(30)
    sma = feature("sma", length=5)
    value = compute_bar_feature(sma, history(data))
    closes = [b.close for b in list(data.values())[-5:]]
    assert value.status is FeatureStatus.VALID
    assert value.value == pytest.approx(sum(closes) / 5)
    assert value.available_at == list(data.values())[-1].available_at
    assert value.inputs["bars_used"] == 5
    # Older history added later cannot change a fixed-window value (EMA seeded in-window).
    ema = feature("ema", length=5)
    short = compute_bar_feature(ema, history(dict(list(data.items())[-15:])))
    full = compute_bar_feature(ema, history(data))
    assert short.value == full.value


def test_missing_period_inside_the_window_is_missing_inputs():
    data = bars(30)
    gap = sorted(data)[-3]
    periods = sorted(data)
    del data[gap]
    value = compute_bar_feature(feature("sma", length=5), history(data, periods))
    assert value.status is FeatureStatus.MISSING_INPUTS
    assert value.value is None
    # A gap outside the window does not matter.
    assert compute_bar_feature(feature("close"), history(data, periods)).valid


def test_short_history_is_insufficient_data():
    data = bars(10)
    periods = [date(2025, 12, 1) + timedelta(days=i) for i in range(5)] + sorted(data)
    value = compute_bar_feature(feature("sma", length=12), history(data, periods))
    assert value.status is FeatureStatus.INSUFFICIENT_DATA
    tiny = compute_bar_feature(feature("sma", length=12), history(bars(3)))
    assert tiny.status is FeatureStatus.INSUFFICIENT_DATA


def test_undefined_values_are_reported_not_zero():
    flat = {d: replace(b, close=50.0, high=50.0, low=50.0, open=50.0) for d, b in bars(30).items()}
    assert compute_bar_feature(feature("rsi", length=5), history(flat)).status is (
        FeatureStatus.UNDEFINED
    )
    assert compute_bar_feature(feature("zscore", length=5), history(flat)).status is (
        FeatureStatus.UNDEFINED
    )


def test_return_offsets_look_back_not_forward():
    data = bars(10)
    closes = [b.close for b in data.values()]
    newest = compute_bar_feature(feature("return", periods=1), history(data))
    lagged = compute_bar_feature(feature("return", periods=1, offset=2), history(data))
    assert newest.value == pytest.approx(closes[-1] / closes[-2] - 1)
    assert lagged.value == pytest.approx(closes[-3] / closes[-4] - 1)


def test_every_bar_feature_computes_on_a_long_clean_history():
    data = bars(600)
    for d in REGISTRY.definitions():
        if d.source is not FeatureSource.BARS:
            continue
        value = compute_bar_feature(REGISTRY.resolve(FeatureRequest(d.name, "1Day")), history(data))
        assert value.status is FeatureStatus.VALID, (d.name, value.reason)
        assert isinstance(value.value, float)
