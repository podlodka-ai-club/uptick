"""Compatibility boundary for the pre-Stage-1 lexical memory protocol.

Legacy concrete memory is loaded only when one of its compatibility names is
requested.  Contract-only imports therefore do not instantiate or import the
legacy implementation eagerly.
"""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uptick_agent.memory.compatibility.legacy import (
        LegacyMemoryAdapter,
        LegacyMemoryRuntime,
        legacy_memory_runtime,
    )

__all__ = ["LegacyMemoryAdapter", "LegacyMemoryRuntime", "legacy_memory_runtime"]


def __getattr__(name: str):
    if name in __all__:
        module = import_module("uptick_agent.memory.compatibility.legacy")
        return getattr(module, name)
    raise AttributeError(name)
