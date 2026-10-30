# config/

Versioned, non-secret configuration files that are reviewed like code.

Nothing lives here yet in Phase 1. Planned contents:

* Initial hard risk-limit configuration (Phase 6). Loaded into the
  `risk_configurations` table as a versioned, audited record. Runtime changes go
  through the database and audit log, never by editing a file in place
  (spec §14.7).
* Provider and market-session reference data (Phases 2–3).

Secrets never belong here. They come from the environment (`.env`, see
`.env.example`).
