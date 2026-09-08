from __future__ import annotations

from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationQuery,
    EpisodeRecord,
    LessonRecord,
    MemoryCommit,
    MemoryPacket,
    MemoryQuery,
    MemoryView,
    MemoryViewRequest,
)


class NoMemory:
    """Control backend that intentionally retains and recalls nothing."""

    async def resolve_view(self, request: MemoryViewRequest) -> MemoryView:
        del request
        return MemoryView()

    async def recall(self, query: MemoryQuery) -> MemoryPacket:
        return MemoryPacket(view=query.view)

    async def load_consolidation_batch(
        self,
        query: ConsolidationQuery,
    ) -> ConsolidationBatch:
        return ConsolidationBatch(query=query)

    async def record_episode(
        self,
        episode: EpisodeRecord,
        base_revision: int,
    ) -> MemoryCommit:
        del base_revision
        return MemoryCommit(
            applied=False,
            record_ids=[episode.record_id],
            reason="NoMemory is read-only and does not retain episodes",
        )

    async def activate_lesson(
        self,
        lesson: LessonRecord,
        base_revision: int,
    ) -> MemoryCommit:
        del base_revision
        return MemoryCommit(
            applied=False,
            record_ids=[lesson.record_id],
            reason="NoMemory is read-only and does not retain lessons",
        )
