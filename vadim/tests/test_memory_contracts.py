import math

import pytest
from pydantic import ValidationError

import uptick_agent.memory as memory_api
import uptick_agent.memory.stores as store_api
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ContextItem,
    ExperienceTransition,
    MemoryContextRequest,
    ObjectiveMetric,
    ProvenanceRef,
    RunOutcome,
    TransitionAssemblyRequest,
    UntrustedMemoryEnvelope,
)

_HASH = "a" * 64


def test_stage_one_contracts_are_available_from_the_public_package_api() -> None:
    expected_memory_symbols = {
        "ConsolidationDelta",
        "ConsolidationParticipant",
        "ConsolidationRequest",
        "ConsolidationResult",
        "ContextItem",
        "ContextContributor",
        "DecisionMemoryContext",
        "ExperienceSink",
        "ExperienceTransition",
        "ExperienceTransitionAssembler",
        "MemoryConflictError",
        "MemoryContractError",
        "MemoryContextRequest",
        "MemoryContribution",
        "MemoryPermanentError",
        "MemoryTransientError",
        "MemoryValidationError",
        "ObjectiveMetric",
        "ProvenanceRef",
        "RunOutcome",
        "RunFinalizer",
        "TransitionAssemblyRequest",
        "UntrustedMemoryEnvelope",
    }

    assert expected_memory_symbols <= set(memory_api.__all__)
    assert all(hasattr(memory_api, symbol) for symbol in expected_memory_symbols)
    assert "SnapshotMember" in store_api.__all__
    assert store_api.SnapshotMember is not None


def _provenance() -> list[ProvenanceRef]:
    return [ProvenanceRef(artefact_id="source-1", content_hash=_HASH)]


def _envelope() -> UntrustedMemoryEnvelope:
    return UntrustedMemoryEnvelope(
        item_id="item-1",
        artefact_type="episode",
        origin_module="test",
        origin_version="1.0",
        trust_classification="external_untrusted",
        provenance=_provenance(),
        item={"summary": "untrusted content"},
    )


def _transition_fields() -> dict[str, object]:
    return {
        "transition_id": "transition-1",
        "run_id": "run-1",
        "iteration": 1,
        "trust_classification": "external_untrusted",
        "provenance": _provenance(),
        "terminal": False,
    }


def test_contracts_reject_unknown_schema_major_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="unsupported schema major"):
        MemoryContextRequest(request_id="request", run_id="run", schema_version="2.0")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MemoryContextRequest(request_id="request", run_id="run", unexpected=True)


def test_contracts_ignore_only_forward_minor_additions_from_supported_major() -> None:
    decoded = MemoryContextRequest.model_validate(
        {
            "schema_version": "1.1",
            "request_id": "request",
            "run_id": "run",
            "future_additive_field": "ignored by 1.0 reader",
        }
    )

    assert decoded.schema_version == "1.1"
    assert "future_additive_field" not in decoded.model_dump()

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MemoryContextRequest.model_validate(
            {
                "schema_version": "1.0",
                "request_id": "request",
                "run_id": "run",
                "future_additive_field": "not valid when authoring 1.0",
            }
        )


def test_transition_is_generic_and_requires_no_simulator_models() -> None:
    transition = ExperienceTransition(
        transition_id="transition-1",
        run_id="run-1",
        iteration=1,
        observation={"message": "untrusted environment result"},
        action={"kind": "inspect"},
        result={"ok": True},
        trust_classification="external_untrusted",
        provenance=_provenance(),
        terminal=False,
    )

    assert transition.action == {"kind": "inspect"}


def test_prompt_and_transition_lifecycle_facts_are_required_and_closed() -> None:
    with pytest.raises(ValidationError):
        UntrustedMemoryEnvelope(
            item_id="item-1",
            artefact_type="episode",
            origin_module="test",
            origin_version="1.0",
            provenance=_provenance(),
            item={"summary": "missing trust classification"},
        )
    with pytest.raises(ValidationError):
        UntrustedMemoryEnvelope(
            item_id="item-1",
            artefact_type="episode",
            origin_module="test",
            origin_version="1.0",
            trust_classification="external_untrusted",
            item={"summary": "missing provenance"},
        )
    with pytest.raises(ValidationError):
        UntrustedMemoryEnvelope(
            item_id="item-1",
            artefact_type="episode",
            origin_module="test",
            origin_version="1.0",
            trust_classification="external_untrusted",
            provenance=_provenance(),
            item={},
        )
    with pytest.raises(ValidationError):
        ExperienceTransition(
            **{
                key: value
                for key, value in _transition_fields().items()
                if key != "trust_classification"
            }
        )
    with pytest.raises(ValidationError):
        ExperienceTransition(
            **{key: value for key, value in _transition_fields().items() if key != "provenance"}
        )
    with pytest.raises(ValidationError):
        ExperienceTransition(
            **{key: value for key, value in _transition_fields().items() if key != "terminal"}
        )
    with pytest.raises(ValidationError):
        ExperienceTransition(
            iteration=0,
            **{key: value for key, value in _transition_fields().items() if key != "iteration"},
        )
    with pytest.raises(ValidationError):
        TransitionAssemblyRequest(transition_id="transition-1", run_id="run-1", iteration=1)
    with pytest.raises(ValidationError):
        TransitionAssemblyRequest(
            transition_id="transition-1", run_id="run-1", iteration=0, terminal=False
        )

    outcome = RunOutcome(run_id="run-1", status="completed", stop_reason="finished")
    assert outcome.terminal is True
    with pytest.raises(ValidationError):
        RunOutcome(run_id="run-1", status="unknown", stop_reason="finished")
    with pytest.raises(ValidationError):
        RunOutcome(run_id="run-1", status="failed", stop_reason="finished", terminal=False)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_contracts_reject_non_finite_floats_and_recursive_json_payloads(invalid: float) -> None:
    with pytest.raises(ValidationError):
        ObjectiveMetric(name="metric", value=invalid, unit="count")
    with pytest.raises(ValidationError):
        ContextItem(
            envelope=_envelope(),
            score=invalid,
            selection_reason="test",
            estimated_tokens=1,
        )
    with pytest.raises(ValidationError, match="NaN or infinity"):
        MemoryContextRequest(
            request_id="request",
            run_id="run",
            context={"nested": [{"bad": invalid}]},
        )


def test_legacy_baseline_config_is_canonical_and_fingerprint_is_stable() -> None:
    first = MemoryConfiguration.legacy_baseline()
    second = MemoryConfiguration.legacy_baseline()

    assert first.compatibility_legacy.enabled is True
    assert first.compatibility_legacy.schema_version == "1.1"
    assert first.context_budget.schema_version == "1.1"
    assert first.context_budget.estimator_id == "utf8-byte-upper-bound"
    assert first.episodic.enabled is False
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64


def test_config_rejects_invalid_dependencies_and_unapproved_defaults() -> None:
    with pytest.raises(ValidationError, match="world_model requires episodic or lessons"):
        MemoryConfiguration(world_model=ModuleConfig(enabled=True))

    with pytest.raises(ValidationError, match="requires approval_record_id"):
        ModuleConfig(status="default")

    with pytest.raises(ValidationError, match="cannot enable experimental module"):
        MemoryConfiguration(profile_kind="default")
