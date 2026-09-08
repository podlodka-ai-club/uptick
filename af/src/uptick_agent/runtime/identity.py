from __future__ import annotations

from uuid import UUID, uuid4


def allocate_ad_hoc_run_id(*, value: UUID | None = None) -> str:
    return f"run-{value or uuid4()}"
