"""Event-driven historical backtesting with look-ahead prevention (spec 18, Phase 8A).

The backtester replays stored historical observations in chronological order through the
same code paths paper trading uses: the strategy evaluation step
(:func:`app.strategies.engine.decide`), the 6B pure risk assessment, 6C reservation
amounts, the 7B fill model and protection rules, and the 6A posting templates on an
in-memory ledger. Nothing here reads a live stream, contacts a provider or a broker, or
writes to a live trading table: results go to the ``backtest_*`` tables only.
See ``docs/architecture/backtesting.md``.
"""
