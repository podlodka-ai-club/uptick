from tests.helpers import inspect_catalog, make_context
from uptick_agent.core.models import (
    AgentContext,
    CapabilityCall,
    ReasoningTelemetry,
    RunSpec,
    SGRDecision,
    SGREnvelope,
    VerificationAssessment,
)
from uptick_agent.core.policy import DecisionPolicy


def _context(*, terminal: bool = False, iteration: int = 1) -> AgentContext:
    catalog = inspect_catalog(terminal_finish=True)
    if not terminal:
        finish = catalog.find("finish")
        assert finish is not None
        finish.terminal = False
    return make_context(
        objective="keep healthy",
        step=iteration,
        step_limit=3,
        capabilities=catalog,
    )


def _decision(name: str, arguments: dict, *, completed: bool) -> SGRDecision:
    return SGRDecision(
        envelope=SGREnvelope(
            phase="finish" if completed else "observe",
            facts=["known state"],
            competing_hypotheses=["test action"],
            contradicting_evidence=[],
            previous_verification=VerificationAssessment(status="not_applicable"),
            strategy="test the selected action",
            selected_action=CapabilityCall(name=name, arguments=arguments),
            expected_result=["the action returns an observation"],
            verification=["inspect whether the observation is successful"],
            task_completed=completed,
        ),
        telemetry=ReasoningTelemetry(
            provider="test",
            requested_model="test",
            thread_mode="stateless",
            duration_seconds=0,
            sdk_name="test",
            sdk_version="1",
        ),
    )


def test_policy_requires_completion_to_match_terminal_capability() -> None:
    context = _context(terminal=True)

    rejected = DecisionPolicy().validate(context, _decision("inspect", {}, completed=True))
    accepted = DecisionPolicy().validate(
        context, _decision("finish", {"reason": "done"}, completed=True)
    )

    assert not rejected.accepted
    assert accepted.accepted


def test_policy_rejects_unknown_and_invalid_capability_arguments() -> None:
    context = _context(terminal=True)

    unknown = DecisionPolicy().validate(context, _decision("missing", {}, completed=False))
    invalid = DecisionPolicy().validate(context, _decision("finish", {}, completed=True))

    assert unknown.violations == ["unknown capability 'missing'"]
    assert "arguments.reason is required" in invalid.violations


def test_run_defaults_are_environment_neutral_and_do_not_limit_world_progress() -> None:
    spec = RunSpec(run_id="run-defaults")
    assert spec.environment == "generic"
    assert spec.step_limit is None
