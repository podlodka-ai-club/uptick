"""Acceptance check across the runner, composition, persistence and validator."""

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path

from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.memory.lesson_contracts import LessonRunDeclaration, LessonSettings
from uptick_agent.memory.lesson_runtime import lessons_memory_runtime
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.memory.stores.contracts import sha256_json
from uptick_agent.models import AgentConfig, ApplyFix, NextStep, RunResult, ToolResult
from uptick_agent.runner import AgentRunner


@dataclass
class _Session:
    run_id: str
    seed: int
    environment_id: str
    scenario_id: str


class _Environment:
    def __init__(self, declaration: LessonRunDeclaration, delta: int, status: str):
        self.declaration = declaration
        self.delta = delta
        self.status = status

    async def start(self, *, seed, agent_id, agent_version):
        return _Session(
            self.declaration.run_id,
            seed,
            self.declaration.environment_id,
            self.declaration.scenario_id,
        ), ToolResult(
            action_kind="start",
            summary="service pressure",
            objective_metrics=[ObjectiveMetric(name="health", value=50, unit="points")],
        )

    async def execute(self, session, action):
        return ToolResult(
            action_kind=action.kind,
            summary="measured outcome",
            data={"applied": True},
            objective_metrics=[
                ObjectiveMetric(name="health", value=50 + self.delta, unit="points")
            ],
            terminal=True,
        )

    async def finish(self, session, *, steps, duration_seconds, stop_reason):
        return RunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="lesson-acceptance",
            agent_version="1.0",
            status=self.status,
            steps=steps,
            duration_seconds=duration_seconds,
            stop_reason=stop_reason,
        )


class _Model:
    def __init__(self):
        self.contexts = []

    async def decide(self, context):
        self.contexts.append(context)
        return NextStep(
            current_situation="service pressure",
            hypothesis="the measured action may help",
            remaining_steps=["apply action"],
            task_completed=False,
            action=ApplyFix(message="restore service capacity"),
        )


def test_runner_learns_across_runs_ignores_evaluation_and_demotes_on_counterexample(tmp_path):
    async def scenario():
        declarations = [
            LessonRunDeclaration(
                run_id=f"run-{index}",
                logical_run_id=f"logical-{index}",
                attempt_index=0,
                phase="frozen_evaluation" if index == 3 else "learning",
                eligible=index != 3,
                environment_id="acceptance-environment",
                scenario_id=f"scenario-{index}",
                environment_content_hash=sha256_json({"environment": "acceptance"}),
                scenario_content_hash=sha256_json({"failure_mode": f"mode-{index}"}),
            )
            for index in range(1, 6)
        ]
        configuration = MemoryConfiguration.episodic_with_lessons(
            lesson_settings=LessonSettings(
                metric_name="health",
                metric_unit="points",
                direction="maximize",
                condition_keys=("summary",),
            )
        )
        # Persist episodes while reserving decision context for the module under test.
        configuration.episodic.max_context_items = 0
        configuration.context_budget.total_tokens = 16_000
        configuration.lessons.max_context_tokens = 16_000
        lessons_by_run = []
        for declaration, delta in zip(declarations, (10, 10, -10, -10, 10), strict=True):
            status = "failed" if declaration.run_id == "run-4" else "completed"
            model = _Model()
            runtime = lessons_memory_runtime(
                SqliteStructuredStore(tmp_path / "runner-lessons.sqlite"),
                episodic_namespace="runner-episodes",
                lesson_namespace="runner-lessons",
                run_declarations=declarations,
                configuration=configuration,
            )
            result = await AgentRunner(
                model=model,
                memory=runtime,
                environment=_Environment(declaration, delta, status),
                config=AgentConfig(max_steps=1),
            ).run(seed=42)
            assert result.status == status
            lessons_by_run.append([
                item for item in model.contexts[0].memory_context.items
                if item.envelope.origin_module == "lessons"
            ])

        assert [len(items) for items in lessons_by_run] == [0, 0, 1, 1, 0]
        assert lessons_by_run[2][0].envelope.trust_classification == "derived_untrusted"
        assert lessons_by_run[2][0].envelope.item_id == lessons_by_run[3][0].envelope.item_id

    asyncio.run(scenario())


def test_lesson_core_imports_only_generic_boundaries():
    package = Path(__file__).parents[1] / "src" / "uptick_agent" / "memory"
    forbidden = (
        "uptick_agent.simulator",
        "uptick_agent.llm",
        "uptick_agent.stage0",
        "uptick_agent.runner",
        "uptick_agent.memory.episodic",
        "uptick_agent.memory.stores.sqlite",
        "uptick_agent.memory.stores.in_memory",
        "openai",
        "httpx",
    )
    for name in (
        "lessons.py", "lesson_contracts.py", "candidate_validation.py", "lesson_evidence.py"
    ):
        tree = ast.parse((package / name).read_text())
        imports = [
            module
            for node in ast.walk(tree)
            for module in (
                [node.module or ""] if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else []
            )
        ]
        assert not [module for module in imports if module.startswith(forbidden)], name
