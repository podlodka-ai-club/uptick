"""Dependency-injection protocols for evaluation use cases."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Literal, Protocol

from uptick_agent.evaluation.contracts import (
    FrozenEvaluationBinding,
    V2AttemptRecord,
    V2Condition,
    V2RunMatrixBlock,
)
from uptick_agent.ports import AgentMemory, DecisionModel, Environment
from uptick_agent.runs.config import AgentConfig


class EvaluationEnvironmentFactory(Protocol):
    def __call__(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> Environment | Awaitable[Environment]: ...


class EvaluationModelFactory(Protocol):
    def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
    ) -> DecisionModel | Awaitable[DecisionModel]: ...


class EvaluationMemoryFactory(Protocol):
    def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
        phase: Literal["training", "evaluation"],
        binding: FrozenEvaluationBinding | None,
    ) -> AgentMemory | Awaitable[AgentMemory]: ...


class EvaluationBindingFactory(Protocol):
    def __call__(
        self,
        condition: V2Condition,
        training_attempts: tuple[V2AttemptRecord, ...],
    ) -> FrozenEvaluationBinding | Awaitable[FrozenEvaluationBinding]: ...


class EvaluationConfigFactory(Protocol):
    def __call__(
        self, block: V2RunMatrixBlock, condition: V2Condition, attempt: V2AttemptRecord
    ) -> AgentConfig | Awaitable[AgentConfig]: ...
