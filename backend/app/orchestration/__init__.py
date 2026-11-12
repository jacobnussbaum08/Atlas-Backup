"""Autonomous paper-trading orchestration (Phase 10A).

Connects existing components into one automated, paper-only lifecycle. It adds no trading
logic: strategies propose, the AI coordinator prioritizes, the deterministic risk engine
decides and reserves, the execution engine places simulated paper orders, the paper broker
simulates fills, the position monitor manages exits and the ledger records everything. The
orchestrator only sequences those steps, durably and idempotently, under operator controls
that default to DISABLED.
"""
