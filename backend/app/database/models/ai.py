"""AI decision engine and LLM storage (Phase 5B).

Decisions:

* ``decision_policies`` / ``decision_policy_versions``: scoring weights, thresholds and
  rules, content-addressed and numbered; a run names the version it used.
* ``decision_runs``: one coordinator evaluation at one decision time. Its outcome is
  written once (RUNNING -> COMPLETED/FAILED, enforced by a trigger).
* ``decision_inputs``: every point-in-time input the run read, with its exact payload and
  hash. A run is replayed from these rows alone.
* ``ai_decisions``: the standardized, immutable decision records: one per evaluated
  opportunity (asset and direction), or one NO_OPPORTUNITY record for the run.
* ``decision_evidence``, ``decision_scoring_components``, ``decision_conflicts``: why.
* ``decision_evaluations``: comparisons of past decisions with simple baselines.

LLM:

* ``llm_requests``: one call (or refused call) to an external LLM, outcome written once.
* ``llm_responses``: what came back, its validation against the strict schema.
* ``llm_token_usage`` / ``llm_cost_records``: tokens reported by the provider, and the
  cost computed from the configured prices (an estimate, labelled with its source).
* ``llm_analyses``: validated LLM outputs stored as immutable feature records with the
  time they became available. Decisions read them only after that time.

Nothing here holds a quantity, an order or an approval: a prioritized decision is a
candidate for the future risk engine (Phase 6), never an authorization. Every table except
``decision_runs`` and ``llm_requests`` is append-only (triggers, migration 0012).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.ai_engine.enums import (
    ComponentKind,
    ComponentStatus,
    ConflictType,
    DecisionMode,
    DecisionOutcome,
    DecisionRunStatus,
    EvaluationVerdict,
    EvidenceStance,
    InputKind,
    LLMRequestStatus,
    LLMSubjectKind,
    LLMTask,
    RunOutcome,
    ValidationResult,
)
from app.database.models.base import Base, UUIDPrimaryKeyMixin, str_enum
from app.market_intelligence.enums import KnowledgeBasis
from app.strategies.enums import Direction

CODE = "^[a-z][a-z0-9_.-]{2,63}$"
NULLABLE_JSON = JSONB(none_as_null=True)
# Analytical values (scores, components): NUMERIC, read back as floats (as D-111).
REAL_NUMBER = Numeric(asdecimal=False)
USD = Numeric(16, 8)
_RUN_FK = "decision_runs.id"
_DECISION_FK = "ai_decisions.id"
_POLICY_VERSION_FK = "decision_policy_versions.id"
_REQUEST_FK = "llm_requests.id"


class DecisionPolicyRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "decision_policies"
    __table_args__ = (
        UniqueConstraint("code"),
        CheckConstraint(f"code ~ '{CODE}'", name="code_format"),
    )

    code: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DecisionPolicyVersionRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "decision_policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version"),
        UniqueConstraint("policy_id", "parameters_sha256"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("decision_policies.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    # Every weight, threshold and rule, explicit (decimals as numbers), and its hash.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parameters_sha256: Mapped[str] = mapped_column(String(64))
    logic_version: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DecisionRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "decision_runs"
    __table_args__ = (
        # One non-failed run per policy version, mode, basis and decision time.
        Index(
            "uq_decision_runs_live_key",
            "policy_version_id",
            "mode",
            "basis",
            "decision_at",
            unique=True,
            postgresql_where=text("status <> 'FAILED'"),
        ),
        Index(None, "decision_at"),
        Index(None, "status", "started_at"),
        CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name="finished_iff_done"),
        CheckConstraint("(status = 'COMPLETED') = (outcome IS NOT NULL)", name="outcome_iff_done"),
        CheckConstraint("(status = 'FAILED') = (error IS NOT NULL)", name="error_iff_failed"),
        CheckConstraint("knowledge_at >= decision_at", name="known_after_decision"),
        CheckConstraint(
            "basis <> 'RECORDED' OR knowledge_at = decision_at", name="recorded_known_at_decision"
        ),
        CheckConstraint("mode <> 'LIVE' OR basis = 'RECORDED'", name="live_is_recorded"),
    )

    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_POLICY_VERSION_FK, ondelete="RESTRICT")
    )
    mode: Mapped[DecisionMode] = mapped_column(str_enum(DecisionMode, "decision_mode"))
    basis: Mapped[KnowledgeBasis] = mapped_column(str_enum(KnowledgeBasis, "knowledge_basis"))
    trigger: Mapped[str] = mapped_column(String(64))
    # Only information available by decision_at (RECORDED) was read; RECONSTRUCTED runs
    # also read rows stored later about periods completed by then (labelled).
    decision_at: Mapped[datetime]
    knowledge_at: Mapped[datetime]
    status: Mapped[DecisionRunStatus] = mapped_column(
        str_enum(DecisionRunStatus, "decision_run_status")
    )
    outcome: Mapped[RunOutcome | None] = mapped_column(str_enum(RunOutcome, "decision_run_outcome"))
    proposals_considered: Mapped[int] = mapped_column(Integer, server_default="0")
    candidates_count: Mapped[int] = mapped_column(Integer, server_default="0")
    prioritized_count: Mapped[int] = mapped_column(Integer, server_default="0")
    # sha256 over every stored input, and over every decision's content.
    inputs_sha256: Mapped[str | None] = mapped_column(String(64))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    # Which inputs were available (ML, LLM, regimes, news), counts and notes.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    notes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    code_version: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class DecisionInputRecord(Base):
    __tablename__ = "decision_inputs"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "ref"),
        Index(None, "kind", "ref"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    kind: Mapped[InputKind] = mapped_column(str_enum(InputKind, "decision_input_kind"))
    # The source row's identifier (proposal id, regime classification id, ...).
    ref: Mapped[str] = mapped_column(String(160))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    as_of: Mapped[datetime | None]
    available_at: Mapped[datetime | None]
    # The exact values the coordinator used (copied, not referenced).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_sha256: Mapped[str] = mapped_column(String(64))


class AIDecisionRecord(UUIDPrimaryKeyMixin, Base):
    """The standardized decision record (Phase 5B prompt section 7). Immutable."""

    __tablename__ = "ai_decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "asset_id", "direction", postgresql_nulls_not_distinct=True),
        Index(None, "asset_id", "decision_at"),
        Index(None, "outcome", "decision_at"),
        CheckConstraint(
            "(outcome = 'NO_OPPORTUNITY') = (asset_id IS NULL)", name="no_opportunity_has_no_asset"
        ),
        CheckConstraint("(asset_id IS NULL) = (direction IS NULL)", name="asset_iff_direction"),
        CheckConstraint("rank IS NULL OR rank >= 1", name="rank_positive"),
        CheckConstraint(
            "rank IS NULL OR outcome IN ('PRIORITIZE_FOR_REVIEW', 'DEPRIORITIZE')",
            name="ranked_only_if_eligible",
        ),
        CheckConstraint("expires_at > decision_at", name="expires_after_decision"),
        CheckConstraint("data_cutoff <= decision_at", name="cutoff_before_decision"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    outcome: Mapped[DecisionOutcome] = mapped_column(
        str_enum(DecisionOutcome, "ai_decision_outcome")
    )
    rank: Mapped[int | None] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    symbol: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[Direction | None] = mapped_column(str_enum(Direction, "proposal_direction"))
    timeframes: Mapped[list[str]] = mapped_column(ARRAY(String(16)), server_default="{}")
    proposal_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default="{}"
    )
    strategy_versions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    model_versions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    regime_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    news_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    llm_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    feature_timestamps: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    policy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(_POLICY_VERSION_FK, ondelete="RESTRICT")
    )
    # Raw model score, calibrated probability (if any), heuristic and ranking scores, kept
    # apart and labelled. None of the heuristic scores is a probability.
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Potential reward and adverse move, costs, expected return/value where supported.
    expected: Mapped[dict[str, Any]] = mapped_column(JSONB)
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSONB)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    explanation: Mapped[str] = mapped_column(Text)
    decision_at: Mapped[datetime]
    data_cutoff: Mapped[datetime]
    expires_at: Mapped[datetime]
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DecisionEvidenceRecord(Base):
    __tablename__ = "decision_evidence"
    __table_args__ = (Index(None, "decision_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_DECISION_FK, ondelete="RESTRICT"))
    input_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("decision_inputs.id", ondelete="RESTRICT")
    )
    kind: Mapped[InputKind] = mapped_column(str_enum(InputKind, "decision_input_kind"))
    stance: Mapped[EvidenceStance] = mapped_column(
        str_enum(EvidenceStance, "decision_evidence_stance")
    )
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class DecisionScoringComponentRecord(Base):
    __tablename__ = "decision_scoring_components"
    __table_args__ = (
        UniqueConstraint("decision_id", "name"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score_range"),
        CheckConstraint("weight >= 0", name="weight_non_negative"),
        CheckConstraint("(status = 'AVAILABLE') = (score IS NOT NULL)", name="score_iff_available"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_DECISION_FK, ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(48))
    kind: Mapped[ComponentKind] = mapped_column(str_enum(ComponentKind, "decision_component_kind"))
    status: Mapped[ComponentStatus] = mapped_column(
        str_enum(ComponentStatus, "decision_component_status")
    )
    raw_value: Mapped[float | None] = mapped_column(REAL_NUMBER)
    score: Mapped[float | None] = mapped_column(REAL_NUMBER)
    weight: Mapped[float] = mapped_column(REAL_NUMBER)
    contribution: Mapped[float | None] = mapped_column(REAL_NUMBER)
    reason: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class DecisionConflictRecord(Base):
    __tablename__ = "decision_conflicts"
    __table_args__ = (Index(None, "run_id"), Index(None, "asset_id"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    type: Mapped[ConflictType] = mapped_column(str_enum(ConflictType, "decision_conflict_type"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    decision_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    proposal_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default="{}"
    )
    # How the coordinator handled it (it never forces a consensus).
    handling: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class DecisionEvaluationRecord(UUIDPrimaryKeyMixin, Base):
    """The coordinator's prioritized decisions against simple baselines, on matured
    outcomes known at ``known_at`` (realized net returns under a stated cost assumption)."""

    __tablename__ = "decision_evaluations"
    __table_args__ = (Index(None, "created_at"),)

    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(_POLICY_VERSION_FK, ondelete="RESTRICT")
    )
    known_at: Mapped[datetime]
    decision_start: Mapped[datetime | None]
    decision_end: Mapped[datetime | None]
    runs_evaluated: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    results: Mapped[dict[str, Any]] = mapped_column(JSONB)
    verdict: Mapped[EvaluationVerdict] = mapped_column(
        str_enum(EvaluationVerdict, "decision_evaluation_verdict")
    )
    notes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LLMRequestRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "llm_requests"
    __table_args__ = (
        Index(None, "cache_key"),
        Index(None, "status", "requested_at"),
        Index(None, "requested_at"),
        CheckConstraint("(status = 'RUNNING') = (finished_at IS NULL)", name="finished_iff_done"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("estimated_input_tokens >= 0", name="estimate_non_negative"),
        CheckConstraint(
            "reserved_cost_usd IS NULL OR reserved_cost_usd >= 0", name="reserve_non_negative"
        ),
    )

    task: Mapped[LLMTask] = mapped_column(str_enum(LLMTask, "llm_task"))
    subject_kind: Mapped[LLMSubjectKind] = mapped_column(
        str_enum(LLMSubjectKind, "llm_subject_kind")
    )
    subject_refs: Mapped[list[str]] = mapped_column(JSONB)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(48))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(48))
    schema_sha256: Mapped[str] = mapped_column(String(64))
    # sha256(task, prompt, schema, provider, model, input): identical requests are answered
    # from the stored result instead of being sent again.
    cache_key: Mapped[str] = mapped_column(String(64))
    input_sha256: Mapped[str] = mapped_column(String(64))
    # The structured input as sent (untrusted article text included, as data).
    structured_input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    estimated_input_tokens: Mapped[int] = mapped_column(Integer)
    max_output_tokens: Mapped[int] = mapped_column(Integer)
    # The worst-case cost held against the budget while the request runs.
    reserved_cost_usd: Mapped[Decimal | None] = mapped_column(USD)
    status: Mapped[LLMRequestStatus] = mapped_column(
        str_enum(LLMRequestStatus, "llm_request_status")
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class LLMResponseRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "llm_responses"
    __table_args__ = (
        UniqueConstraint("request_id", "attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("(validation = 'VALID') = (parsed IS NOT NULL)", name="parsed_iff_valid"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_REQUEST_FK, ondelete="RESTRICT"))
    attempt: Mapped[int] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    # The model identifier the provider reported (may carry a dated snapshot).
    provider_model: Mapped[str | None] = mapped_column(String(128))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    # The structured output as received (bounded), before validation.
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    parsed: Mapped[dict[str, Any] | None] = mapped_column(NULLABLE_JSON)
    validation: Mapped[ValidationResult] = mapped_column(
        str_enum(ValidationResult, "llm_validation_result")
    )
    validation_errors: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    latency_ms: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime]


class LLMTokenUsageRecord(Base):
    __tablename__ = "llm_token_usage"
    __table_args__ = (
        Index(None, "recorded_at"),
        Index(None, "request_id"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0",
            name="tokens_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_REQUEST_FK, ondelete="RESTRICT"))
    response_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("llm_responses.id", ondelete="RESTRICT")
    )
    task: Mapped[LLMTask] = mapped_column(str_enum(LLMTask, "llm_task"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    recorded_at: Mapped[datetime]


class LLMCostRecord(Base):
    __tablename__ = "llm_cost_records"
    __table_args__ = (
        UniqueConstraint("usage_id"),
        Index(None, "recorded_at"),
        CheckConstraint("cost_usd >= 0", name="cost_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_REQUEST_FK, ondelete="RESTRICT"))
    usage_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("llm_token_usage.id", ondelete="RESTRICT")
    )
    task: Mapped[LLMTask] = mapped_column(str_enum(LLMTask, "llm_task"))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    input_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    output_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    pricing_source: Mapped[str] = mapped_column(String(256))
    # Computed from the configured prices: an estimate, not the provider's invoice.
    cost_usd: Mapped[Decimal] = mapped_column(USD)
    recorded_at: Mapped[datetime]


class LLMAnalysisRecord(UUIDPrimaryKeyMixin, Base):
    """A validated LLM output stored as an immutable feature record (spec 12.4).

    ``available_at`` is when it was stored; decisions read it only from then on. A
    ``usable`` false record (suspected prompt injection, dropped out-of-scope content) is
    kept for audit but never used.
    """

    __tablename__ = "llm_analyses"
    __table_args__ = (
        UniqueConstraint(
            "task",
            "subject_kind",
            "subject_ref",
            "content_sha256",
            "prompt_version",
            "provider",
            "model",
            name="uq_llm_analyses_subject",
        ),
        Index(None, "subject_kind", "subject_ref"),
        Index(None, "available_at"),
        Index("ix_llm_analyses_asset_ids", "asset_ids", postgresql_using="gin"),
    )

    task: Mapped[LLMTask] = mapped_column(str_enum(LLMTask, "llm_task"))
    subject_kind: Mapped[LLMSubjectKind] = mapped_column(
        str_enum(LLMSubjectKind, "llm_subject_kind")
    )
    subject_ref: Mapped[str] = mapped_column(String(160))
    # The article or decision the analysis is about (when there is one).
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("news_articles.id", ondelete="RESTRICT")
    )
    revision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("news_article_revisions.id", ondelete="RESTRICT")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(_RUN_FK, ondelete="RESTRICT"))
    # Assets the analysis concerns: only assets Atlas had mapped (never ones the LLM named).
    asset_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default="{}"
    )
    # sha256 of the subject's content as analysed (dedup: unchanged content is never resent).
    content_sha256: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(_REQUEST_FK, ondelete="RESTRICT"))
    response_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("llm_responses.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(48))
    schema_version: Mapped[str] = mapped_column(String(48))
    analysis: Mapped[dict[str, Any]] = mapped_column(JSONB)
    flags: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    usable: Mapped[bool] = mapped_column(Boolean)
    # The subject's own time (article publication) and when the analysis became usable.
    subject_time: Mapped[datetime | None]
    available_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


__all__ = [
    "AIDecisionRecord",
    "DecisionConflictRecord",
    "DecisionEvaluationRecord",
    "DecisionEvidenceRecord",
    "DecisionInputRecord",
    "DecisionPolicyRecord",
    "DecisionPolicyVersionRecord",
    "DecisionRunRecord",
    "DecisionScoringComponentRecord",
    "LLMAnalysisRecord",
    "LLMCostRecord",
    "LLMRequestRecord",
    "LLMResponseRecord",
    "LLMTokenUsageRecord",
]
