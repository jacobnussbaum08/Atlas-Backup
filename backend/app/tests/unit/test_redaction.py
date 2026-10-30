from __future__ import annotations

import pytest

from app.core.security.redaction import REDACTED, redact_mapping, redact_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql+psycopg://atlas:pw@host/db", f"postgresql+psycopg://atlas:{REDACTED}@host/db"),
        ("redis://:pw@cache:6379/0", f"redis://:{REDACTED}@cache:6379/0"),
        ("https://example.com/path", "https://example.com/path"),
        ("password=abc rest", f"password={REDACTED} rest"),
        ("API_KEY: 'xyz'", f"API_KEY: {REDACTED}"),
        ("token=abc&other=1", f"token={REDACTED}&other=1"),
        ("nothing to hide", "nothing to hide"),
    ],
)
def test_redact_text(raw, expected):
    assert redact_text(raw) == expected


def test_redact_mapping_masks_sensitive_keys_recursively():
    data = {
        "Authorization": "Bearer abc",
        "db_password": "x",
        "items": [{"secret": "s"}, "url postgresql://u:p@h/d"],
        "count": 3,
    }

    result = redact_mapping(data)

    assert result == {
        "Authorization": REDACTED,
        "db_password": REDACTED,
        "items": [{"secret": REDACTED}, f"url postgresql://u:{REDACTED}@h/d"],
        "count": 3,
    }
    assert data["db_password"] == "x"  # input is not mutated
