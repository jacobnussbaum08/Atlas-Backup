"""Redaction of credentials before they reach logs or API responses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"

# Keys whose values are always secret, matched case-insensitively as substrings.
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)

# scheme://user:password@host  ->  scheme://user:***REDACTED***@host
_URL_CREDENTIALS = re.compile(r"(?P<prefix>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]*:)[^@\s]+(?=@)")
# password=xyz / token: "xyz" style fragments inside free text
_KEY_VALUE_SECRET = re.compile(
    r"(?P<key>\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[=:]\s*)"
    r"(?P<quote>['\"]?)[^'\"\s,;&]+(?P=quote)",
    re.IGNORECASE,
)
# Phase 10B named operator tokens (atlas_op_<random>) wherever they appear.
_OPERATOR_TOKEN = re.compile(r"atlas_op_[A-Za-z0-9_\-]{8,}")


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str) -> str:
    """Mask credentials embedded in URLs and ``key=value`` fragments."""
    text = _URL_CREDENTIALS.sub(rf"\g<prefix>{REDACTED}", text)
    text = _OPERATOR_TOKEN.sub(REDACTED, text)
    return _KEY_VALUE_SECRET.sub(rf"\g<key>{REDACTED}", text)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list | tuple):
        return type(value)(redact_value(item) for item in value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with secret keys masked and string values scrubbed."""
    return {
        key: REDACTED if is_sensitive_key(str(key)) else redact_value(value)
        for key, value in data.items()
    }
