"""Atomic risk reservations (spec 13, 14.3; Phase 6C).

Reserved capacity for approved trade intents, recorded in PostgreSQL under the portfolio
account row lock. ``exposure`` turns active reservations into the risk view; ``service``
reserves, consumes, releases and expires them; ``reconciliation`` checks them against
their events and the ledger. Nothing here submits, fills or executes anything.
"""
