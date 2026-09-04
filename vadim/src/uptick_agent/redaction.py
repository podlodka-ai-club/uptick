"""Small shared boundary for removing credential-shaped persisted values."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

_SECRET_LABEL = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|"
    r"credential(?:s)?|token|authorization)"
)
_SECRET_KEY = re.compile(rf"(?i)^{_SECRET_LABEL}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)authorization\s*[:=]\s*(?:basic|bearer|token)\s+[^\s,;]+"),
    re.compile(rf"(?i){_SECRET_LABEL}\s*[:=]\s*(?:bearer|token)\s+[^\s,;]+"),
    re.compile(rf"(?i){_SECRET_LABEL}\s*[:=]\s*[^\s,;]+"),
    re.compile(
        r"(?i)authorization\s*(?:(?:[:=]\s*)?(?:bearer|token)\s+|[:=]\s*)"
        r"[^\s,;]+"
    ),
    re.compile(r"(?i)\b(?:bearer|token)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def redact_text(value: str) -> str:
    """Remove the credential forms covered by the repository policy fixtures."""

    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("<redacted>", result)
    return result


def sanitize_json(value: object) -> object:
    """Recursively redact JSON; reject values that cannot cross this boundary."""

    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if is_secret_key(str(key)) else sanitize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("persisted JSON must not contain NaN or infinity")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported persisted value {type(value).__name__}")
