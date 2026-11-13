Atlas AI Trader — Complete Remaining Roadmap
Read `ATLAS_MASTER_SPEC_V2.md` in its entirety, along with all phase completion reports and the current repository state.
Phase 10B must be completed and verified before beginning.
Objective
Complete every remaining development phase documented in the master specification, in the exact intended order, through final validation and deployment-readiness review.
Execution rules

* Treat the master specification as the source of truth.
* Identify every remaining phase and its dependencies before starting.
* Work sequentially. Never skip phases or silently combine their requirements.
* Complete, test, and document each phase before proceeding to the next.
* Preserve existing architecture and working functionality unless a justified change is necessary.
* Do not duplicate already completed work.
* Maintain a running record of completed phases, commits, tests, and outstanding issues.

Mandatory checkpoint after every phase
Before proceeding:

1. Run relevant tests and the full regression suites.
2. Verify the phase's actual acceptance criteria.
3. Review financial integrity, risk controls, execution safety, and security implications.
4. Record files changed, migrations, configuration changes, and test results.
5. Produce a concise completion report.
6. Commit the completed phase separately when the repository is in a safe state.

If a phase fails validation, fix it before proceeding.
Stop conditions
Stop and request my review if:

* A P0/P1 issue affects financial integrity, risk, execution, or security.
* Requirements conflict or are ambiguous.
* A required external service, credential, or data source is unavailable.
* A migration or architectural change risks existing data.
* A phase cannot meet its acceptance criteria.
* Live trading or real-money execution would be required.

Do not conceal limitations or mark incomplete work as complete.
Permanent safety restrictions

* No real-money trading.
* No live brokerage orders.
* Never enable live execution.
* Never automatically increase risk limits.
* Never bypass risk checks.
* Never fabricate test results, market data, or performance.
* Keep paper trading isolated from live accounts.
* Require explicit human approval for consequential operational changes.

Final deliverable
When all remaining phases are complete, produce:

* Full phase-by-phase completion matrix.
* Final architecture overview.
* Full backend/frontend test results.
* Security and financial-integrity audit.
* Historical and paper-trading performance evidence.
* Remaining limitations and unresolved risks.
* Deployment instructions and rollback procedure.
* Explicit verdict: ready for further paper trading, ready for cloud deployment, or not ready.

Do not declare Atlas ready for live trading under any circumstances.
Begin with a roadmap inventory and preflight. Then execute the first remaining phase only.
