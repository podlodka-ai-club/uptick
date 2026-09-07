"""Bounded capacity observations projected from public ``query_logs`` results.

The projector only interprets fields present in a successful public log query.
It deliberately records a request exceeding the reported *remaining* capacity
without inferring the server's total capacity or a utilization threshold.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from uptick_agent.memory.contracts import ExperienceTransition, TransitionAssemblyRequest
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.settings import PatternQuerySettings
from uptick_agent.memory.stores.contracts import sha256_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

CAPACITY_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.error_code", "observation.relation"),
    action_path="action.kind",
    result_path="result.rejected",
)

_LOGS_RECORD_TYPE = "experience-transition"
_CAPACITY_ERROR = "SERVER_CAPACITY_EXCEEDED"
_CAPACITY_MESSAGE = re.compile(
    r"^server\s+capacity\s+exceeded:\s*"
    r"required=(?P<required>[0-9]+(?:\.[0-9]+)?)\s+"
    r"available=(?P<available>[0-9]+(?:\.[0-9]+)?)\s*$",
    re.IGNORECASE,
)


def _finite_nonnegative_number(value: object) -> int | float | None:
    """Return a JSON number when it is finite and non-negative."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _message_number(value: str) -> int | float | None:
    """Parse a non-negative decimal from the public error message."""

    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    if number == number.to_integral_value():
        return int(number)
    converted = float(number)
    return converted if math.isfinite(converted) else None


def _same_number(left: int | float, right: int | float) -> bool:
    """Compare JSON numbers without making binary float formatting observable."""

    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False


def _timestamp(value: object) -> tuple[str, datetime] | None:
    """Validate a public log timestamp and return its original text and UTC time."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return raw, parsed.astimezone(UTC)


def _aware_cutoffs(cutoffs: Mapping[str, datetime]) -> dict[str, datetime]:
    """Keep only usable cutoffs; an unusable cutoff cannot authorize evidence."""

    if not isinstance(cutoffs, Mapping):
        return {}
    usable: dict[str, datetime] = {}
    for run_id, cutoff in cutoffs.items():
        if not isinstance(run_id, str) or not run_id:
            continue
        if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
            continue
        usable[run_id] = cutoff.astimezone(UTC)
    return usable


def _capacity_row(log: object) -> dict[str, Any] | None:
    """Decode one log row only when every public capacity fact agrees."""

    if not isinstance(log, Mapping):
        return None
    if log.get("error") != _CAPACITY_ERROR:
        return None
    request_id = log.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    parsed_timestamp = _timestamp(log.get("timestamp"))
    if parsed_timestamp is None:
        return None
    timestamp, timestamp_utc = parsed_timestamp
    if log.get("status") != 500:
        return None
    load_units = _finite_nonnegative_number(log.get("load_units"))
    message = log.get("message")
    if not isinstance(message, str):
        return None
    match = _CAPACITY_MESSAGE.fullmatch(message.strip())
    if match is None:
        return None
    required_units = _message_number(match.group("required"))
    available_units = _message_number(match.group("available"))
    if required_units is None or available_units is None or load_units is None:
        return None
    if not _same_number(required_units, load_units) or required_units <= available_units:
        return None
    return {
        "request_id": request_id.strip(),
        "timestamp": timestamp,
        "timestamp_utc": timestamp_utc,
        "error": _CAPACITY_ERROR,
        "status": 500,
        "required_units": required_units,
        "available_units": available_units,
    }


def _source_transitions(
    evidence: LessonEvidence, cutoffs: Mapping[str, datetime]
) -> list[tuple[ExperienceTransition, object]]:
    """Read only cutoff-eligible source transitions before inspecting log rows."""

    if not isinstance(evidence, LessonEvidence):
        return []
    owned_cutoffs = _aware_cutoffs(cutoffs)
    candidates: list[tuple[ExperienceTransition, object]] = []
    for record in evidence.records:
        if record.record_type != _LOGS_RECORD_TYPE:
            continue
        try:
            transition = ExperienceTransition.model_validate(record.payload)
        except (TypeError, ValueError):
            continue
        cutoff = owned_cutoffs.get(transition.run_id)
        # This check is intentionally before reading ``result.data.logs`` so a
        # future source page cannot influence the projection even transiently.
        if cutoff is None or transition.occurred_at > cutoff:
            continue
        if transition.action.get("kind") != "query_logs" or transition.result.get("ok") is not True:
            continue
        data = transition.result.get("data")
        if not isinstance(data, Mapping):
            continue
        logs = data.get("logs")
        if not isinstance(logs, list):
            continue
        for log in logs:
            candidates.append((transition, log))
    candidates.sort(
        key=lambda item: (
            item[0].occurred_at,
            item[0].iteration,
            item[0].transition_id,
        )
    )
    return candidates


def project_capacity_observations(
    evidence: LessonEvidence, cutoffs: Mapping[str, datetime]
) -> list[ExperienceTransition]:
    """Project one descriptive transition per valid public capacity log event.

    Source pages may overlap. Events are identified by ``run_id``,
    ``request_id``, log ``timestamp``, and ``error``; the earliest eligible
    source transition wins. Every projected transition carries the source
    record's ID and content hash in its result source references; the
    assembler's provenance leaves remain intact for generic validation.
    """

    seen: set[tuple[str, str, str, str]] = set()
    projected: list[ExperienceTransition] = []
    assembler = DefaultExperienceTransitionAssembler()
    for source, raw_log in _source_transitions(evidence, cutoffs):
        row = _capacity_row(raw_log)
        if row is None:
            continue
        identity = (
            source.run_id,
            row["request_id"],
            row["timestamp"],
            row["error"],
        )
        if identity in seen:
            continue
        seen.add(identity)

        source_record = next(
            (
                record
                for record in evidence.records
                if record.record_type == _LOGS_RECORD_TYPE
                and record.record_id == source.transition_id
            ),
            None,
        )
        if source_record is None:
            # A transition without its stored source cannot preserve provenance.
            continue
        identity_hash = sha256_json(
            {
                "run_id": source.run_id,
                "request_id": row["request_id"],
                "timestamp": row["timestamp"],
                "error": row["error"],
            }
        )
        source_ref = {
            "record_id": source_record.record_id,
            "content_hash": source_record.content_hash,
            "transition_id": source.transition_id,
        }
        transition = assembler.assemble(
            TransitionAssemblyRequest(
                transition_id=f"sre-capacity:{identity_hash}",
                run_id=source.run_id,
                iteration=source.iteration,
                occurred_at=source.occurred_at,
                environment_id=source.environment_id,
                scenario_id=source.scenario_id,
                trust_classification="derived_untrusted",
                observation={
                    "error_code": _CAPACITY_ERROR,
                    "relation": "request_exceeds_remaining_capacity",
                },
                action={"kind": "request_admission"},
                result={
                    "rejected": True,
                    "status": row["status"],
                    "request_id": row["request_id"],
                    "timestamp": row["timestamp"],
                    "required_units": row["required_units"],
                    "available_units": row["available_units"],
                    "capacity_claim": "remaining_not_total",
                    "source_record_id": source_record.record_id,
                    "source_record_hash": source_record.content_hash,
                    "source_transition_id": source.transition_id,
                    "source_refs": [source_ref],
                },
                terminal=False,
            )
        )
        # The assembler's two provenance leaves are intentionally preserved:
        # generic evidence validation closes exactly those leaves. The raw
        # record hash is carried in ``result.source_refs`` and
        # ``result.source_record_hash`` above, where the projector's source
        # claim remains inspectable without weakening the generic contract.
        projected.append(transition)
    return projected


__all__ = [
    "CAPACITY_SETTINGS",
    "project_capacity_observations",
]
