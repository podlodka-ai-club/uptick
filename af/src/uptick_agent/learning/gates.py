from __future__ import annotations

import re

from uptick_agent.core.bootstrap_models import ToolRegistry
from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    LessonGateResult,
    LessonProposal,
    LessonRecord,
    LessonValidation,
)
from uptick_agent.runtime.episodes import lesson_record_id

LESSON_VALIDATOR_VERSION = "lesson-gate-v4"
_SECRET = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._~-]+|"
    r"(?:api[_ -]?key|password|client[_ -]?secret|access[_ -]?token)\s*[:=])"
)
_OBSERVED_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?")
_REMEDIATION_TOKEN = re.compile(r"\b(?:fix|mitigate|remediate)[-_][A-Za-z0-9._~-]+\b", re.I)
_IDENTIFIER_ARGUMENT = re.compile(
    r"(?:^|_)(?:id|token|message|secret|key|code)(?:$|_)",
    re.I,
)


def evaluate_lesson_proposal(
    *,
    batch: ConsolidationBatch,
    proposal: LessonProposal,
    registry: ToolRegistry,
) -> LessonGateResult:
    violations: list[str] = []
    episodes = {item.record_id: item for item in batch.episodes}
    active_lessons = {item.record_id: item for item in batch.active_lessons}
    evidence_ids = proposal.evidence_episode_ids
    contradiction_ids = proposal.contradicting_episode_ids

    _require_unique("evidence episode IDs", evidence_ids, violations)
    _require_unique("contradicting episode IDs", contradiction_ids, violations)
    _require_unique("superseded lesson IDs", proposal.supersedes, violations)
    _require_unique("capability names", proposal.capability_names, violations)

    unknown_evidence = sorted(set(evidence_ids) - set(episodes))
    if unknown_evidence:
        violations.append("unknown evidence episodes: " + ", ".join(unknown_evidence))
    unknown_contradictions = sorted(set(contradiction_ids) - set(episodes))
    if unknown_contradictions:
        violations.append("unknown contradicting episodes: " + ", ".join(unknown_contradictions))
    overlap = sorted(set(evidence_ids) & set(contradiction_ids))
    if overlap:
        violations.append(
            "episodes cannot support and contradict one lesson: " + ", ".join(overlap)
        )

    selected_evidence = [episodes[item] for item in evidence_ids if item in episodes]
    group_count = len({item.evidence_group_id for item in selected_evidence})
    if group_count < batch.query.min_evidence_groups:
        violations.append(
            f"proposal has {group_count} evidence groups; "
            f"requires {batch.query.min_evidence_groups}"
        )

    for episode in batch.episodes:
        if (
            episode.environment_id != batch.query.environment_id
            or episode.environment_profile_version != batch.query.environment_profile_version
        ):
            violations.append(f"episode {episode.record_id} is outside the consolidation scope")
        for reference in episode.evidence_refs:
            if reference.stream_id != f"run:{episode.run_id}" or reference.kind != "decision_trace":
                violations.append(f"episode {episode.record_id} has an invalid evidence reference")

    registry_names = {item.capability_name for item in registry.items}
    unknown_capabilities = sorted(set(proposal.capability_names) - registry_names)
    if unknown_capabilities:
        violations.append("unknown capabilities: " + ", ".join(unknown_capabilities))
    unknown_superseded = sorted(set(proposal.supersedes) - set(active_lessons))
    if unknown_superseded:
        violations.append("unknown active lessons to supersede: " + ", ".join(unknown_superseded))

    proposal_text = _proposal_text(proposal)
    if any(_SECRET.search(value) for value in proposal_text):
        violations.append("proposal contains credential-like material")
    if any(_OBSERVED_TIMESTAMP.search(value) for value in proposal_text):
        violations.append("proposal contains an observed timestamp")
    if any(_REMEDIATION_TOKEN.search(value) for value in proposal_text):
        violations.append("proposal contains a remediation token")
    normalized_text = "\n".join(proposal_text).casefold()
    if any(identifier.casefold() in normalized_text for identifier in _batch_identifiers(batch)):
        violations.append("proposal text contains an evidence, run, or world identifier")
    if any(value.casefold() in normalized_text for value in _sensitive_argument_values(batch)):
        violations.append("proposal text contains a world-specific capability argument")

    proposal_signature = _lesson_signature(proposal)
    if any(_record_signature(lesson) == proposal_signature for lesson in active_lessons.values()):
        violations.append("an identical active lesson already exists")

    return LessonGateResult(accepted=not violations, violations=violations)


def build_lesson_record(
    *,
    batch: ConsolidationBatch,
    proposal: LessonProposal,
) -> LessonRecord:
    evidence = {item.record_id: item for item in batch.episodes}
    evidence_group_count = len(
        {
            evidence[item].evidence_group_id
            for item in proposal.evidence_episode_ids
            if item in evidence
        }
    )
    return LessonRecord(
        record_id=lesson_record_id(
            proposal=proposal,
            environment_id=batch.query.environment_id,
            environment_profile_version=batch.query.environment_profile_version,
            validator_version=LESSON_VALIDATOR_VERSION,
        ),
        claim=proposal.claim,
        applies_when=proposal.applies_when,
        exceptions=proposal.exceptions,
        environment_id=batch.query.environment_id,
        environment_profile_version=batch.query.environment_profile_version,
        capability_names=proposal.capability_names,
        evidence_episode_ids=proposal.evidence_episode_ids,
        contradicting_episode_ids=proposal.contradicting_episode_ids,
        supersedes=proposal.supersedes,
        validation=LessonValidation(
            validator_version=LESSON_VALIDATOR_VERSION,
            evidence_group_count=evidence_group_count,
        ),
    )


def _require_unique(label: str, values: list[str], violations: list[str]) -> None:
    if len(values) != len(set(values)):
        violations.append(f"{label} must be unique")


def _proposal_text(proposal: LessonProposal) -> list[str]:
    return [proposal.claim, *proposal.applies_when, *proposal.exceptions]


def _batch_identifiers(batch: ConsolidationBatch) -> set[str]:
    return {
        value
        for episode in batch.episodes
        for value in (episode.record_id, episode.run_id, episode.evidence_group_id)
        if len(value) >= 4
    }


def _sensitive_argument_values(batch: ConsolidationBatch) -> set[str]:
    values: set[str] = set()
    for episode in batch.episodes:
        for key, value in episode.selected_action.arguments.items():
            if _IDENTIFIER_ARGUMENT.search(key):
                values.update(item for item in _string_values(value) if len(item) >= 4)
    return values


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_values(child)]
    return []


def _lesson_signature(proposal: LessonProposal) -> tuple[object, ...]:
    return (
        proposal.claim,
        tuple(proposal.applies_when),
        tuple(proposal.exceptions),
        tuple(sorted(proposal.capability_names)),
    )


def _record_signature(lesson: LessonRecord) -> tuple[object, ...]:
    return (
        lesson.claim,
        tuple(lesson.applies_when),
        tuple(lesson.exceptions),
        tuple(sorted(lesson.capability_names)),
    )
