import asyncio
import hashlib
import json

from tests.helpers import decode_prompt_context, make_context
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.models import AgentContext, SGREnvelope
from uptick_agent.core.policy import validate_json_schema
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.reasoners.scripted import ScriptedReasoner


def _context(environment: str = "fake") -> AgentContext:
    return make_context(
        environment=environment,
        objective="test portability",
        step_limit=3,
    )


def test_agent_core_accepts_only_values_and_calls_reasoner() -> None:
    async def scenario() -> None:
        reasoner = ScriptedReasoner(
            [
                {
                    "phase": "observe",
                    "facts": ["the run is ready"],
                    "competing_hypotheses": [],
                    "contradicting_evidence": [],
                    "previous_verification": {"status": "not_applicable", "evidence": []},
                    "strategy": "inspect before changing state",
                    "selected_action": {"name": "inspect", "arguments": {}},
                    "expected_result": ["inspection returns current state"],
                    "verification": ["the observation contains current state"],
                    "task_completed": False,
                }
            ]
        )
        core = AgentCore(reasoner=reasoner, sgr=CurrentSGR())

        decision = await core.decide(_context())

        assert decision.envelope.selected_action.name == "inspect"
        assert decision.envelope.competing_hypotheses == []
        assert (
            validate_json_schema(
                decision.envelope.model_dump(mode="json"), reasoner.requests[0].output_schema, "$"
            )
            == []
        )
        assert decision.telemetry.provider == "scripted"
        assert len(reasoner.requests) == 1

    asyncio.run(scenario())


def test_same_agent_core_accepts_contexts_from_two_environment_shapes() -> None:
    async def scenario() -> None:
        reasoner = ScriptedReasoner(
            [
                {
                    "phase": "observe",
                    "facts": ["environment a is ready"],
                    "competing_hypotheses": ["inspect"],
                    "contradicting_evidence": [],
                    "previous_verification": {"status": "not_applicable", "evidence": []},
                    "strategy": "inspect",
                    "selected_action": {"name": "inspect", "arguments": {}},
                    "expected_result": ["state observed"],
                    "verification": ["observation is returned"],
                    "task_completed": False,
                },
                {
                    "phase": "observe",
                    "facts": ["environment b is ready"],
                    "competing_hypotheses": ["inspect"],
                    "contradicting_evidence": [],
                    "previous_verification": {"status": "not_applicable", "evidence": []},
                    "strategy": "inspect",
                    "selected_action": {"name": "inspect", "arguments": {}},
                    "expected_result": ["state observed"],
                    "verification": ["observation is returned"],
                    "task_completed": False,
                },
            ]
        )
        core = AgentCore(reasoner=reasoner, sgr=CurrentSGR())

        first = await core.decide(_context("alpha"))
        second = await core.decide(_context("beta"))

        assert first.envelope.selected_action == second.envelope.selected_action
        assert (
            decode_prompt_context(reasoner.requests[0].user_prompt)["environment_profile"][
                "environment_id"
            ]
            == "alpha"
        )
        assert (
            decode_prompt_context(reasoner.requests[1].user_prompt)["environment_profile"][
                "environment_id"
            ]
            == "beta"
        )

    asyncio.run(scenario())


def test_agent_core_fingerprints_exact_prompt_input_and_normalized_schema() -> None:
    context = _context()
    sgr = CurrentSGR()
    core = AgentCore(reasoner=ScriptedReasoner([]), sgr=sgr)
    request = sgr.build_request(context)

    fingerprint = core.fingerprint(context)

    assert (
        fingerprint.system_prompt_sha256
        == hashlib.sha256(request.system_prompt.encode("utf-8")).hexdigest()
    )
    assert (
        fingerprint.context_sha256
        == hashlib.sha256(context.model_dump_json(indent=2).encode("utf-8")).hexdigest()
    )
    normalized = json.dumps(
        normalized_output_schema(request.output_schema),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert hashlib.sha256(normalized.encode("utf-8")).hexdigest() == (
        "da6a1e6e1fb732e1f3fe5f64ff498ce56408a887b35afc1ac8715ecacef75a89"
    )
    assert fingerprint.schema_sha256 == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert "metadata" not in context.model_dump()


def test_sgr_assesses_prior_outcome_before_planning_in_both_schemas() -> None:
    request = CurrentSGR().build_request(_context())
    properties = request.output_schema["properties"]
    assert isinstance(properties, dict)
    fields = list(properties)

    assert fields == list(SGREnvelope.model_fields)
    assert fields == request.output_schema["required"]
    assert fields == normalized_output_schema(request.output_schema)["required"]
    assert fields[0] == "previous_verification"
    assert fields.index("strategy") < fields.index("phase") < fields.index("selected_action")


def test_custom_sgr_prompt_always_includes_hashed_encoding_instructions() -> None:
    from uptick_agent.core.prompt_serialization import CONTEXT_FORMAT_INSTRUCTIONS

    sgr = CurrentSGR(system_prompt="Custom generic decision instructions.")
    context = _context()
    request = sgr.build_request(context)
    assert (
        request.system_prompt
        == "Custom generic decision instructions.\n\n" + CONTEXT_FORMAT_INSTRUCTIONS
    )
    fingerprint = AgentCore(reasoner=ScriptedReasoner([]), sgr=sgr).fingerprint(context)
    assert (
        fingerprint.system_prompt_sha256
        == hashlib.sha256(request.system_prompt.encode()).hexdigest()
    )
