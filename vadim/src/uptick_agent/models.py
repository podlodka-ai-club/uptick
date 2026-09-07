"""Lazy compatibility facade for the historical ``uptick_agent.models`` module."""

# The names are populated by ``__getattr__`` to keep this compatibility module
# from eagerly loading every canonical contract module.
# ruff: noqa: F822

from importlib import import_module

__all__ = [
    "StrictModel",
    "AgentConfig",
    "GetOverview",
    "GetMetrics",
    "GetLogs",
    "V1GetLogs",
    "GetResources",
    "GetDeployments",
    "ScaleBackend",
    "ApplyFix",
    "StartDeployment",
    "GetOperation",
    "ProbePage",
    "AdvanceTime",
    "AdvanceTimeStopCondition",
    "V2ProbePage",
    "V2AdvanceTime",
    "FinishRun",
    "AgentAction",
    "V1AgentAction",
    "V2AgentAction",
    "ControlCommand",
    "GetInbox",
    "GetControlCommands",
    "NextStep",
    "V1NextStep",
    "V2NextStep",
    "ToolResult",
    "MemoryEntry",
    "MemoryQuery",
    "MemoryMatch",
    "RecentStep",
    "RunState",
    "DecisionContext",
    "StepRecord",
    "RunResult",
    "ExperimentResult",
]


_EXPORTS = {
    "StrictModel": ("_model_base", "StrictModel"),
    "AgentConfig": ("runs.config", "AgentConfig"),
    "GetOverview": ("decisions.actions", "GetOverview"),
    "GetMetrics": ("decisions.actions", "GetMetrics"),
    "GetLogs": ("decisions.actions", "GetLogs"),
    "V1GetLogs": ("decisions.actions", "V1GetLogs"),
    "GetResources": ("decisions.actions", "GetResources"),
    "GetDeployments": ("decisions.actions", "GetDeployments"),
    "ScaleBackend": ("decisions.actions", "ScaleBackend"),
    "ApplyFix": ("decisions.actions", "ApplyFix"),
    "StartDeployment": ("decisions.actions", "StartDeployment"),
    "GetOperation": ("decisions.actions", "GetOperation"),
    "ProbePage": ("decisions.actions", "ProbePage"),
    "AdvanceTime": ("decisions.actions", "AdvanceTime"),
    "AdvanceTimeStopCondition": ("decisions.actions", "AdvanceTimeStopCondition"),
    "V2ProbePage": ("decisions.actions", "V2ProbePage"),
    "V2AdvanceTime": ("decisions.actions", "V2AdvanceTime"),
    "FinishRun": ("decisions.actions", "FinishRun"),
    "AgentAction": ("decisions.actions", "AgentAction"),
    "V1AgentAction": ("decisions.actions", "V1AgentAction"),
    "V2AgentAction": ("decisions.actions", "V2AgentAction"),
    "ControlCommand": ("v2_actions", "ControlCommand"),
    "GetInbox": ("v2_actions", "GetInbox"),
    "GetControlCommands": ("v2_actions", "GetControlCommands"),
    "NextStep": ("decisions.contracts", "NextStep"),
    "V1NextStep": ("decisions.contracts", "V1NextStep"),
    "V2NextStep": ("decisions.contracts", "V2NextStep"),
    "ToolResult": ("decisions.contracts", "ToolResult"),
    "MemoryEntry": ("memory.compatibility.contracts", "MemoryEntry"),
    "MemoryQuery": ("memory.compatibility.contracts", "MemoryQuery"),
    "MemoryMatch": ("memory.compatibility.contracts", "MemoryMatch"),
    "RecentStep": ("decisions.contracts", "RecentStep"),
    "RunState": ("decisions.contracts", "RunState"),
    "DecisionContext": ("decisions.contracts", "DecisionContext"),
    "StepRecord": ("runs.results", "StepRecord"),
    "RunResult": ("runs.results", "RunResult"),
    "ExperimentResult": ("runs.results", "ExperimentResult"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__.rpartition('.')[0]}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
