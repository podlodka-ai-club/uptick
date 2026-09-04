from __future__ import annotations

import asyncio
import json
from pathlib import Path

from uptick_agent.models import RunResult, StepRecord
from uptick_agent.redaction import sanitize_json


class NullObserver:
    async def on_step(self, record: StepRecord) -> None:
        return None

    async def on_finish(self, result: RunResult) -> None:
        return None


class ConsoleObserver:
    async def on_step(self, record: StepRecord) -> None:
        action = record.decision.action.kind
        marker = "ok" if record.result.ok else "error"
        print(f"step={record.iteration} action={action} result={marker} {record.result.summary}")

    async def on_finish(self, result: RunResult) -> None:
        if result.objective_kind == "uptime_cost":
            print(
                f"run={result.run_id} status={result.status} steps={result.steps} "
                f"uptime_ratio={result.uptime_ratio} slo_passed={result.slo_passed} "
                f"total_cost_minor={result.total_cost_minor}"
            )
        else:
            print(
                f"run={result.run_id} status={result.status} steps={result.steps} "
                f"balance_minor={result.balance_minor}"
            )


class JsonlObserver:
    """Append-only experiment trace suitable for later analysis or replay."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def on_step(self, record: StepRecord) -> None:
        await self._write({"event": "step", "data": record.model_dump(mode="json")})

    async def on_finish(self, result: RunResult) -> None:
        await self._write({"event": "run_finished", "data": result.model_dump(mode="json")})

    async def _write(self, payload: dict) -> None:
        line = (
            json.dumps(
                sanitize_json(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as target:
            target.write(line)


class CompositeObserver:
    def __init__(self, *observers) -> None:
        self.observers = observers

    async def on_step(self, record: StepRecord) -> None:
        for observer in self.observers:
            await observer.on_step(record)

    async def on_finish(self, result: RunResult) -> None:
        for observer in self.observers:
            await observer.on_finish(result)
