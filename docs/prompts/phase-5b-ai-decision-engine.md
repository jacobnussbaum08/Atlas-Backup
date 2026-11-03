ATLAS AI TRADER — PHASE 5B
CENTRAL AI DECISION ENGINE & LLM INTEGRATION
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify Phases 1–5A, including all six strategies, Market Intelligence, News Intelligence, ML training, model validation, and the model registry.
Run existing tests and review the latest audit.
Stop if unresolved P0/P1 problems or critical dependencies exist. Preserve all working functionality.
OBJECTIVE
Build Atlas's central AI Decision Engine.
It must coordinate strategy proposals, quantitative ML predictions, market regimes, cross-asset information, and structured news features to evaluate and rank potential trading opportunities.
Use a hybrid architecture:

* Deterministic quantitative calculations.
* Validated ML predictions.
* Structured external LLM analysis.
* A reproducible decision coordinator.

No single LLM should control trading decisions.
1. DECISION COORDINATOR
Create a central coordinator that receives proposals from all six strategy modules:

* Momentum.
* Trend following.
* Mean reversion.
* Breakout.
* Volatility.
* Event driven.

Combine them with:

* ML predictions.
* Calibrated probabilities where validated.
* Market regime.
* Volatility conditions.
* Cross-asset relationships.
* News and event features.
* Data quality.
* Expected transaction costs.

Evaluate each proposal independently and in relation to competing proposals.
The coordinator must recognize:

* Agreement between strategies.
* Conflicting long and short proposals.
* Duplicate opportunities.
* Highly correlated opportunities.
* Insufficient evidence.
* Poor expected reward relative to risk.
* Unfavorable market conditions.

It must be allowed to recommend no opportunity.
2. QUANTITATIVE SCORING
Build an explainable, configurable decision-scoring framework.
Evaluate:

* Signal quality.
* Historical strategy performance.
* ML predictions.
* Probability calibration.
* Market regime suitability.
* Momentum and trend conditions.
* Volatility.
* Liquidity.
* Expected transaction costs.
* News relevance.
* Data reliability.

Separate raw model scores, calibrated probabilities, heuristic scores, and final ranking scores.
Never present an arbitrary weighted score as a probability of profit.
Version all scoring weights and decision policies.
Initial weights must be configurable and documented, not described as optimal.
Compare the coordinator against simple baselines to establish whether combining the inputs actually provides incremental value.
3. EXPECTED VALUE AND TRADE QUALITY
Where sufficient validated information exists, estimate:

* Potential reward.
* Potential adverse movement.
* Transaction costs.
* Expected return.
* Expected value.
* Uncertainty.

Distinguish predicted price direction from expected trading profitability.
Account for spread, slippage, and costs where available.
Do not fabricate probability distributions or risk estimates.
Do not calculate authoritative position sizes.
If information is insufficient, defer or reject the proposal.
4. ML INTEGRATION
Connect the existing Phase 5A inference engine.
Use only approved and validated models for primary decision support.
Candidate and shadow models must not silently influence execution decisions.
Record:

* Model ID and version.
* Prediction.
* Feature snapshot.
* Calibration information.
* Timestamp.
* Model status.

Support model disagreement and uncertainty.
A prediction with poor calibration or degraded performance should receive reduced authority or be excluded according to documented rules.
5. LLM INTEGRATION
Implement an external LLM provider abstraction.
Support configurable providers, initially allowing Anthropic and OpenAI-compatible integrations where practical.
Use LLMs for:

* News classification.
* Event interpretation.
* Article summarization.
* Asset relevance.
* Catalyst identification.
* Structured research summaries.
* Explaining conflicts between existing analytical outputs.

LLM calls must run asynchronously and must not block the critical decision pipeline.
Use strict Pydantic schemas and structured outputs.
LLMs must never:

* Invent market prices.
* Calculate authoritative position sizes.
* Override risk limits.
* Authorize orders.
* Execute trades.
* Modify system configuration.
* Promote their own models.

Treat article content as untrusted input. Prevent prompt injection and instruction-following from external articles.
6. COST AND TOKEN MANAGEMENT
Keep initial AI API spending low.
Implement:

* Configurable model selection.
* Token and request usage tracking.
* Per-task cost estimation where pricing information is available.
* Article deduplication.
* Content-hash caching.
* Bounded requests and concurrency.
* Rate-limit handling.
* Retry limits.
* Batch processing where supported.
* Configurable spending limits.

Do not send unchanged articles to an LLM repeatedly.
Do not call an LLM for every market tick.
Use quantitative calculations for routine numerical work.
Never silently switch to an inferior model or fabricate an analysis when a provider is unavailable.
If AI analysis is unavailable, mark it unavailable and let deterministic components follow their documented fallback behavior.
Do not require paid API credentials for ordinary automated tests.
7. DECISION OUTPUT
Create a standardized, immutable decision record.
Include:

* Decision ID.
* Asset.
* Direction.
* Strategy proposals considered.
* Strategy versions.
* Model versions.
* Market regime snapshot.
* News feature references.
* Feature timestamps.
* Scoring-policy version.
* Expected-return estimates where supported.
* Uncertainty.
* Conflicts.
* Evidence.
* Rejection reasons.
* Decision timestamp.
* Data cutoff timestamp.
* Decision expiration.

Use outcomes such as:

* PRIORITIZE_FOR_REVIEW.
* DEPRIORITIZE.
* REJECT.
* DEFER.
* NO_OPPORTUNITY.
* INSUFFICIENT_DATA.

A prioritized decision is only a candidate for future risk review. It is not an approved trade.
8. REPRODUCIBILITY AND AUDITABILITY
Every decision must be reconstructable from stored evidence.
Preserve:

* Exact input references.
* Feature snapshots.
* Strategy proposals.
* Model predictions.
* LLM output references.
* Prompt version.
* Provider and model identifiers.
* Decision policy version.
* Scoring parameters.
* Data availability timestamps.

Do not rely on future API calls to reproduce historical LLM responses.
Replay stored outputs instead.
Historical decisions must only use information available at their original decision timestamps.
9. DECISION CONFLICTS
Implement explicit handling for:

* Different strategies recommending opposite directions.
* Multiple strategies proposing nearly identical trades.
* ML models disagreeing.
* Market intelligence conflicting with a strategy.
* News features contradicting price action.
* Correlated proposals competing for future portfolio capacity.

Do not force consensus.
Document the evidence and uncertainty.
Allow the coordinator to reject all proposals.
10. DATABASE AND API
Create or extend database models for:

* Decision policies.
* Policy versions.
* Decision runs.
* Decision inputs.
* Decision evidence.
* Scoring components.
* LLM requests.
* LLM responses.
* Token usage.
* Cost records.
* Final decision records.

Preserve immutable decision history.
Expose APIs for:

* Decision history.
* Decision details.
* Proposal rankings.
* Evidence.
* Model contributions.
* LLM analysis.
* Provider usage and cost.

11. DASHBOARD
Extend the Atlas interface with a Central AI Activity page.
Display:

* Evaluated proposals.
* Ranked opportunities.
* Accepted and rejected candidates.
* Reasons for rejection.
* Strategy agreement and disagreement.
* ML predictions.
* Market context.
* News analysis.
* Decision explanations.
* Model versions.
* AI API usage and estimated costs.

Clearly distinguish analytical prioritization from risk approval.
Do not add buy/sell execution controls.
Do not fabricate decision metrics.
12. TESTING
Test:

* Multi-strategy coordination.
* Conflicting proposals.
* Duplicate detection.
* Model disagreement.
* Probability calibration handling.
* Expected-value calculations.
* Transaction-cost adjustments.
* News-feature availability.
* LLM schema validation.
* Prompt-injection resistance.
* LLM timeout and outage behavior.
* Caching and deduplication.
* Token/cost limits.
* Decision reproducibility.
* Historical timestamp correctness.
* API and dashboard functionality.

Use mocked LLM responses in ordinary tests and clearly identify them as fixtures.
Use opt-in real-provider smoke tests where credentials permit.
Preserve all previous tests.
13. SCOPE RESTRICTIONS
Implement Phase 5B only.
Do not implement:

* Portfolio management.
* Position sizing.
* Risk reservations.
* Risk approval.
* Order execution.
* Paper brokerage.
* Live trading.

These belong to subsequent phases.
The decision coordinator cannot bypass the future deterministic risk engine.
14. COMPLETION REPORT
Report:

1. Decision architecture.
2. Strategy integration.
3. ML integration.
4. LLM provider integration.
5. Scoring methodology.
6. Conflict-resolution behavior.
7. Decision-record design.
8. Reproducibility safeguards.
9. Token and cost controls.
10. Database and API changes.
11. Dashboard additions.
12. Files created and modified.
13. Test results.
14. Actual LLM provider tests.
15. Known limitations.
16. Readiness for Phase 6.

Be honest about actual predictive performance. Do not claim that an AI-generated ranking represents a profitable trade.
Implement incrementally. Preserve existing systems and do not introduce unnecessary dependencies.
FINAL INSTRUCTION: Implement Phase 5B only. Build a reproducible, cost-conscious AI decision coordinator that evaluates opportunities without authorizing or executing trades.
