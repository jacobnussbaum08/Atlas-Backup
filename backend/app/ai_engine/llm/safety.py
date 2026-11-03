"""Untrusted-content handling for LLM prompts (Phase 5B, spec 12.5).

Article text is written by third parties and may try to instruct a model ("ignore your
instructions and ..."). Atlas defends in layers; none of them relies on the model obeying:

1. **Data, not instructions.** Untrusted text is sent only inside a JSON document, inside
   a delimited block the system prompt declares to be data. ``<`` and ``>`` are escaped as
   JSON unicode escapes, so text cannot close the block or open a fake one.
2. **Bounded input.** Control and bidirectional-format characters are removed (as at
   ingestion, D-081) and each text is truncated.
3. **Detection.** Instruction-like patterns aimed at an AI are flagged before sending;
   the model is also asked to report embedded instructions. Flagged analyses are stored
   for audit with ``usable = false`` and never reach a decision.
4. **No capability to abuse.** The model can only answer through a strict schema with no
   price, size, order or configuration field (:mod:`.schemas`). Its symbols are checked
   against the ones Atlas supplied; anything else is dropped and flagged.
5. **No authority.** LLM output is never an instruction to Atlas: it is a stored,
   low-weight, unverified feature that a deterministic coordinator may read.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

_CONTROL = {"Cc", "Cf"}

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "ignore_instructions",
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(instructions?|rules?|prompts?|guidelines?|directions?)\b",
        ),
        ("system_prompt", r"\b(system|developer)\s+(prompt|message|instructions?)\b"),
        ("role_change", r"\byou\s+are\s+(now|no\s+longer)\b|\bact\s+as\s+(an?|the)\b"),
        ("new_instructions", r"\bnew\s+(instructions?|task|rules?)\s*:"),
        (
            "chat_markup",
            r"<\|?(im_start|im_end|system|assistant)\|?>|\[/?INST\]|###\s*(system|instruction)",
        ),
        (
            "ai_addressed",
            r"\b(dear|attention|note\s+to)\s+(ai|assistant|llm|language\s+model|chatbot)\b",
        ),
        (
            "tool_or_action",
            r"\b(call|invoke|execute|run)\s+(the\s+)?(tool|function|command|trade|order)\b",
        ),
        ("jailbreak", r"\b(jailbreak|dan\s+mode|developer\s+mode)\b"),
    )
)


def clean_text(value: str | None, limit: int) -> str | None:
    """Remove control and format characters (keeping newlines and tabs) and truncate."""
    if value is None:
        return None
    kept = "".join(ch for ch in value if ch in "\n\t" or unicodedata.category(ch) not in _CONTROL)
    kept = re.sub(r"[ \t]+", " ", kept).strip()
    if len(kept) > limit:
        kept = kept[: max(0, limit - 1)].rstrip() + "…"
    return kept


def injection_flags(*texts: str | None) -> list[str]:
    found = []
    for name, pattern in INJECTION_PATTERNS:
        if any(t and pattern.search(t) for t in texts):
            found.append(f"suspected_prompt_injection:{name}")
    return found


def encode_untrusted(document: Any) -> str:
    """JSON with ``<``, ``>`` and ``&`` escaped so no text can form markup or delimiters."""
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


__all__ = ["INJECTION_PATTERNS", "clean_text", "encode_untrusted", "injection_flags"]
