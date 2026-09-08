from __future__ import annotations

import hashlib
import json

from uptick_agent.core.memory_models import (
    EpisodeRecord,
    EvidenceRef,
    LearningTrigger,
    LessonProposal,
    MemoryView,
)
from uptick_agent.core.models import OpenDecision, VerificationAssessment

_EPISODE_ID_VERSION = "episode-v2"
_EVIDENCE_GROUP_ID_VERSION = "evidence-group-v2"
_LEARNING_OPERATION_ID_VERSION = "learning-operation-v3"
_LESSON_ID_VERSION = "lesson-v1"
_CLOSED_STATUSES = {"confirmed", "contradicted", "inconclusive"}


def validate_episode_transition(
    *,
    open_decision: OpenDecision | None,
    assessment: VerificationAssessment,
) -> None:
    if open_decision is None and assessment.status != "not_applicable":
        raise ValueError("verification without an open decision must be not_applicable")
    if open_decision is not None and assessment.status == "not_applicable":
        raise ValueError("not_applicable cannot assess an open decision")


def episode_record_id(
    *,
    environment_id: str,
    environment_profile_version: str,
    run_id: str,
    step: int,
) -> str:
    return "episode-" + _sha256_json(
        {
            "environment_id": environment_id,
            "environment_profile_version": environment_profile_version,
            "namespace": _EPISODE_ID_VERSION,
            "run_id": run_id,
            "step": step,
        }
    )


def evidence_group_id(
    *,
    environment_id: str,
    environment_profile_version: str,
    world_id: str | None,
    run_id: str,
) -> str:
    """Return an opaque independence key shared by runs of the same world."""

    scope = {"world_id": world_id} if world_id is not None else {"run_id": run_id}
    return "evidence-group-" + _sha256_json(
        {
            "environment_id": environment_id,
            "environment_profile_version": environment_profile_version,
            "namespace": _EVIDENCE_GROUP_ID_VERSION,
            **scope,
        }
    )


def project_closed_episode(
    *,
    open_decision: OpenDecision | None,
    assessment: VerificationAssessment,
    environment_id: str,
    environment_profile_version: str,
    evidence_group_id: str,
    run_id: str,
    situation_summary: str,
    observation_summary: str,
    evidence_refs: list[EvidenceRef],
) -> EpisodeRecord | None:
    validate_episode_transition(open_decision=open_decision, assessment=assessment)
    if open_decision is None:
        return None
    if assessment.status == "pending":
        return None
    if assessment.status not in _CLOSED_STATUSES:
        raise ValueError(f"unsupported verification status: {assessment.status}")
    return EpisodeRecord(
        record_id=episode_record_id(
            environment_id=environment_id,
            environment_profile_version=environment_profile_version,
            run_id=run_id,
            step=open_decision.step,
        ),
        environment_id=environment_id,
        environment_profile_version=environment_profile_version,
        evidence_group_id=evidence_group_id,
        run_id=run_id,
        step=open_decision.step,
        situation_summary=situation_summary,
        selected_action=open_decision.selected_action,
        expected_result=open_decision.expected_result,
        observation_summary=observation_summary,
        verification=assessment,
        evidence_refs=evidence_refs,
        facts=open_decision.facts,
        strategy=open_decision.strategy,
    )


def learning_operation_id(
    *,
    trigger: LearningTrigger,
    trigger_run_id: str,
    trigger_episode_id: str | None,
    base_view: MemoryView,
    environment_id: str,
    environment_profile_version: str,
) -> str:
    if trigger == "after_closed_episode" and trigger_episode_id is None:
        raise ValueError("after_closed_episode requires trigger_episode_id")
    if trigger == "after_run" and trigger_episode_id is not None:
        raise ValueError("after_run must not set trigger_episode_id")
    return "learning-" + _sha256_json(
        {
            "base_view": base_view.model_dump(mode="json"),
            "environment_id": environment_id,
            "environment_profile_version": environment_profile_version,
            "namespace": _LEARNING_OPERATION_ID_VERSION,
            "trigger": trigger,
            "trigger_episode_id": trigger_episode_id,
            "trigger_run_id": trigger_run_id,
        }
    )


def lesson_record_id(
    *,
    proposal: LessonProposal,
    environment_id: str,
    environment_profile_version: str,
    validator_version: str,
) -> str:
    return "lesson-" + _sha256_json(
        {
            "environment_id": environment_id,
            "environment_profile_version": environment_profile_version,
            "namespace": _LESSON_ID_VERSION,
            "proposal": proposal.model_dump(mode="json"),
            "validator_version": validator_version,
        }
    )


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
