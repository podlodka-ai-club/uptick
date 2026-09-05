from __future__ import annotations

from collections.abc import Callable, Iterable
from statistics import fmean, median

from uptick_agent.runs.execute import AgentRunner
from uptick_agent.runs.results import ExperimentResult


class ExperimentRunner:
    """Runs comparable seeds; memory carry-over is always an explicit choice."""

    def __init__(self, runner_factory: Callable[[], AgentRunner]) -> None:
        self.runner_factory = runner_factory

    async def run(
        self,
        *,
        name: str,
        seeds: Iterable[int],
        carry_memory: bool = False,
    ) -> ExperimentResult:
        seed_list = list(seeds)
        if not seed_list:
            raise ValueError("at least one seed is required")
        if 0 in seed_list:
            raise ValueError("simulator seed 0 is invalid")

        shared_runner = self.runner_factory() if carry_memory else None
        runs = []
        for seed in seed_list:
            runner = shared_runner or self.runner_factory()
            if not carry_memory:
                await runner.memory.clear()
            runs.append(await runner.run(seed))

        objective_kind = runs[0].objective_kind
        if any(run.objective_kind != objective_kind for run in runs):
            raise ValueError("all runs in an experiment must use the same objective")
        balances = [run.balance_minor for run in runs]
        if objective_kind == "uptime_cost":
            slo_passed_runs = sum(
                run.status == "completed" and run.slo_passed is True for run in runs
            )
            successful_costs = [
                run.total_cost_minor
                for run in runs
                if (
                    run.status == "completed"
                    and run.slo_passed is True
                    and run.total_cost_minor is not None
                )
            ]
            return ExperimentResult(
                name=name,
                runs=runs,
                objective_kind=objective_kind,
                completed_runs=sum(run.status == "completed" for run in runs),
                slo_passed_runs=slo_passed_runs,
                mean_successful_total_cost_minor=(
                    fmean(successful_costs) if successful_costs else None
                ),
            )
        return ExperimentResult(
            name=name,
            runs=runs,
            objective_kind=objective_kind,
            mean_balance_minor=fmean(balances),
            median_balance_minor=median(balances),
            min_balance_minor=min(balances),
            max_balance_minor=max(balances),
            completed_runs=sum(run.status == "completed" for run in runs),
        )
