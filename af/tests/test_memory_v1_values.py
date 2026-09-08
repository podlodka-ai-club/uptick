import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from uptick_agent.core.bootstrap_models import (
    EnvironmentBootstrapBundle,
    EnvironmentBrief,
    EnvironmentFact,
    EnvironmentProfile,
    ToolMetadata,
    ToolRegistry,
)
from uptick_agent.core.memory_models import EvidenceRef, MemoryPacket, MemoryView
from uptick_agent.core.models import (
    AgentConstraints,
    AgentWorkingState,
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentProfileRef,
    EnvironmentState,
    Observation,
    OpenDecision,
    VerificationAssessment,
)
from uptick_agent.core.trace_models import TRACE_PAYLOAD_MODELS
from uptick_agent.runtime.bootstrap import (
    bootstrap_identity,
    environment_profile_version,
)
from uptick_agent.runtime.context import ContextAssembler, RunState
from uptick_agent.runtime.episodes import (
    episode_record_id,
    learning_operation_id,
    project_closed_episode,
)
from uptick_agent.runtime.identity import (
    allocate_ad_hoc_run_id,
)


def _capabilities() -> CapabilityCatalog:
    return CapabilityCatalog(
        items=[
            Capability(
                name="inspect",
                description="inspect current state",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            )
        ]
    )


def _open_decision() -> OpenDecision:
    return OpenDecision(
        step=3,
        phase="verify",
        strategy="verify the prior change",
        situation_summary="the prior state required verification",
        selected_action=CapabilityCall(name="inspect"),
        expected_result=["the service is healthy"],
        verification=["the observation reports a healthy service"],
    )


def test_operational_run_id_preserves_issued_uuid() -> None:
    fixed = UUID("00000000-0000-0000-0000-000000000001")
    assert allocate_ad_hoc_run_id(value=fixed) == f"run-{fixed}"


def test_bootstrap_hashes_whole_document_catalog_and_semantic_output() -> None:
    source = "Environment: café"
    encoded = source.encode("utf-8")

    capabilities = _capabilities()
    identity = bootstrap_identity(
        source=source,
        environment_id="scripted",
        capabilities=capabilities,
        bootstrap_prompt_version="bootstrap-prompt-v1",
        bootstrap_schema_version="bootstrap-schema-v1",
        validator_version="bootstrap-validator-v1",
    )
    fact = EnvironmentFact(category="goal", statement="keep healthy")
    registry = ToolRegistry(
        items=[ToolMetadata(capability_name="inspect", purpose="inspect current state")]
    )
    version = environment_profile_version(
        environment_id="scripted",
        facts=[fact],
        guidance=["Inspect before changing state."],
        registry=registry,
        capability_catalog_sha256=identity.capability_catalog_sha256,
    )
    changed = environment_profile_version(
        environment_id="scripted",
        facts=[fact],
        guidance=["Inspect only when required."],
        registry=registry,
        capability_catalog_sha256=identity.capability_catalog_sha256,
    )
    bundle = EnvironmentBootstrapBundle(
        profile=EnvironmentProfile(
            environment_id="scripted",
            profile_version=version,
            facts=[fact],
        ),
        brief=EnvironmentBrief(
            environment_id="scripted",
            profile_version=version,
            guidance=["Inspect before changing state."],
        ),
        registry=registry,
        capability_catalog_sha256=identity.capability_catalog_sha256,
    )

    assert identity.document_sha256 == hashlib.sha256(encoded).hexdigest()
    assert version != changed
    assert bundle == EnvironmentBootstrapBundle.model_validate_json(bundle.model_dump_json())


def test_episode_projection_closes_only_verified_open_decisions() -> None:
    evidence = [EvidenceRef(stream_id="run:run-1", sequence=4, kind="decision_trace")]
    kwargs = {
        "open_decision": _open_decision(),
        "environment_id": "scripted",
        "environment_profile_version": "profile-1",
        "evidence_group_id": "run-1",
        "run_id": "run-1",
        "situation_summary": "a change is awaiting verification",
        "observation_summary": "the service is healthy",
        "evidence_refs": evidence,
    }

    assert (
        project_closed_episode(
            **kwargs,
            assessment=VerificationAssessment(status="pending"),
        )
        is None
    )
    closed = project_closed_episode(
        **kwargs,
        assessment=VerificationAssessment(status="confirmed", evidence=["healthy"]),
    )

    assert closed is not None
    assert closed.record_id == episode_record_id(
        environment_id="scripted",
        environment_profile_version="profile-1",
        run_id="run-1",
        step=3,
    )
    assert closed.verification.status == "confirmed"
    with pytest.raises(ValueError, match="not_applicable cannot assess"):
        project_closed_episode(
            **kwargs,
            assessment=VerificationAssessment(status="not_applicable"),
        )


def test_learning_operation_identity_includes_trigger_identity() -> None:
    view = MemoryView(database_id="memory-1", revision=7)
    first = learning_operation_id(
        trigger="after_run",
        trigger_run_id="run-1",
        trigger_episode_id=None,
        base_view=view,
        environment_id="uptick",
        environment_profile_version="profile-1",
    )
    repeated = learning_operation_id(
        trigger="after_run",
        trigger_run_id="run-1",
        trigger_episode_id=None,
        base_view=view,
        environment_id="uptick",
        environment_profile_version="profile-1",
    )
    another_run = learning_operation_id(
        trigger="after_run",
        trigger_run_id="run-2",
        trigger_episode_id=None,
        base_view=view,
        environment_id="uptick",
        environment_profile_version="profile-1",
    )
    first_inline = learning_operation_id(
        trigger="after_closed_episode",
        trigger_run_id="run-1",
        trigger_episode_id="episode-1",
        base_view=view,
        environment_id="uptick",
        environment_profile_version="profile-1",
    )
    second_inline = learning_operation_id(
        trigger="after_closed_episode",
        trigger_run_id="run-1",
        trigger_episode_id="episode-2",
        base_view=view,
        environment_id="uptick",
        environment_profile_version="profile-1",
    )

    assert first == repeated
    assert first != another_run
    assert first_inline != second_inline


def test_compact_context_excludes_internal_run_and_memory_view_metadata() -> None:
    observation = Observation(action_kind="start", summary="ready")
    environment_state = EnvironmentState(
        profile=EnvironmentProfileRef(environment_id="scripted", version="profile-1"),
        status="active",
        decision_view={"health": "ok"},
        latest_observation=observation,
    )
    working = AgentWorkingState()
    run_state = RunState(
        run_id="run-secret-from-prompt",
        step=1,
        step_limit=5,
        memory_view=MemoryView(database_id="db-private", revision=9),
        environment_state=environment_state,
        agent_working_state=working,
    )
    context = ContextAssembler().assemble(
        objective="keep healthy",
        environment_profile=EnvironmentBrief(
            environment_id="scripted",
            profile_version="profile-1",
            guidance=["inspect first"],
        ),
        run_state=run_state,
        memory=MemoryPacket(view=run_state.memory_view),
        capabilities=_capabilities(),
        constraints=AgentConstraints(),
    )
    serialized = context.model_dump_json()

    assert "run-secret-from-prompt" not in serialized
    assert "db-private" not in serialized
    assert '"revision"' not in serialized
    assert set(context.model_dump()) == {
        "objective",
        "environment_profile",
        "environment_state",
        "agent_working_state",
        "memory_brief",
        "capabilities",
        "constraints",
        "progress",
    }


def test_memory_view_and_new_values_are_strict() -> None:
    with pytest.raises(ValidationError, match="both be set or both be null"):
        MemoryView(database_id="db")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EnvironmentProfileRef.model_validate({"environment_id": "test", "version": "1", "seed": 1})


def test_trace_v3_payload_registry_is_complete_without_generic_entries() -> None:
    assert set(TRACE_PAYLOAD_MODELS) == {
        "run_started",
        "decision_trace",
        "episode_closed",
        "episode_committed",
        "run_failed",
        "run_finished",
        "learning_started",
        "lesson_evaluated",
        "lesson_activated",
        "learning_failed",
        "learning_finished",
    }
    assert len(set(TRACE_PAYLOAD_MODELS.values())) == len(TRACE_PAYLOAD_MODELS)
