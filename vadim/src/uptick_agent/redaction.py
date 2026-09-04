"""Small shared boundary for removing credential-shaped persisted values."""

from __future__ import annotations

import json
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
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    rf'''(?ix)
        (?P<key_quote>["'])
        (?P<key>{_SECRET_LABEL})
        (?P=key_quote)(?P<separator>\s*:\s*)
        (?P<value_quote>["'])
        (?:\\.|(?! (?P=value_quote) ).)*
        (?P=value_quote)
    '''
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def redact_text(value: str) -> str:
    """Remove the credential forms covered by the repository policy fixtures."""

    result = _redact_embedded_json(value)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("<redacted>", result)
    return _QUOTED_SECRET_ASSIGNMENT.sub(_replace_quoted_secret, result)


def _replace_quoted_secret(match: re.Match[str]) -> str:
    key_quote = match.group("key_quote")
    value_quote = match.group("value_quote")
    return (
        f"{key_quote}{match.group('key')}{key_quote}{match.group('separator')}"
        f"{value_quote}<redacted>{value_quote}"
    )


def _redact_embedded_json(value: str) -> str:
    """Redact secrets in JSON objects nested inside otherwise free-form text."""

    decoder = json.JSONDecoder()
    result: list[str] = []
    source_start = 0
    changed = False
    index = 0
    while index < len(value):
        if value[index] not in '[{"':
            index += 1
            continue
        escaped_fragment: str | None = None
        try:
            decoded, end = decoder.raw_decode(value, index)
        except json.JSONDecodeError:
            escaped = _decode_escaped_json_fragment(value, index)
            if escaped is None:
                index += 1
                continue
            decoded, end, escaped_fragment = escaped
        if not isinstance(decoded, (Mapping, list, str)):
            index = end
            continue
        safe = sanitize_json(decoded)
        if safe != decoded:
            result.append(value[source_start:index])
            rendered = json.dumps(
                safe,
                ensure_ascii=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if escaped_fragment == "json-string":
                rendered = json.dumps(rendered, ensure_ascii=True)[1:-1]
            elif escaped_fragment == "quote-only":
                rendered = rendered.replace('"', '\\"')
            result.append(rendered)
            source_start = end
            changed = True
        index = end
    if not changed:
        return value
    result.append(value[source_start:])
    return "".join(result)


def _decode_escaped_json_fragment(
    value: str, start: int
) -> tuple[object, int, str] | None:
    """Decode escaped JSON by validating complete candidates with ``json``.

    Escaped fragments can contain backslashes immediately before structural
    quotes, so a hand-written string-state scanner cannot reliably identify
    the closing delimiter. Trying each closing delimiter is bounded by the
    JSON decoder: braces in a string or an unmatched nested delimiter are
    rejected until the first complete object or array is found.
    """

    if value[start] not in "[{":
        return None

    for index in range(start + 1, len(value)):
        if value[index] not in "]}":
            continue
        candidate = value[start : index + 1]
        try:
            decoded_text = json.loads(f'"{candidate}"')
            decoded = json.loads(decoded_text)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, (Mapping, list)):
            return decoded, index + 1, "json-string"

        try:
            decoded = json.loads(candidate.replace('\\"', '"'))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, (Mapping, list)):
            return decoded, index + 1, "quote-only"
    return None


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
