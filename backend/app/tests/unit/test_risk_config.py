"""Phase 6B risk configuration, governance and the separation from AI code (pure)."""

from __future__ import annotations

import ast
from decimal import Decimal as D
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.risk.config import HardLimits, Preferences, config_diff
from app.risk.governance import GovernanceError, require_operator

APP = Path(__file__).resolve().parents[2]


def test_defaults_are_the_specified_starting_values():
    h = HardLimits()
    assert h.risk_per_trade_pct == D("0.005")
    assert h.max_open_positions == 8
    assert h.daily_loss_limit_pct == D("0.02")
    assert h.weekly_loss_limit_pct == D("0.05")
    assert h.max_drawdown_pct == D("0.10")
    assert h.max_leverage == D("1")
    assert h.max_gross_exposure_pct == D("1.00")
    assert h.require_stop is True


def test_floats_are_refused():
    with pytest.raises(ValidationError, match="never a float"):
        HardLimits(risk_per_trade_pct=0.005)
    with pytest.raises(ValidationError, match="never a float"):
        Preferences(fee_bps=1.5)


@pytest.mark.parametrize(
    "values",
    [
        {"risk_per_trade_pct": "0"},
        {"risk_per_trade_pct": "0.06"},
        {"max_leverage": "2"},  # the ledger has no margin loan
        {"max_gross_exposure_pct": "1.0", "max_leverage": "0.5"},
        {"max_net_exposure_pct": "1", "max_gross_exposure_pct": "0.8", "max_leverage": "1"},
        {"weekly_loss_limit_pct": "0.01"},
        {"max_open_positions": 0},
        {"asset_class_limits_pct": {"OPTIONS": "1"}},
        {"unexpected_limit": "1"},
    ],
)
def test_incoherent_or_out_of_range_limits_are_refused(values):
    with pytest.raises(ValidationError):
        HardLimits.model_validate({**HardLimits().canonical(), **values})


def test_configuration_objects_are_immutable():
    h = HardLimits()
    with pytest.raises(ValidationError):
        h.risk_per_trade_pct = D("0.5")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Preferences().fee_bps = D("0")  # type: ignore[misc]


def test_hash_and_diff():
    a, p = HardLimits(), Preferences()
    assert a.sha256() == HardLimits().sha256()
    b = HardLimits.model_validate({**a.canonical(), "max_open_positions": 6})
    assert a.sha256() != b.sha256()
    changes = config_diff((a, p), (b, p))
    assert changes == [
        {"section": "hard_limits", "field": "max_open_positions", "previous": 8, "new": 6}
    ]
    first = config_diff(None, (a, p))
    assert len(first) == len(HardLimits.model_fields) + len(Preferences.model_fields)


@pytest.mark.parametrize(
    "actor",
    [
        "ai:coordinator",
        "strategy:momentum",
        "ml:model-v3",
        "llm:sentiment",
        "worker:strategy-1",
        "system:risk-monitor",
        "operator:",
        "operator: jacob",
        "OPERATOR:jacob",
        "",
        "operator:jacob;drop",
    ],
)
def test_only_operators_may_change_risk_configuration(actor):
    with pytest.raises(GovernanceError):
        require_operator(actor)


def test_operator_identity_is_accepted():
    assert require_operator("operator:jacob") == "operator:jacob"
    assert require_operator("operator:cli") == "operator:cli"


# ------------------------------------------------------------------ separation
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _modules(package: str) -> list[Path]:
    return sorted((APP / package).rglob("*.py"))


@pytest.mark.parametrize("package", ["ai_engine", "strategies", "ml_engine", "news", "workers"])
def test_ai_strategy_and_model_code_cannot_reach_risk_configuration(package):
    """No strategy, model, LLM or worker module imports the risk engine at all, so none can
    write a limit, reset a breaker or record an assessment."""
    offenders = {
        str(path.relative_to(APP)): sorted(m for m in _imports(path) if m.startswith("app.risk"))
        for path in _modules(package)
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_risk_engine_does_not_import_ai_strategy_or_model_code():
    """The risk engine reads stored rows (database models) only; no AI, ML, LLM or strategy
    code runs inside a risk decision."""
    forbidden = ("app.ai_engine", "app.ml_engine", "app.strategies", "app.news")
    offenders = {
        str(path.relative_to(APP)): sorted(m for m in _imports(path) if m.startswith(forbidden))
        for path in _modules("risk")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_execution_or_liquidation_in_the_risk_engine():
    """Phase 6B reserves, submits, executes and liquidates nothing."""
    for path in _modules("risk"):
        text = path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith(("#", '"', "*"))
        )
        for word in ("submit_order", "place_order", "paper_broker", "app.execution"):
            assert word not in code, f"{path.name} mentions {word}"
        assert "def liquidat" not in code
