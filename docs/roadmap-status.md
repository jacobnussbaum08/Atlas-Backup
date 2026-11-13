# Roadmap status (running record)

The running record the remaining-roadmap prompt asks for ([`prompts/remaining-roadmap.md`](prompts/remaining-roadmap.md)):
completed phases, commits, tests and outstanding issues. Updated at every phase checkpoint.
The specification ([`../ATLAS_MASTER_SPEC_V2.md`](../ATLAS_MASTER_SPEC_V2.md) §27) is the source
of truth for the phases.

## Roadmap inventory (2026-11-12)

| Spec phase | Delivered as | Commit | Status |
|---|---|---|---|
| 1 Foundation | Phase 1 | `d7801bf`, `14ff1b8` | complete |
| 2 Market data | 2A, 2B–2D, 2E | `dc4616f`, `ae87599`, `b727bcb` | complete |
| 3 Asset discovery and market intelligence | 3A, 3B, 3C (+ audit fixes `25ab296`) | `43d7cce`, `de2af86`, `ebef9bd` | complete |
| 4 Strategy laboratory | 4A, 4B | `2b86df6`, `ee52feb` | complete |
| 5 AI and ML engine | 5A–5B | `abf1bca` | complete |
| 6 Portfolio, accounting and risk | 6A, 6B, 6C | `3e95e37`, `e8e786b`, `0023e5e` | complete |
| 7 Paper execution | 7A–7B | `30b9749` | complete |
| 8 Backtesting and learning | 8A, 8B | `9c4e74f`, `dd8b1c1` | complete |
| 9 Complete dashboard | Phase 9 | `cce91a7` | complete |
| 10 Autonomous paper trading | 10A, 10B | `b38b65e`, `b5fd893` | complete (10B verified below) |
| **11 Validation and cloud readiness** | **11A** extended paper-trading validation (prompt: [`prompts/phase-11a-extended-paper-validation.md`](prompts/phase-11a-extended-paper-validation.md)) | — | **in progress** |
| | **11B** cloud readiness (spec §26.2; no prompt yet) | — | not started; depends on 11A evidence |
| — | Final deliverable (completion matrix, audit, deployment and rollback, verdict) | — | after 11B |

**Remaining phases: Phase 11 only.** The user's prompts split it into 11A (evaluate: paper
performance, risk adherence, reconciliation, data quality, execution realism, strategy
robustness, model performance, recovery reliability) and 11B (prepare cloud deployment "only
when justified", spec §27 and §26.2: "cloud deployment only after local development and paper
trading establish reliability").

**Dependencies.** 11A needs 10B (verified). 11B needs 11A's evidence to justify it; the 11A
prompt forbids starting cloud deployment within 11A. The final deliverable needs 11B.

## Preflight before 11A (2026-11-12)

| Check | Result |
|---|---|
| Branch / tree | `main` at `b5fd893` (Phases 1–10B), clean apart from the untracked `.claude/` launch configs. Not pushed. One old stash (`stash@{0}`, a Dockerfile edit identical to `main`) left alone. |
| Backend full suite on `b5fd893` (PostgreSQL) | **3,640 passed, 1 skipped** (Redis; `ATLAS_TEST_REDIS_URL` unset), **1 failed**: `test_expected_migration_heads_come_from_the_code_base`, caused by this session's new migration file appearing on disk mid-run (it compares the migration directory with the pre-edit expectation). Not a 10B defect. 40 min. |
| Frontend | typecheck clean; **163 passed** (a first run under heavy parallel load failed; the rerun passed) |
| `ruff check` / `ruff format --check` | clean |
| `mypy` | 76 errors in 5 pre-existing test files (unchanged baseline) |
| Open P0/P1 (financial integrity, execution safety, risk) | **none** in the 6C–10B reports or found here |

**Issues found in preflight (all P2 or data, fixed in 11A):**

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]
4. Strategy approval needed only 20 forward shadow sessions, whatever happened in them (D-224).

## Phase log

| Phase | Started | Commit | Tests | Outstanding |
|---|---|---|---|---|
| 11A | 2026-11-12 | (pending) | see the 11A report | live paper observation not started (needs operator approval); see the report |

## Final system audit (2026-11-12 / 13)

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]

## Phase 11A closeout (2026-11-13)

[REDACTED FOR GITHUB: provider-derived or research-result text; kept in the offline backup]
