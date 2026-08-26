from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from uptick_agent.models import (
    AgentConfig,
    DecisionContext,
    MemoryEntry,
    MemoryQuery,
    RunResult,
    StepRecord,
    ToolResult,
)
from uptick_agent.observers import NullObserver
from uptick_agent.ports import DecisionModel, Environment, Memory, RunObserver


def _memory_text(result: ToolResult, *, limit: int = 6_000) -> str:
    payload = result.model_dump_json()
    if len(payload) <= limit:
        return payload
    return payload[:limit] + f"\n...[{len(payload) - limit} characters omitted]"


class AgentRunner:
    """Small orchestration core: recall -> SGR decision -> one action -> remember."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        model: DecisionModel,
        memory: Memory,
        environment: Environment,
        observer: RunObserver | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.memory = memory
        self.environment = environment
        self.observer = observer or NullObserver()

    async def run(self, seed: int) -> RunResult:
        run_started = monotonic()
        session, latest = await self.environment.start(
            seed=seed,
            agent_id=self.config.agent_id,
            agent_version=self.config.agent_version,
        )
        await self._remember(session.run_id, latest, kind="observation", importance=0.7)

        stop_reason = "maximum step limit reached"
        completed_steps = 0
        for iteration in range(1, self.config.max_steps + 1):
            memories = await self.memory.recall(
                MemoryQuery(
                    text=latest.summary + " " + _memory_text(latest, limit=4000),
                    run_id=session.run_id,
                    include_other_runs=True,
                    limit=self.config.memory_recall_limit,
                )
            )
            context = DecisionContext(
                objective=self.config.objective,
                run_id=session.run_id,
                seed=seed,
                iteration=iteration,
                max_steps=self.config.max_steps,
                latest_result=latest,
                recalled_memories=memories,
            )
            step_started = monotonic()
            decision = await self.model.decide(context)
            result = await self.environment.execute(session, decision.action)
            duration = monotonic() - step_started
            completed_steps = iteration
            record = StepRecord(
                run_id=session.run_id,
                iteration=iteration,
                decision=decision,
                result=result,
                started_at=datetime.now(UTC),
                duration_seconds=duration,
            )
            await self._remember(
                session.run_id,
                result,
                kind="experience",
                importance=0.85 if not result.ok or result.terminal else 0.5,
                metadata={"iteration": iteration, "decision": decision.model_dump(mode="json")},
            )
            await self.observer.on_step(record)
            latest = result
            if result.terminal:
                stop_reason = result.summary
                break

        total_duration = monotonic() - run_started
        final = await self.environment.finish(
            session,
            steps=completed_steps,
            duration_seconds=total_duration,
            stop_reason=stop_reason,
        )
        await self._remember(
            session.run_id,
            ToolResult(
                action_kind="run_outcome",
                summary=(
                    f"Run finished with status={final.status}, balance={final.balance_minor}, "
                    f"lost_revenue={final.lost_revenue_minor}, steps={final.steps}."
                ),
                data=final.model_dump(mode="json"),
                terminal=True,
            ),
            kind="outcome",
            importance=1.0,
            tags={"run-outcome"},
        )
        await self.observer.on_finish(final)
        return final

    async def _remember(
        self,
        run_id: str,
        result: ToolResult,
        *,
        kind: str,
        importance: float,
        tags: set[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        await self.memory.remember(
            MemoryEntry(
                id=uuid4().hex,
                run_id=run_id,
                kind=kind,
                content=_memory_text(result),
                importance=importance,
                tags=(tags or set()) | {result.action_kind},
                metadata=metadata or {},
            )
        )
