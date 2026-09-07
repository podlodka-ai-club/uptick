"""Pure construction of generic experience transitions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryValidationError,
    ObjectiveMetric,
    ObjectiveMetricDelta,
    ProvenanceRef,
    TransitionAssemblyRequest,
)
from uptick_agent.redaction import sanitize_json


def _sha256_json(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _source_provenance(
    request: TransitionAssemblyRequest,
    *,
    label: str,
    content: dict,
) -> ProvenanceRef:
    identity = {
        "label": label,
        "run_id": request.run_id,
        "iteration": request.iteration,
        "transition_id": request.transition_id,
    }
    return ProvenanceRef(
        artefact_id=f"{label}:{_sha256_json(identity)}",
        content_hash=_sha256_json(content),
    )


def _metric_map(metrics: list[ObjectiveMetric]) -> dict[tuple[str, str], ObjectiveMetric]:
    result: dict[tuple[str, str], ObjectiveMetric] = {}
    for metric in metrics:
        key = (metric.name, metric.unit)
        if key in result:
            raise MemoryValidationError(
                f"duplicate objective metric observation {metric.name!r}/{metric.unit!r}"
            )
        result[key] = metric
    return result


def _objective_deltas(
    before: list[ObjectiveMetric], after: list[ObjectiveMetric]
) -> list[ObjectiveMetricDelta]:
    before_by_key = _metric_map(before)
    after_by_key = _metric_map(after)
    return [
        ObjectiveMetricDelta(
            name=name,
            unit=unit,
            before=before_by_key[(name, unit)].value,
            after=after_by_key[(name, unit)].value,
            delta=after_by_key[(name, unit)].value - before_by_key[(name, unit)].value,
        )
        for name, unit in sorted(before_by_key.keys() & after_by_key.keys())
    ]


class DefaultExperienceTransitionAssembler:
    """Validate explicit inputs, derive source hashes, and compute transparent deltas."""

    def assemble(self, request: TransitionAssemblyRequest) -> ExperienceTransition:
        if not isinstance(request, TransitionAssemblyRequest):
            raise MemoryValidationError("transition assembly requires TransitionAssemblyRequest")
        try:
            owned = TransitionAssemblyRequest.model_validate(
                request.model_dump(mode="python", round_trip=True, warnings="error")
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError(
                "transition assembly request contains invalid data"
            ) from error

        if owned.occurred_at is None or owned.occurred_at.utcoffset() is None:
            raise MemoryValidationError("transition assembly requires an aware occurrence time")
        if owned.trust_classification is None:
            raise MemoryValidationError("transition assembly requires a trust classification")
        if not owned.observation or not owned.action or not owned.result:
            raise MemoryValidationError(
                "transition assembly requires observation, action, and result payloads"
            )

        try:
            pre_state = sanitize_json(owned.pre_state)
            observation = sanitize_json(owned.observation)
            action = sanitize_json(owned.action)
            result = sanitize_json(owned.result)
        except (TypeError, ValueError) as error:
            raise MemoryValidationError(
                "transition payload could not cross the persistence redaction boundary"
            ) from error
        if not all(isinstance(value, dict) for value in (pre_state, observation, action, result)):
            raise MemoryValidationError("transition payload must remain a JSON object")

        observation_source = {
            "pre_state": pre_state,
            "observation": observation,
        }
        result_source = {"action": action, "result": result}
        provenance = [
            _source_provenance(owned, label="observation", content=observation_source),
            _source_provenance(owned, label="result", content=result_source),
        ]
        operation_links = sorted(
            {(link.relation, link.operation_id): link for link in owned.operation_links}.values(),
            key=lambda link: (link.relation, link.operation_id),
        )
        try:
            transition = ExperienceTransition(
                transition_id=owned.transition_id,
                run_id=owned.run_id,
                iteration=owned.iteration,
                occurred_at=owned.occurred_at.astimezone(UTC),
                environment_id=owned.environment_id,
                scenario_id=owned.scenario_id,
                trust_classification=owned.trust_classification,
                pre_state=pre_state,
                observation=observation,
                action=action,
                result=result,
                objective_metrics=owned.after_objective_metrics,
                objective_deltas=_objective_deltas(
                    owned.before_objective_metrics,
                    owned.after_objective_metrics,
                ),
                operation_links=operation_links,
                provenance=provenance,
                terminal=owned.terminal,
            )
        except (PydanticSerializationError, TypeError, ValueError, ValidationError) as error:
            raise MemoryValidationError(
                "could not assemble a valid experience transition"
            ) from error
        serialized = transition.model_dump(mode="json")
        if sanitize_json(serialized) != serialized:
            raise MemoryValidationError("transition metadata contains credential-shaped content")
        return transition
