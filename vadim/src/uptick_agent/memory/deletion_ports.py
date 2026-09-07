"""Neutral protocol for the optional physical-deletion store extension."""

from __future__ import annotations

from typing import Protocol

from uptick_agent.memory.deletion_contracts import (
    PhysicalDeletionPlan,
    PhysicalDeletionReceipt,
    StoreInventory,
)


class DeletionStore(Protocol):
    async def deletion_inventory(self) -> StoreInventory: ...

    async def apply_deletion(
        self, plan: PhysicalDeletionPlan, *, idempotency_key: str
    ) -> PhysicalDeletionReceipt: ...
