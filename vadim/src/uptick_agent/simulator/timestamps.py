"""Lossless RFC3339 timestamp parsing for simulator query windows."""

from __future__ import annotations

import re
from datetime import datetime
from fractions import Fraction

_RFC3339_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)

# This is deliberately a lexical constraint as well as a runtime parser.  It
# documents the wire contract in generated schemas while the parser below
# checks calendar and offset ranges that a regex cannot express.  Keep this
# schema pattern in ECMA-262 syntax; Python's named-group syntax is not valid
# in provider JSON-schema validators.
RFC3339_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


TimestampOrder = tuple[int, Fraction]


def parse_rfc3339(value: object) -> TimestampOrder:
    """Parse a timezone-aware RFC3339 value without truncating its fraction."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        # Python datetime cannot carry more than microseconds.  Converting it
        # to the same exact representation keeps the compatibility input
        # while avoiding datetime comparison for string timestamps.
        return parse_rfc3339(value.isoformat())

    if not isinstance(value, str):
        raise ValueError("timestamp must be an RFC3339 string or datetime")
    match = _RFC3339_RE.fullmatch(value)
    if match is None:
        raise ValueError("timestamp must be a timezone-aware RFC3339 date-time")

    try:
        base = datetime.strptime(
            f"{match['date']}T{match['hour']}:{match['minute']}:{match['second']}",
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError as error:
        raise ValueError("timestamp is not a valid date-time") from error

    offset_text = match["offset"]
    if offset_text == "Z":
        offset_seconds = 0
    else:
        offset_hours = int(offset_text[1:3])
        offset_minutes = int(offset_text[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError("timestamp has an invalid timezone offset")
        offset_seconds = (offset_hours * 60 + offset_minutes) * 60
        if offset_text[0] == "-":
            offset_seconds = -offset_seconds

    delta = base - datetime(1970, 1, 1)
    seconds = delta.days * 86_400 + delta.seconds - offset_seconds
    fraction_text = match["fraction"]
    fraction = (
        Fraction(int(fraction_text[1:]), 10 ** (len(fraction_text) - 1))
        if fraction_text is not None
        else Fraction(0)
    )
    return seconds, fraction


def coerce_query_timestamp(value: object) -> object:
    """Normalize datetime compatibility inputs while preserving strings."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.isoformat()
    return value


def validate_query_timestamp(value: str) -> str:
    """Pydantic after-validator for a lossless query timestamp field."""

    parse_rfc3339(value)
    return value


def query_timestamp(value: datetime | str) -> str:
    """Return the exact wire value for a query bound after validation."""

    value = coerce_query_timestamp(value)
    if not isinstance(value, str):
        raise ValueError("timestamp must be an RFC3339 string or datetime")
    parse_rfc3339(value)
    return value
