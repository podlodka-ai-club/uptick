"""Legacy SRE state reduction kept at the simulator compatibility boundary."""

from __future__ import annotations

from uptick_agent.decisions.contracts import RunState, ToolResult
from uptick_agent.simulator.actions import (
    AgentAction,
    ApplyFix,
    GetOperation,
    ScaleBackend,
    StartDeployment,
)


def record_run_state(run_state: RunState, action: AgentAction, result: ToolResult) -> None:
    """Apply historical SRE action outcomes to the legacy run state."""

    for link in result.operation_links:
        if link.relation == "initiated" and result.ok:
            run_state.operation_statuses[link.operation_id] = "accepted"
        elif link.relation == "observed":
            status = result.data.get("status")
            if isinstance(status, str):
                run_state.operation_statuses[link.operation_id] = status

    if not result.ok:
        return
    if isinstance(action, ApplyFix) and result.data.get("applied") is True:
        if action.message not in run_state.applied_fix_messages:
            run_state.applied_fix_messages.append(action.message)
        return
    operation_id = result.data.get("operation_id")
    if isinstance(action, ScaleBackend) and isinstance(operation_id, str):
        run_state.desired_backend_instances = action.desired_instances
        run_state.operation_statuses[operation_id] = "accepted"
        return
    if isinstance(action, StartDeployment) and isinstance(operation_id, str):
        if action.deployment_id not in run_state.started_deployment_ids:
            run_state.started_deployment_ids.append(action.deployment_id)
        run_state.operation_statuses[operation_id] = "accepted"
        return
    if isinstance(action, GetOperation):
        status = result.data.get("status")
        resolved_operation_id = result.data.get("operation_id", action.operation_id)
        if isinstance(resolved_operation_id, str) and isinstance(status, str):
            run_state.operation_statuses[resolved_operation_id] = status


__all__ = ["record_run_state"]
