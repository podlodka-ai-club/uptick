"""Lazy compatibility facade for historical simulator action imports."""

# ruff: noqa: F822

from importlib import import_module

__all__ = [
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
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    value = getattr(import_module("uptick_agent.simulator.actions"), name)
    globals()[name] = value
    return value
