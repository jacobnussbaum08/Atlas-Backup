"""The Phase 11A paper harness starts every worker paper trading depends on (D-233)."""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "paper_observation.sh"


def workers() -> list[str]:
    match = re.search(r"^WORKERS=\(([^)]*)\)", SCRIPT.read_text(encoding="utf-8"), re.M)
    assert match is not None
    return match.group(1).split()


def test_the_paper_harness_runs_the_asset_discovery_worker() -> None:
    """Without it the liquidity measurements expire and the risk engine refuses every
    trade as INSUFFICIENT_DATA (observed 2026-11-13: last refresh 2026-11-02)."""
    assert "asset_discovery" in workers()
    assert "export ASSET_DISCOVERY_ENABLED=true" in SCRIPT.read_text(encoding="utf-8")


def test_the_paper_harness_runs_the_trading_workers() -> None:
    for worker in (
        "market_data_stream",
        "market_data_recovery",
        "paper_execution",
        "position_monitor",
        "trading_orchestrator",
    ):
        assert worker in workers()
