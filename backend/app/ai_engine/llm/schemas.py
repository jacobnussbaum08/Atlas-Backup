"""Strict output schemas of every LLM task (Phase 5B).

An LLM's answer is accepted only if it validates against one of these models exactly
(``extra="forbid"``, bounded strings and lists, enumerated labels). There is deliberately
no field for a price, a quantity, a position size, an order, a risk limit or a
configuration value: an output cannot carry one, and anything outside the schema fails
validation and is stored as INVALID, never used.

Labels are the model's *estimates* (unverified). ``confidence`` is self-reported by the
LLM and is not a probability.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.feature_engine.registry import sha256_json
from app.news.enums import EventCategory

SYMBOL = r"^[A-Za-z0-9.\-/:$]{1,32}$"
REF = r"^[a-z][a-z0-9_-]{0,31}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AssetAssessment(_Strict):
    symbol: str = Field(pattern=SYMBOL)
    relevance: Literal["PRIMARY", "SECONDARY", "MENTIONED", "NOT_RELEVANT"]
    sentiment: Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "MIXED"]
    rationale: str = Field(max_length=300)


class ArticleAnalysis(_Strict):
    article_ref: str = Field(pattern=REF)
    # Labels of Atlas's news taxonomy (news-taxonomy-v1); JSON strings are accepted.
    event_type: Annotated[EventCategory, Field(strict=False)]
    event_interpretation: str = Field(max_length=400)
    summary: str = Field(max_length=500)
    catalysts: list[str] = Field(max_length=5)
    impact_horizon: Literal["INTRADAY", "DAYS", "WEEKS", "LONGER", "UNCLEAR"]
    novelty: Literal["NEW_INFORMATION", "FOLLOW_UP", "RECAP", "UNCLEAR"]
    assets: list[AssetAssessment] = Field(max_length=10)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    # The model's own report that the article text tried to instruct it.
    contains_instructions: bool


class NewsAnalysisBatch(_Strict):
    analyses: list[ArticleAnalysis] = Field(max_length=20)


class ConflictNote(_Strict):
    conflict_ref: str = Field(pattern=REF)
    explanation: str = Field(max_length=600)
    evidence_refs: list[str] = Field(max_length=10)


class ConflictExplanation(_Strict):
    overview: str = Field(max_length=800)
    conflicts: list[ConflictNote] = Field(max_length=20)
    caveats: list[str] = Field(max_length=5)


class ResearchSummary(_Strict):
    symbol: str = Field(pattern=SYMBOL)
    summary: str = Field(max_length=1000)
    themes: list[str] = Field(max_length=6)
    sentiment_balance: Literal["MOSTLY_POSITIVE", "MOSTLY_NEGATIVE", "MIXED", "NEUTRAL", "UNCLEAR"]
    open_questions: list[str] = Field(max_length=5)
    cited_refs: list[str] = Field(max_length=20)


SCHEMA_VERSIONS: dict[type[BaseModel], str] = {
    NewsAnalysisBatch: "news-analysis-schema-v1",
    ConflictExplanation: "conflict-explanation-schema-v1",
    ResearchSummary: "research-summary-schema-v1",
}

# Keywords some providers' structured-output modes reject. They are removed from the
# schema *sent*; the full model above still validates every answer locally.
_UNPORTABLE = ("maxLength", "minLength", "maxItems", "minItems", "pattern", "title", "default")


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _inline(copy.deepcopy(defs[name]), defs)
        return {
            k: _inline(v, defs) for k, v in node.items() if k not in _UNPORTABLE and k != "$defs"
        }
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A self-contained JSON schema (no $ref) of ``model`` for a provider request."""
    full = model.model_json_schema()
    schema = _inline(full, dict(full.get("$defs", {})))
    assert isinstance(schema, dict)  # noqa: S101  # a model's schema is an object
    return schema


def schema_sha256(model: type[BaseModel]) -> str:
    return sha256_json(model.model_json_schema())


__all__ = [
    "SCHEMA_VERSIONS",
    "ArticleAnalysis",
    "AssetAssessment",
    "ConflictExplanation",
    "ConflictNote",
    "NewsAnalysisBatch",
    "ResearchSummary",
    "provider_schema",
    "schema_sha256",
]
