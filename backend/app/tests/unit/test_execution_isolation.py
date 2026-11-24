"""Phase 7A paper-only isolation and authorization rules (static and pure checks).

* The paper broker and execution engine import no market-data provider, HTTP or websocket
  client: they cannot reach a brokerage (spec 24.1).
* Strategies, the AI decision engine and the ML engine import nothing from execution or the
  paper broker: they cannot submit orders.
* No configuration names a live mode or a broker selection.
* Only the execution coordinator and operators may submit.
"""

from __future__ import annotations

import ast
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.accounting.enums import EntrySource
from app.execution.enums import BrokerEventSource, OrderSide, OrderStatus, OrderType
from app.execution.models import OrderRequest
from app.execution.portfolio import MOVEMENT_SIGN, SOURCES, TEMPLATES, AccountingPositionLedger
from app.execution.validation import _price_fields, authorize_submitter, check_fill

APP = Path(__file__).resolve().parents[2]
ISOLATED = ("execution", "paper_broker", "events")
NO_ORDER_ACCESS = ("strategies", "ai_engine", "ml_engine", "market_intelligence", "news")
ENDPOINT_WORDS = ("alpaca.markets", "paper-api.", "broker-api", "live_trading", "mode = live")
FORBIDDEN_IMPORTS = (
    "app.data_engine.providers",
    "httpx",
    "requests",
    "websockets",
    "aiohttp",
    "urllib",
    "socket",
    "alpaca",
)


def _imports(package: str) -> dict[Path, set[str]]:
    out: dict[Path, set[str]] = {}
    for path in (APP / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        out[path] = names
    return out


@pytest.mark.parametrize("package", ISOLATED)
def test_execution_packages_cannot_reach_a_network_or_provider(package):
    offenders = {
        str(p.relative_to(APP)): sorted(n for n in names if n.startswith(FORBIDDEN_IMPORTS))
        for p, names in _imports(package).items()
    }
    assert {k: v for k, v in offenders.items() if v} == {}


@pytest.mark.parametrize("package", ISOLATED)
def test_execution_sources_name_no_brokerage_endpoint(package):
    for path in (APP / package).rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in ENDPOINT_WORDS:
            assert word not in text, f"{path.name} mentions {word!r}"


@pytest.mark.parametrize("package", NO_ORDER_ACCESS)
def test_strategies_and_ai_cannot_import_order_submission(package):
    offenders = {
        str(p.relative_to(APP)): sorted(
            n for n in names if n.startswith(("app.execution", "app.paper_broker", "app.events"))
        )
        for p, names in _imports(package).items()
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_setting_selects_a_broker_or_a_live_mode():
    for path in (APP / "core" / "config").rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "broker" not in text, path.name
        assert "execution_mode" not in text, path.name


@pytest.mark.parametrize(
    "actor",
    [
        "system:execution-coordinator",
        "system:execution",
        "system:execution-engine",
        "operator:jacob",
        "operator:risk.desk",
    ],
)
def test_the_coordinator_and_operators_may_submit(actor):
    assert authorize_submitter(actor) == []


@pytest.mark.parametrize(
    "actor",
    [
        "",
        "strategy:momentum",
        "system:strategy-worker",
        "system:ai-decision-engine",
        "system:execution-strategy",
        "system:execution-llm",
        "system:execution-ai",
        "llm:claude",
        "ai:coordinator",
        "ml:model",
        "system:risk-reservations",
        "test",
        "operator:",
        "operator:bad name",
    ],
)
def test_strategies_llms_and_unknown_actors_are_refused(actor):
    reasons = authorize_submitter(actor)
    assert [r["code"] for r in reasons] == ["UNAUTHORIZED_SUBMITTER"]


def test_fills_post_through_the_existing_6a_templates_only():
    assert TEMPLATES == {
        OrderSide.BUY: "long_purchase",
        OrderSide.SELL: "long_sale",
        OrderSide.SELL_SHORT: "short_open",
        OrderSide.BUY_TO_COVER: "short_cover",
    }
    assert MOVEMENT_SIGN == {
        OrderSide.BUY: 1,
        OrderSide.SELL: -1,
        OrderSide.SELL_SHORT: -1,
        OrderSide.BUY_TO_COVER: 1,
    }
    # Phase 7B: the 6A source follows the fill's broker source; there is no other mapping
    # and no setting that chooses one.
    assert SOURCES == {
        BrokerEventSource.CONTROLLED_TEST: EntrySource.CONTROLLED_TEST,
        BrokerEventSource.SIMULATION: EntrySource.SIMULATED_EXECUTION,
    }
    ledger = AccountingPositionLedger(posting=None)  # type: ignore[arg-type]
    fill = SimpleNamespace(source=BrokerEventSource.SYSTEM)
    with pytest.raises(ValueError, match="not a fill"):
        ledger.post_fill(None, fill, actor="system:execution-test")  # type: ignore[arg-type]


def _req(order_type: OrderType, **fields: object) -> OrderRequest:
    import uuid

    return OrderRequest(
        account_id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        order_type=order_type,
        quantity=D(10),
        **fields,
    )


@pytest.mark.parametrize(
    ("order_type", "fields", "ok"),
    [
        (OrderType.MARKET, {}, True),
        (OrderType.MARKET, {"limit_price": D(1)}, False),
        (OrderType.LIMIT, {"limit_price": D(1)}, True),
        (OrderType.LIMIT, {}, False),
        (OrderType.LIMIT, {"limit_price": D(1), "stop_price": D(1)}, False),
        (OrderType.STOP, {"stop_price": D(1)}, True),
        (OrderType.STOP, {"limit_price": D(1)}, False),
        (OrderType.STOP_LIMIT, {"stop_price": D(1), "limit_price": D(1)}, True),
        (OrderType.STOP_LIMIT, {"stop_price": D(1)}, False),
        (OrderType.TAKE_PROFIT, {"limit_price": D(1)}, True),
        (OrderType.TAKE_PROFIT, {}, False),
        (OrderType.TRAILING_STOP, {"trail_amount": D(1)}, True),
        (OrderType.TRAILING_STOP, {"trail_percent": D(2)}, True),
        (OrderType.TRAILING_STOP, {}, False),
        (OrderType.TRAILING_STOP, {"trail_amount": D(1), "trail_percent": D(2)}, False),
        (OrderType.TRAILING_STOP, {"trail_percent": D(100)}, False),
        (OrderType.LIMIT, {"limit_price": D(0)}, False),
        (OrderType.MARKET, {"trail_amount": D(1)}, False),
    ],
)
def test_price_fields_per_order_type(order_type, fields, ok):
    out: list[dict[str, object]] = []
    _price_fields(_req(order_type, **fields), out)
    assert (out == []) is ok, out


def _order(**kw: object) -> SimpleNamespace:
    base = {
        "status": OrderStatus.OPEN,
        "remaining_quantity": D(50),
        "quantity_increment": D(1),
        "price_increment": D("0.01"),
        "limit_price": None,
        "protection_price": D("99.70"),
        "side": OrderSide.BUY,
    }
    return SimpleNamespace(**(base | kw))


@pytest.mark.parametrize(
    ("order", "quantity", "price", "codes"),
    [
        (_order(), D(10), D("99.70"), set()),
        (_order(), D(10), D("99.71"), {"PROTECTION_VIOLATED"}),
        (_order(), D(51), D("99.00"), {"OVER_FILL"}),
        (_order(), D("1.5"), D("99.00"), {"QUANTITY_PRECISION"}),
        (_order(), D(1), D("99.005"), {"PRICE_PRECISION"}),
        (_order(limit_price=D("99.00")), D(1), D("99.01"), {"LIMIT_VIOLATED"}),
        (_order(status=OrderStatus.FILLED), D(1), D("99.00"), {"NOT_FILLABLE"}),
        (_order(status=OrderStatus.APPROVED), D(1), D("99.00"), {"NOT_FILLABLE"}),
        (
            _order(side=OrderSide.SELL_SHORT, protection_price=D("99.70")),
            D(1),
            D("99.69"),
            {"PROTECTION_VIOLATED"},
        ),
        (
            _order(side=OrderSide.SELL, protection_price=None, limit_price=D("105")),
            D(1),
            D("104.99"),
            {"LIMIT_VIOLATED"},
        ),
        (_order(side=OrderSide.SELL, protection_price=None), D(1), D("10"), set()),
    ],
)
def test_fills_must_respect_the_order(order, quantity, price, codes):
    assert {r["code"] for r in check_fill(order, quantity, price)} == codes
