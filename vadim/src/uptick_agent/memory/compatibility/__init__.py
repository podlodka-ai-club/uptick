"""Compatibility boundary for the pre-Stage-1 lexical memory protocol."""

from uptick_agent.memory.compatibility.legacy import (
    LegacyMemoryAdapter,
    LegacyMemoryRuntime,
    legacy_memory_runtime,
)

__all__ = ["LegacyMemoryAdapter", "LegacyMemoryRuntime", "legacy_memory_runtime"]
