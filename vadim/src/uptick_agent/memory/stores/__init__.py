"""Stable structured-store contracts with lazy store implementations."""

from importlib import import_module

from uptick_agent.memory.stores.contracts import (
    MemorySnapshot,
    RecordWrite,
    SnapshotMember,
    SnapshotReceipt,
    StoredRecord,
    StructuredMemoryStore,
    WriteReceipt,
)

__all__ = [
    "InMemoryStructuredStore",
    "MemorySnapshot",
    "RecordWrite",
    "SnapshotMember",
    "SnapshotReceipt",
    "SqliteStructuredStore",
    "StoredRecord",
    "StructuredMemoryStore",
    "WriteReceipt",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "InMemoryStructuredStore": ("in_memory", "InMemoryStructuredStore"),
    "SqliteStructuredStore": ("sqlite", "SqliteStructuredStore"),
}


def __getattr__(name: str):
    """Load persistence implementations only when their exports are used."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value
