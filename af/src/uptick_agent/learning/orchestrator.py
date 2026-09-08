from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from uptick_agent.core.bootstrap_models import ToolRegistry
from uptick_agent.core.contracts import Memory, RunStore
from uptick_agent.core.errors import ReasonerFailure, RunStoreFailure
from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationQuery,
    LearningOperationResult,
    LearningTrigger,
    MemoryView,
)
from uptick_agent.core.models import FailureRecord
from uptick_agent.core.trace_models import (
    LearningEventKindV5,
    LearningFailedPayload,
    LearningFinishedPayload,
    LearningStartedPayload,
    LessonActivatedPayload,
    LessonEvaluatedPayload,
    TraceEvent,
    TracePayloadV5,
    learning_stream_id,
)
from uptick_agent.learning.consolidator import MemoryConsolidator
from uptick_agent.learning.gates import build_lesson_record, evaluate_lesson_proposal
from uptick_agent.runtime.episodes import learning_operation_id


class LearningOrchestrator:
    """Runs bounded consolidation without owning operational state."""

    def __init__(
        self,
        *,
        memory: Memory,
        run_store: RunStore,
        consolidator: MemoryConsolidator,
        trigger: LearningTrigger,
        min_evidence_groups: int,
        utcnow_fn=lambda: datetime.now(UTC),
    ) -> None:
        if min_evidence_groups < 1:
            raise ValueError("min_evidence_groups must be positive")
        if trigger == "after_closed_episode" and min_evidence_groups != 1:
            raise ValueError("after_closed_episode requires exactly one evidence group")
        self._memory = memory
        self._run_store = run_store
        self._consolidator = consolidator
        self._trigger: LearningTrigger = trigger
        self._min_evidence_groups = min_evidence_groups
        self._utcnow = utcnow_fn

    @property
    def trigger(self) -> LearningTrigger:
        return self._trigger

    async def consolidate(
        self,
        *,
        trigger_run_id: str,
        trigger_episode_id: str | None,
        memory_view: MemoryView,
        environment_id: str,
        environment_profile_version: str,
        registry: ToolRegistry,
    ) -> LearningOperationResult:
        operation_id = learning_operation_id(
            trigger=self.trigger,
            trigger_run_id=trigger_run_id,
            trigger_episode_id=trigger_episode_id,
            base_view=memory_view,
            environment_id=environment_id,
            environment_profile_version=environment_profile_version,
        )
        stream_id = learning_stream_id(operation_id)
        sequence = 0
        store_failed = False

        async def record(kind: LearningEventKindV5, payload: TracePayloadV5) -> None:
            nonlocal sequence, store_failed
            sequence += 1
            try:
                await self._run_store.record(
                    TraceEvent(
                        stream_id=stream_id,
                        stream_kind="learning",
                        sequence=sequence,
                        kind=kind,
                        payload=payload,
                    )
                )
            except Exception as error:
                store_failed = True
                raise RunStoreFailure("learning trace persistence failed") from error

        await record(
            "learning_started",
            LearningStartedPayload(
                learning_operation_id=operation_id,
                trigger=self.trigger,
                trigger_run_id=trigger_run_id,
                trigger_episode_id=trigger_episode_id,
                base_view=memory_view,
                started_at=self._utcnow(),
            ),
        )
        batch: ConsolidationBatch | None = None
        proposal = None
        telemetry = None
        gate_result = None
        commit = None
        selected_run_ids: list[str] = []
        selected_evidence_group_ids: list[str] = []
        try:
            batch = await self._memory.load_consolidation_batch(
                ConsolidationQuery(
                    trigger=self.trigger,
                    view=memory_view,
                    environment_id=environment_id,
                    environment_profile_version=environment_profile_version,
                    trigger_episode_id=trigger_episode_id,
                    min_evidence_groups=self._min_evidence_groups,
                )
            )
            if self.trigger == "after_closed_episode":
                if len(batch.episodes) != 1:
                    raise ValueError(
                        "inline learning must load exactly its trigger Episode; "
                        f"found {len(batch.episodes)}"
                    )
                inline_episode = batch.episodes[0]
                if inline_episode.record_id != trigger_episode_id:
                    raise ValueError("inline learning loaded a different trigger Episode")
                if inline_episode.run_id != trigger_run_id:
                    raise ValueError("inline trigger Episode does not belong to its trigger run")
            selected_run_ids = _ordered_unique(episode.run_id for episode in batch.episodes)
            selected_evidence_group_ids = _ordered_unique(
                episode.evidence_group_id for episode in batch.episodes
            )
            evidence_groups = {episode.evidence_group_id for episode in batch.episodes}
            if not batch.episodes or len(evidence_groups) < self._min_evidence_groups:
                reason = (
                    "no uncovered episodes"
                    if not batch.episodes
                    else "insufficient independent evidence groups"
                )
                result = LearningOperationResult(
                    operation_id=operation_id,
                    trigger=self.trigger,
                    trigger_run_id=trigger_run_id,
                    trigger_episode_id=trigger_episode_id,
                    selected_run_ids=selected_run_ids,
                    selected_evidence_group_ids=selected_evidence_group_ids,
                    start_view=memory_view,
                    end_view=memory_view,
                    skipped_reason=reason,
                )
                await record(
                    "learning_finished",
                    LearningFinishedPayload(
                        learning_operation_id=operation_id,
                        result=result,
                        finished_at=self._utcnow(),
                    ),
                )
                return result

            decision = await self._consolidator.consolidate(batch, registry)
            proposal = decision.proposal
            telemetry = decision.telemetry
            if decision.no_lesson is not None:
                result = LearningOperationResult(
                    operation_id=operation_id,
                    trigger=self.trigger,
                    trigger_run_id=trigger_run_id,
                    trigger_episode_id=trigger_episode_id,
                    selected_run_ids=selected_run_ids,
                    selected_evidence_group_ids=selected_evidence_group_ids,
                    start_view=memory_view,
                    end_view=memory_view,
                    learner_telemetry=telemetry,
                    skipped_reason=decision.no_lesson.reason,
                )
                await record(
                    "learning_finished",
                    LearningFinishedPayload(
                        learning_operation_id=operation_id,
                        result=result,
                        finished_at=self._utcnow(),
                    ),
                )
                return result
            if proposal is None:
                raise ValueError("learner returned neither a lesson nor no_lesson")
            gate_result = evaluate_lesson_proposal(
                batch=batch,
                proposal=proposal,
                registry=registry,
            )
            await record(
                "lesson_evaluated",
                LessonEvaluatedPayload(
                    learning_operation_id=operation_id,
                    batch=batch,
                    proposal=proposal,
                    gate_result=gate_result,
                ),
            )
            if not gate_result.accepted:
                result = LearningOperationResult(
                    operation_id=operation_id,
                    trigger=self.trigger,
                    trigger_run_id=trigger_run_id,
                    trigger_episode_id=trigger_episode_id,
                    selected_run_ids=selected_run_ids,
                    selected_evidence_group_ids=selected_evidence_group_ids,
                    start_view=memory_view,
                    end_view=memory_view,
                    proposal=proposal,
                    learner_telemetry=telemetry,
                    gate_result=gate_result,
                    skipped_reason="lesson proposal rejected",
                )
                await record(
                    "learning_finished",
                    LearningFinishedPayload(
                        learning_operation_id=operation_id,
                        result=result,
                        finished_at=self._utcnow(),
                    ),
                )
                return result

            if memory_view.revision is None:
                raise ValueError("learning requires a revisioned memory view")
            lesson = build_lesson_record(batch=batch, proposal=proposal)
            commit = await self._memory.activate_lesson(
                lesson,
                base_revision=memory_view.revision,
            )
            if commit.new_revision is None:
                raise ValueError("learning memory returned an unrevisioned commit")
            end_view = memory_view.model_copy(update={"revision": commit.new_revision})
            await record(
                "lesson_activated",
                LessonActivatedPayload(
                    learning_operation_id=operation_id,
                    lesson_id=lesson.record_id,
                    commit=commit,
                ),
            )
            result = LearningOperationResult(
                operation_id=operation_id,
                trigger=self.trigger,
                trigger_run_id=trigger_run_id,
                trigger_episode_id=trigger_episode_id,
                selected_run_ids=selected_run_ids,
                selected_evidence_group_ids=selected_evidence_group_ids,
                start_view=memory_view,
                end_view=end_view,
                proposal=proposal,
                learner_telemetry=telemetry,
                gate_result=gate_result,
                commit=commit,
            )
            await record(
                "learning_finished",
                LearningFinishedPayload(
                    learning_operation_id=operation_id,
                    result=result,
                    finished_at=self._utcnow(),
                ),
            )
            return result

        except Exception as error:
            if store_failed:
                raise
            failure = _failure_record(error)
            if batch is not None and proposal is None:
                await record(
                    "lesson_evaluated",
                    LessonEvaluatedPayload(
                        learning_operation_id=operation_id,
                        batch=batch,
                        failure=failure,
                    ),
                )
            await record(
                "learning_failed",
                LearningFailedPayload(
                    learning_operation_id=operation_id,
                    failure=failure,
                    last_durable_sequence=sequence,
                ),
            )
            result = LearningOperationResult(
                operation_id=operation_id,
                trigger=self.trigger,
                trigger_run_id=trigger_run_id,
                trigger_episode_id=trigger_episode_id,
                selected_run_ids=selected_run_ids,
                selected_evidence_group_ids=selected_evidence_group_ids,
                start_view=memory_view,
                end_view=memory_view,
                proposal=proposal,
                learner_telemetry=telemetry,
                gate_result=gate_result,
                commit=commit,
                failure=failure,
                skipped_reason="learning failed",
            )
            await record(
                "learning_finished",
                LearningFinishedPayload(
                    learning_operation_id=operation_id,
                    result=result,
                    finished_at=self._utcnow(),
                ),
            )
            return result


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _failure_record(error: Exception) -> FailureRecord:
    telemetry = error.telemetry if isinstance(error, ReasonerFailure) else None
    category = error.category if isinstance(error, ReasonerFailure) else None
    return FailureRecord(
        stage="learning",
        error_type=type(error).__name__,
        message=str(error),
        category=category,
        telemetry=telemetry,
    )
