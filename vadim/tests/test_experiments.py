import asyncio

import pytest

from uptick_agent.experiments import ExperimentRunner
from uptick_agent.models import RunResult


class _Memory:
    def __init__(self) -> None:
        self.clears = 0
        self.entries: list[str] = []

    async def clear(self, run_id=None) -> None:
        self.clears += 1
        self.entries.clear()


class _Runner:
    def __init__(self) -> None:
        self.memory = _Memory()
        self.seeds = []
        self.seen_memory: list[list[str]] = []

    async def run(self, seed: int) -> RunResult:
        self.seeds.append(seed)
        self.seen_memory.append(list(self.memory.entries))
        self.memory.entries.append(f"result-{seed}")
        return RunResult(
            run_id=f"run-{seed}",
            seed=seed,
            agent_id="test",
            agent_version="v1",
            status="completed",
            steps=1,
            duration_seconds=0,
            balance_minor=seed * 10,
            stop_reason="done",
        )


def test_experiments_isolate_or_carry_one_memory_runtime_explicitly() -> None:
    async def scenario() -> None:
        isolated = []

        def isolated_factory() -> _Runner:
            runner = _Runner()
            isolated.append(runner)
            return runner

        result = await ExperimentRunner(isolated_factory).run(name="isolated", seeds=[1, 3])
        assert len(isolated) == 2
        assert [runner.memory.clears for runner in isolated] == [1, 1]
        assert [runner.seen_memory for runner in isolated] == [[[]], [[]]]
        assert [runner.memory.entries for runner in isolated] == [["result-1"], ["result-3"]]
        assert result.mean_balance_minor == 20
        assert result.median_balance_minor == 20

        carried = []

        def carried_factory() -> _Runner:
            runner = _Runner()
            carried.append(runner)
            return runner

        await ExperimentRunner(carried_factory).run(name="carried", seeds=[1, 3], carry_memory=True)
        assert len(carried) == 1
        assert carried[0].memory.clears == 0
        assert carried[0].seeds == [1, 3]
        assert carried[0].seen_memory == [[], ["result-1"]]
        assert carried[0].memory.entries == ["result-1", "result-3"]

    asyncio.run(scenario())


def test_experiments_reject_empty_and_zero_seed_sets_before_running() -> None:
    async def scenario() -> None:
        experiment = ExperimentRunner(_Runner)
        with pytest.raises(ValueError, match="at least one seed"):
            await experiment.run(name="empty", seeds=[])
        with pytest.raises(ValueError, match="seed 0"):
            await experiment.run(name="zero", seeds=[1, 0])

    asyncio.run(scenario())
