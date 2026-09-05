"""Extensible SGR agent with lazy top-level compatibility exports.

Importing a focused submodule should not eagerly construct the runner and its
optional memory implementations.  The historical top-level names are loaded
on demand when callers request them.
"""

from importlib import import_module

__all__ = ["AgentConfig", "AgentRunner", "RunResult"]


_LAZY_EXPORTS = {
    "AgentConfig": ("models", "AgentConfig"),
    "RunResult": ("models", "RunResult"),
    "AgentRunner": ("runner", "AgentRunner"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
