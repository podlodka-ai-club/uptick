import hashlib

from uptick_agent.decisions.contracts import DecisionContext, ToolResult, V2NextStep
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.simulator.briefings import (
    V2_ENVIRONMENT_BRIEFING,
    V2_SYSTEM_PROMPT,
    v2_environment_briefing,
)


def test_default_briefing_and_system_prompt_keep_pre_opt_in_bytes() -> None:
    assert v2_environment_briefing() == V2_ENVIRONMENT_BRIEFING
    assert hashlib.sha256(V2_ENVIRONMENT_BRIEFING.encode()).hexdigest() == (
        "7c4d88f7b89644cccfa87af4827e5a5da3d5de0266367e9309ac9dcceb89196e"
    )
    assert hashlib.sha256(V2_SYSTEM_PROMPT.encode()).hexdigest() == (
        "3c160cc4d2495d2f6cbec2a3472acaeedd5837aa9429b5632d0cdc5fe4019580"
    )


def test_opt_in_permission_reaches_system_message_without_old_prohibition() -> None:
    class Client:
        model = "offline"

    model = StructuredDecisionModel(
        Client(),
        response_model=V2NextStep,
        environment_briefing=v2_environment_briefing(bounded_no_stop=True),
    )
    trace = model.prompt_trace(
        DecisionContext(
            objective="uptime",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )
    )
    system = trace["messages"][0]["content"]
    assert "bounded_no_stop_eligibility.eligible=true permits" in system
    assert "Do not make a blind wait with stop_when=null unless" not in system
    assert "maximum_duration_seconds" in system
    assert "does not predict healthy" in system
    assert "does not stop simulator billing" in system
    assert "Never attempt to obtain simulator source code" in system
