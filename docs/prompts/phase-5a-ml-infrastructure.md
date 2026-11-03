ATLAS AI TRADER — PHASE 5A
MACHINE LEARNING INFRASTRUCTURE & PREDICTIVE MODELING
Read `ATLAS_MASTER_SPEC_V2.md` and inspect the entire existing codebase.
First verify Phases 1–4, including the Strategy Laboratory, six strategies, database migrations, and tests. Review the latest code audit. Stop if unresolved P0/P1 issues or critical broken dependencies exist.
Preserve all existing functionality.
OBJECTIVE
Build Atlas's local machine-learning infrastructure to train, evaluate, version, and produce predictions from quantitative market data.
Initially evaluate:

* Logistic Regression.
* Random Forest.
* XGBoost.

Use scikit-learn, XGBoost, Polars, and NumPy where appropriate.
Do not introduce complex neural networks without evidence that they improve performance.
1. DATASET ENGINE
Create a reproducible, point-in-time dataset pipeline using existing market features, historical data, strategy evaluations, and proposals.
Support:

* Asset-specific datasets.
* Multiple timeframes.
* Long and short observations.
* Strategy-specific observations.
* Market-regime features.
* Volatility features.
* Relevant event features where available.

Every observation must preserve its decision timestamp, feature availability timestamps, asset identity, feature version, strategy version, and data provenance.
Only use information available at the historical decision time.
2. TARGETS AND LABELING
Implement configurable target-generation methods for predicting future market outcomes.
Evaluate targets such as:

* Forward returns over defined horizons.
* Directional price movement.
* Volatility-adjusted outcomes.
* Barrier-based outcomes where appropriate.

Account for:

* Transaction costs where available.
* Long versus short direction.
* Strategy holding horizon.
* Overlapping observations.
* Label maturity.

Document every target definition.
Do not assume that predicting price direction automatically predicts profitability.
Never include future information in model input features.
3. FEATURE PROCESSING
Build reusable feature preparation.
Support:

* Missing-value handling.
* Invalid-observation rejection.
* Scaling where appropriate.
* Categorical encoding where appropriate.
* Feature selection.
* Feature versioning.

Fit all preprocessing transformations exclusively on training data.
Prevent data leakage through normalization, feature selection, or imputation.
Preserve the original feature definitions.
4. MODEL TRAINING
Implement independent training pipelines for:

* Logistic Regression baseline.
* Random Forest.
* XGBoost.

Support configurable hyperparameters, training windows, target definitions, timeframes, and asset groups.
Begin with simple models and compare them objectively.
Every training run must record:

* Dataset version.
* Feature definitions.
* Target definition.
* Training period.
* Validation period.
* Test period.
* Model parameters.
* Code version.
* Random seed where applicable.
* Training metrics.
* Evaluation metrics.

Training must be reproducible as far as the underlying data and libraries permit.
5. VALIDATION AND LEAKAGE PREVENTION
Use chronological walk-forward validation as the primary evaluation method.
Support purged time-series validation and CPCV as supplementary robustness tests where appropriate.
Implement:

* Chronological training, validation, and testing.
* Embargo periods based on label overlap and prediction horizons.
* An untouched final test period.
* Model comparisons against simple baselines.
* Evaluation across different market regimes.

Prevent:

* Look-ahead bias.
* Survivorship bias.
* Target leakage.
* Overlapping-label contamination.
* Train/test contamination.
* Hyperparameter overfitting.

Never randomly shuffle financial time-series observations into conventional train/test splits.
6. MODEL EVALUATION
Track appropriate classification and regression metrics.
Classification:

* Precision.
* Recall.
* F1.
* ROC-AUC where meaningful.
* Precision-recall metrics.
* Confusion matrices.
* Probability calibration.

Regression:

* MAE.
* RMSE.
* Directional accuracy where meaningful.
* Error distributions.

Financial evaluation:

* Net strategy outcomes.
* Expected value.
* Transaction costs.
* Risk-adjusted performance where applicable.

Evaluate separately by strategy, asset, timeframe, long/short direction, and market regime.
Do not judge models using accuracy alone.
Do not describe uncalibrated model scores as probabilities.
7. PROBABILITY CALIBRATION
Where models produce predicted probabilities, evaluate calibration using appropriate methods such as:

* Reliability diagrams.
* Brier score.
* Calibration curves.
* Out-of-sample calibration.

Fit calibration using validation data only.
Do not calibrate on the final untouched test set.
Only expose probabilities when calibration evidence supports doing so.
Otherwise expose raw scores with clear labels.
8. MODEL REGISTRY
Create an immutable model registry.
Record:

* Model ID.
* Model type.
* Version.
* Training run.
* Dataset version.
* Feature schema.
* Target definition.
* Training parameters.
* Evaluation results.
* Artifact location.
* Creation timestamp.
* Validation status.
* Model lifecycle status.

Support:

* Candidate.
* Validated.
* Shadow.
* Approved.
* Rejected.
* Retired.

Do not automatically promote models.
Preserve previous model versions.
9. INFERENCE ENGINE
Build an inference interface that future Atlas components can call.
It should:

* Load approved model artifacts.
* Validate incoming feature schemas.
* Reject missing or incompatible features.
* Generate predictions.
* Record model version.
* Record feature snapshot.
* Record prediction timestamp.
* Record model output and validity status.

Inference must be reproducible against stored inputs and model artifacts.
Do not permit model inference to submit orders or change risk settings.
10. DATABASE AND API
Reuse existing architecture and add necessary migrations for:

* Training datasets.
* Dataset versions.
* Training runs.
* Model artifacts.
* Model evaluations.
* Model predictions.
* Calibration results.
* Model registry states.

Expose APIs for:

* Model registry.
* Training-run history.
* Evaluation results.
* Candidate comparisons.
* Prediction history.
* Model status.

Add a basic Machine Learning page to the dashboard showing actual model and training information.
Do not fabricate performance metrics or predictions.
11. TESTING
Test:

* Dataset reproducibility.
* Point-in-time feature availability.
* Label generation.
* Train/test separation.
* Preprocessing leakage.
* Model training.
* Model artifact loading.
* Prediction reproducibility.
* Probability calibration.
* Missing features.
* Invalid inputs.
* Model versioning.
* Registry lifecycle.
* Database persistence.
* API functionality.

Use deterministic fixtures and synthetic data only where explicitly labeled as test data.
Preserve all previous phase tests.
12. RESTRICTIONS
Implement Phase 5A only.
Do not implement:

* External LLM decision-making.
* News interpretation beyond existing structured features.
* Central AI trade coordination.
* Portfolio management.
* Position sizing.
* Risk approval.
* Order execution.
* Brokerage integration.

No model may independently authorize a trade.
Candidate models must not automatically replace approved models.
13. COMPLETION REPORT
Report:

1. Dataset architecture.
2. Target-generation methodology.
3. Models implemented.
4. Validation methodology.
5. Leakage prevention.
6. Calibration results.
7. Model registry.
8. Inference functionality.
9. Database and API changes.
10. Dashboard additions.
11. Files created and modified.
12. Test results.
13. Actual training results, clearly distinguishing test fixtures from real historical data.
14. Known limitations.
15. Readiness for Phase 5B.

Implement incrementally. Preserve existing functionality. Do not claim predictive value without out-of-sample evidence.
FINAL INSTRUCTION: Implement Phase 5A only. Build a reproducible, statistically disciplined ML foundation for Atlas, without introducing autonomous trade execution.
