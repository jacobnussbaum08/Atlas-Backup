"""The LLM call service (Phase 5B): cached, budgeted, bounded, validated, recorded.

:meth:`LLMService.call` runs one task call:

1. **No provider** configured: UNAVAILABLE, nothing sent. Callers record the absence;
   deterministic components follow their documented fallback.
2. **Cache**: an earlier SUCCEEDED request with the same cache key (task, prompt hash,
   schema hash, provider, model, input hash) answers it from the stored, validated output.
   Unchanged content is never sent twice.
3. **Cool-down**: after ``AI_LLM_FAILURE_THRESHOLD`` consecutive provider failures the
   service stops calling for ``AI_LLM_COOLDOWN_SECONDS`` (UNAVAILABLE).
4. **Admission**: budget, request-limit and input-size checks under a lock, in the same
   transaction that records the RUNNING request with its worst-case reservation; a
   refused call is recorded with its reason (BUDGET_REFUSED / UNPRICED).
5. **Call**: under a concurrency semaphore, with a timeout, retrying only rate limits,
   timeouts and unavailability, at most ``AI_LLM_MAX_RETRIES`` times with bounded backoff
   (the provider's ``retry-after`` when given). Authentication and request errors are not
   retried. The configured model is the only one ever called.
6. **Validation**: the output must match the task's strict schema and the task's own
   checks; otherwise the response is stored INVALID and nothing is used (invalid outputs
   are not retried: they would cost again for a likely repeat).
7. **Records**: every response, its token usage and its computed cost.

A failure never produces a substitute analysis.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.ai_engine.config import AISettings
from app.ai_engine.enums import LLMRequestStatus, LLMSubjectKind, ValidationResult
from app.ai_engine.llm import budget
from app.ai_engine.llm.prompts import Prompt
from app.ai_engine.llm.providers import LLMProvider, ProviderError, ProviderRequest
from app.ai_engine.llm.safety import encode_untrusted
from app.ai_engine.llm.schemas import provider_schema
from app.core.clocks import Clock
from app.core.logging import get_logger
from app.core.security.redaction import redact_text
from app.database.models import (
    LLMCostRecord,
    LLMRequestRecord,
    LLMResponseRecord,
    LLMTokenUsageRecord,
)
from app.database.sessions import session_scope
from app.feature_engine.registry import sha256_json

logger = get_logger(__name__)

UNAVAILABLE = "UNAVAILABLE"
CACHED = "CACHED"
MAX_BACKOFF_SECONDS = 30.0
RAW_OUTPUT_LIMIT = 50_000


@dataclass(frozen=True)
class TaskCall:
    prompt: Prompt
    subject_kind: LLMSubjectKind
    subject_refs: list[str]
    # The structured input (stored with the request; untrusted text included as data).
    data: dict[str, Any]
    # Trusted framing of the data for the user message.
    intro: str
    # Semantic checks of a schema-valid answer (e.g. refs it may cite): a list of errors.
    check: Callable[[Any], list[str]] | None = None


@dataclass(frozen=True)
class CallOutcome:
    status: str  # an LLMRequestStatus value, CACHED or UNAVAILABLE
    parsed: Any | None = None
    request_id: uuid.UUID | None = None
    response_id: uuid.UUID | None = None
    reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.parsed is not None and self.status in (
            LLMRequestStatus.SUCCEEDED.value,
            CACHED,
        )


class LLMService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: AISettings,
        clock: Clock,
        provider: LLMProvider | None,
        *,
        worker_id: str = "llm",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions = session_factory
        self.settings = settings
        self.clock = clock
        self.provider = provider
        self.worker_id = worker_id
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        self._failures = 0
        self._cooldown_until: datetime | None = None

    @property
    def available(self) -> bool:
        return self.provider is not None

    def cooling_down(self) -> bool:
        return self._cooldown_until is not None and self.clock.now() < self._cooldown_until

    def keys(self, call: TaskCall) -> tuple[str, str]:
        assert self.provider is not None  # noqa: S101
        input_sha = sha256_json(call.data)
        cache_key = sha256_json(
            {
                "task": call.prompt.task.value,
                "prompt": call.prompt.sha256,
                "schema": call.prompt.schema_sha256,
                "provider": self.provider.name,
                "model": self.provider.model,
                "input": input_sha,
            }
        )
        return cache_key, input_sha

    # --- storage (synchronous; run in a thread) ------------------------------------------

    def _cached(self, cache_key: str, schema: type[BaseModel]) -> CallOutcome | None:
        with session_scope(self._sessions) as session:
            row = session.execute(
                select(LLMRequestRecord.id, LLMResponseRecord.id, LLMResponseRecord.parsed)
                .join(LLMResponseRecord, LLMResponseRecord.request_id == LLMRequestRecord.id)
                .where(
                    LLMRequestRecord.cache_key == cache_key,
                    LLMRequestRecord.status == LLMRequestStatus.SUCCEEDED,
                    LLMResponseRecord.validation == ValidationResult.VALID,
                )
                .order_by(LLMRequestRecord.requested_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        return CallOutcome(CACHED, schema.model_validate(row[2]), row[0], row[1])

    def _admit(
        self, call: TaskCall, cache_key: str, input_sha: str, user: str, actor: str
    ) -> tuple[uuid.UUID, LLMRequestStatus, str | None]:
        assert self.provider is not None  # noqa: S101
        now = self.clock.now()
        estimated = budget.estimate_tokens(call.prompt.system, user)
        with session_scope(self._sessions) as session:
            admission = budget.admit(session, self.settings, estimated_input=estimated, now=now)
            request_id = uuid.uuid4()
            final = admission.status is not LLMRequestStatus.RUNNING
            session.add(
                LLMRequestRecord(
                    id=request_id,
                    task=call.prompt.task,
                    subject_kind=call.subject_kind,
                    subject_refs=list(call.subject_refs),
                    provider=self.provider.name,
                    model=self.provider.model,
                    prompt_version=call.prompt.version,
                    prompt_sha256=call.prompt.sha256,
                    schema_version=call.prompt.schema_version,
                    schema_sha256=call.prompt.schema_sha256,
                    cache_key=cache_key,
                    input_sha256=input_sha,
                    structured_input=call.data,
                    estimated_input_tokens=estimated,
                    max_output_tokens=self.settings.llm_max_output_tokens,
                    reserved_cost_usd=admission.reserved_usd,
                    status=admission.status,
                    attempts=0,
                    error=admission.reason,
                    worker_id=self.worker_id,
                    created_by=actor,
                    requested_at=now,
                    finished_at=now if final else None,
                )
            )
        return request_id, admission.status, admission.reason

    def _record_response(
        self,
        request_id: uuid.UUID,
        attempt: int,
        call: TaskCall,
        response: Any,
        parsed: BaseModel | None,
        errors: list[str],
    ) -> uuid.UUID:
        assert self.provider is not None  # noqa: S101
        now = self.clock.now()
        response_id = uuid.uuid4()
        raw = response.output
        if raw is not None and len(str(raw)) > RAW_OUTPUT_LIMIT:
            raw = {"truncated": True, "size": len(str(raw))}
        with session_scope(self._sessions) as session:
            session.add(
                LLMResponseRecord(
                    id=response_id,
                    request_id=request_id,
                    attempt=attempt,
                    provider_request_id=response.provider_request_id,
                    provider_model=response.provider_model,
                    stop_reason=response.stop_reason,
                    raw_output=raw,
                    parsed=None if parsed is None else parsed.model_dump(mode="json"),
                    validation=ValidationResult.VALID
                    if parsed is not None
                    else ValidationResult.INVALID,
                    validation_errors=errors[:50],
                    latency_ms=response.latency_ms,
                    received_at=now,
                )
            )
            session.flush()
            usage = LLMTokenUsageRecord(
                request_id=request_id,
                response_id=response_id,
                task=call.prompt.task,
                provider=self.provider.name,
                model=self.provider.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_write_tokens=response.cache_write_tokens,
                recorded_at=now,
            )
            session.add(usage)
            session.flush()
            input_price = self.settings.llm_input_usd_per_mtok
            output_price = self.settings.llm_output_usd_per_mtok
            if input_price is not None and output_price is not None:
                session.add(
                    LLMCostRecord(
                        request_id=request_id,
                        usage_id=usage.id,
                        task=call.prompt.task,
                        provider=self.provider.name,
                        model=self.provider.model,
                        input_usd_per_mtok=input_price,
                        output_usd_per_mtok=output_price,
                        pricing_source=self.settings.llm_pricing_source or "operator-configured",
                        cost_usd=budget.cost_usd(
                            response.input_tokens + response.cache_write_tokens,
                            response.output_tokens,
                            input_price,
                            output_price,
                        ),
                        recorded_at=now,
                    )
                )
        return response_id

    def _finish(
        self, request_id: uuid.UUID, status: LLMRequestStatus, attempts: int, error: str | None
    ) -> None:
        with session_scope(self._sessions) as session:
            session.execute(
                update(LLMRequestRecord)
                .where(
                    LLMRequestRecord.id == request_id,
                    LLMRequestRecord.status == LLMRequestStatus.RUNNING,
                )
                .values(
                    status=status,
                    attempts=attempts,
                    error=None if error is None else redact_text(error)[:2000],
                    finished_at=self.clock.now(),
                )
            )

    def sweep_interrupted(self) -> int:
        """Mark RUNNING requests whose process died INTERRUPTED (releases reservations)."""
        cutoff = budget.interrupted_before(
            self.clock.now(), self.settings.llm_timeout_seconds, self.settings.llm_max_retries
        )
        with session_scope(self._sessions) as session:
            result = session.execute(
                update(LLMRequestRecord)
                .where(
                    LLMRequestRecord.status == LLMRequestStatus.RUNNING,
                    LLMRequestRecord.requested_at < cutoff,
                )
                .values(
                    status=LLMRequestStatus.INTERRUPTED,
                    error="the process stopped while the request was running",
                    finished_at=self.clock.now(),
                )
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # --- the call ------------------------------------------------------------------------

    def _validate(self, call: TaskCall, output: dict[str, Any] | None) -> tuple[Any, list[str]]:
        if output is None:
            return None, ["no structured output"]
        try:
            parsed = call.prompt.schema.model_validate(output)
        except ValidationError as exc:
            return None, [
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)
            ]
        errors = call.check(parsed) if call.check is not None else []
        return (None, errors) if errors else (parsed, [])

    async def call(self, call: TaskCall, *, actor: str = "llm-service") -> CallOutcome:
        if self.provider is None:
            return CallOutcome(
                UNAVAILABLE, reason="no LLM provider is configured (AI_LLM_PROVIDER)"
            )
        cache_key, input_sha = self.keys(call)
        cached = await asyncio.to_thread(self._cached, cache_key, call.prompt.schema)
        if cached is not None:
            return cached
        if self.cooling_down():
            return CallOutcome(
                UNAVAILABLE,
                reason=f"provider paused after {self._failures} consecutive failures until "
                f"{self._cooldown_until.isoformat() if self._cooldown_until else '?'}",
            )
        user = call.prompt.user_message(call.intro, encode_untrusted(call.data))
        request_id, status, reason = await asyncio.to_thread(
            self._admit, call, cache_key, input_sha, user, actor
        )
        if status is not LLMRequestStatus.RUNNING:
            logger.warning("ai.llm_refused", task=call.prompt.task.value, status=status.value)
            return CallOutcome(status.value, request_id=request_id, reason=reason)
        request = ProviderRequest(
            model=self.provider.model,
            system=call.prompt.system,
            user=user,
            tool_name=call.prompt.tool_name,
            tool_description=call.prompt.tool_description,
            schema=provider_schema(call.prompt.schema),
            max_output_tokens=self.settings.llm_max_output_tokens,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )
        attempts = 0
        final: LLMRequestStatus = LLMRequestStatus.PROVIDER_ERROR
        error: str | None = None
        try:
            while attempts <= self.settings.llm_max_retries:
                attempts += 1
                try:
                    async with self._semaphore:
                        response = await asyncio.wait_for(
                            self.provider.complete(request),
                            timeout=self.settings.llm_timeout_seconds + 5,
                        )
                except (ProviderError, TimeoutError) as exc:
                    perr = (
                        exc
                        if isinstance(exc, ProviderError)
                        else ProviderError("TIMEOUT", "no response in time")
                    )
                    final = {
                        "TIMEOUT": LLMRequestStatus.TIMEOUT,
                        "RATE_LIMITED": LLMRequestStatus.RATE_LIMITED,
                    }.get(perr.kind, LLMRequestStatus.PROVIDER_ERROR)
                    error = f"{perr.kind}: {perr}"
                    if not perr.retryable or attempts > self.settings.llm_max_retries:
                        break
                    delay = perr.retry_after if perr.retry_after is not None else 2.0**attempts
                    await self._sleep(min(MAX_BACKOFF_SECONDS, delay))
                    continue
                parsed, errors = self._validate(call, response.output)
                if response.note and parsed is None:
                    errors = [response.note, *errors]
                response_id = await asyncio.to_thread(
                    self._record_response, request_id, attempts, call, response, parsed, errors
                )
                self._failures = 0
                self._cooldown_until = None
                if parsed is None:
                    await asyncio.to_thread(
                        self._finish,
                        request_id,
                        LLMRequestStatus.INVALID_OUTPUT,
                        attempts,
                        "; ".join(errors)[:2000],
                    )
                    return CallOutcome(
                        LLMRequestStatus.INVALID_OUTPUT.value,
                        request_id=request_id,
                        response_id=response_id,
                        reason="the output failed strict validation",
                        errors=errors,
                    )
                await asyncio.to_thread(
                    self._finish, request_id, LLMRequestStatus.SUCCEEDED, attempts, None
                )
                return CallOutcome(
                    LLMRequestStatus.SUCCEEDED.value, parsed, request_id, response_id
                )
        except BaseException:
            await asyncio.to_thread(
                self._finish, request_id, LLMRequestStatus.INTERRUPTED, attempts, "interrupted"
            )
            raise
        self._failures += 1
        if self._failures >= self.settings.llm_failure_threshold:
            self._cooldown_until = self.clock.now() + timedelta(
                seconds=self.settings.llm_cooldown_seconds
            )
        await asyncio.to_thread(self._finish, request_id, final, attempts, error)
        logger.warning("ai.llm_failed", task=call.prompt.task.value, status=final.value)
        return CallOutcome(final.value, request_id=request_id, reason=error)


__all__ = ["CACHED", "UNAVAILABLE", "CallOutcome", "LLMService", "TaskCall"]
