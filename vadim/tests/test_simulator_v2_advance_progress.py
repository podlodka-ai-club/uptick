import asyncio
import json
import math
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from uptick_agent.simulator.actions import GetOperation, V2AdvanceTime
from uptick_agent.simulator.v2_client import SimulatorV2ApiError
from uptick_agent.simulator.v2_environment import (
    SimulatorV2Environment,
    SimulatorV2Session,
    _advance_progress,
)

OPERATION_ID = "Ab12Cd34Ef56Gh78"


def _session(run_id: str = "run-1") -> SimulatorV2Session:
    return SimulatorV2Session(
        run_id=run_id,
        seed=42,
        agent_id="agent",
        agent_version="test",
        status="running",
        simulation_time=datetime(2033, 1, 1, tzinfo=UTC),
        logs_from=None,
    )


def _completed(
    *,
    requested: object = 300,
    applied: object = 120,
    stop_reason: object = "log_error",
    minute: int = 2,
) -> dict[str, object]:
    return {
        "clock": {
            "simulation_time": f"2033-01-01T00:{minute:02d}:00Z",
            "simulation_ends_at": "2033-01-01T01:00:00Z",
            "remaining_seconds": 3600 - minute * 60,
            "applied_advance_seconds": applied,
        },
        "requested_duration_seconds": requested,
        "processed_events": 4,
        "new_logs": 1,
        "stop_reason": stop_reason,
    }


class AsyncClient:
    def __init__(self, statuses: list[str], result: dict[str, object] | None = None) -> None:
        self.statuses = iter(statuses)
        self.result = result

    async def operation(self, run_id: str, operation_id: str) -> dict[str, object]:
        status = next(self.statuses)
        return {
            "run_id": run_id,
            "request_id": "advance-request-1",
            "operation_id": operation_id,
            "type": "time.advance",
            "status": status,
            "submitted_at": "2033-01-01T00:00:00Z",
            "started_at": "2033-01-01T00:00:01Z",
            "completed_at": "2033-01-01T00:02:00Z" if status in {"succeeded", "failed"} else None,
            "result": deepcopy(self.result) if status == "succeeded" else None,
            **({"error": {"error": "FAILED", "message": "failed"}} if status == "failed" else {}),
        }


class SyncClient:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = iter(results)

    async def advance_time(self, run_id: str, **kwargs: object) -> dict[str, object]:
        return deepcopy(next(self.results))


class FailingSyncClient:
    async def advance_time(self, run_id: str, **kwargs: object) -> dict[str, object]:
        raise SimulatorV2ApiError(503, "UNAVAILABLE", "unavailable")


def test_completed_async_progress_is_visible_once_with_exact_public_facts() -> None:
    async def scenario() -> None:
        state = _session()
        client = AsyncClient(["running", "succeeded", "succeeded"], _completed())
        env = SimulatorV2Environment(client)  # type: ignore[arg-type]
        pending = await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert pending.ok and "advance_progress" not in env.public_state(state)

        completed = await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert "advanced 120s of requested 300s" in completed.summary
        progress = env.public_state(state)["advance_progress"]
        assert isinstance(progress, list)
        assert progress == [
            {
                "status": "completed",
                "requested_seconds": 300,
                "applied_seconds": 120,
                "applied_ratio": 0.4,
                "stop_reason": "log_error",
                "source_kind": "public_time_advance_operation",
                "source_id": OPERATION_ID,
                "observed_at": "2033-01-01T00:02:00Z",
            }
        ]
        await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert len(env.public_state(state)["advance_progress"]) == 1  # type: ignore[arg-type]

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_unfinished_or_failed_operation_has_no_completed_progress(status: str) -> None:
    state = _session()
    result = asyncio.run(
        SimulatorV2Environment(AsyncClient([status], _completed())).execute(  # type: ignore[arg-type]
            state, GetOperation(operation_id=OPERATION_ID)
        )
    )
    assert result.ok is (status != "failed")
    assert "advance_progress" not in SimulatorV2Environment(AsyncClient([])).public_state(state)  # type: ignore[arg-type]


def test_failed_advance_tool_result_does_not_create_progress() -> None:
    state = _session()
    env = SimulatorV2Environment(FailingSyncClient())  # type: ignore[arg-type]
    result = asyncio.run(env.execute(state, V2AdvanceTime(duration_seconds=300)))
    assert not result.ok
    assert "advance_progress" not in env.public_state(state)


@pytest.mark.parametrize(
    ("requested", "applied", "reason"),
    [
        (True, 1, "invalid_requested_seconds"),
        (0, 0, "nonpositive_requested_seconds"),
        (300, True, "invalid_applied_seconds"),
        (300, -1, "out_of_range_applied_seconds"),
        (300, 301, "out_of_range_applied_seconds"),
    ],
)
def test_malformed_progress_is_skipped_and_marked_without_failing_result(
    requested: object, applied: object, reason: str
) -> None:
    async def scenario() -> None:
        state = _session()
        client = AsyncClient(["succeeded"], _completed(requested=requested, applied=applied))
        env = SimulatorV2Environment(client)  # type: ignore[arg-type]
        result = await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert result.ok and "metadata is unavailable" in result.summary
        progress = env.public_state(state)["advance_progress"]
        assert isinstance(progress, list)
        assert progress == [{"status": "invalid", "reason": reason, "source_id": OPERATION_ID}]

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_nonfinite_progress_is_rejected_by_extractor(value: float) -> None:
    progress = _advance_progress(
        "get_operation",
        {
            "type": "time.advance",
            "status": "succeeded",
            "operation_id": OPERATION_ID,
            "result": _completed(applied=value),
        },
    )
    assert progress == {
        "status": "invalid",
        "reason": "invalid_applied_seconds",
        "source_id": OPERATION_ID,
    }


def test_oversized_integer_is_marked_without_raising() -> None:
    async def scenario() -> None:
        state = _session()
        env = SimulatorV2Environment(  # type: ignore[arg-type]
            AsyncClient(["succeeded"], _completed(requested=10**400, applied=120.0))
        )
        result = await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert result.ok and "metadata is unavailable" in result.summary
        assert env.public_state(state)["advance_progress"] == [
            {
                "status": "invalid",
                "reason": "invalid_requested_seconds",
                "source_id": OPERATION_ID,
            }
        ]

    asyncio.run(scenario())


def test_succeeded_status_deduplicates_after_progress_entry_is_evicted() -> None:
    async def scenario() -> None:
        state = _session()
        state.operation_statuses[OPERATION_ID] = "succeeded"
        env = SimulatorV2Environment(AsyncClient(["succeeded"], _completed()))  # type: ignore[arg-type]
        result = await env.execute(state, GetOperation(operation_id=OPERATION_ID))
        assert result.ok
        assert "advance_progress" not in env.public_state(state)

    asyncio.run(scenario())


def test_sync_progress_is_bounded_by_whole_entries_and_public_state_is_owned() -> None:
    async def scenario() -> None:
        results = [
            _completed(
                requested=300,
                applied=120,
                stop_reason=f"reason-{index}-" + "x" * 400,
                minute=index,
            )
            for index in range(1, 11)
        ]
        state = _session()
        other = _session("run-2")
        env = SimulatorV2Environment(SyncClient(results))  # type: ignore[arg-type]
        for _ in results:
            response = await env.execute(state, V2AdvanceTime(duration_seconds=300))
            assert response.ok
        public = env.public_state(state)
        progress = public["advance_progress"]
        assert isinstance(progress, list)
        assert 0 < len(progress) <= 8
        encoded = json.dumps(
            {"advance_progress": progress}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        assert len(encoded) <= 2500
        assert all(item["stop_reason"].endswith("x" * 400) for item in progress)
        progress.clear()
        assert env.public_state(state)["advance_progress"]
        assert "advance_progress" not in env.public_state(other)

    asyncio.run(scenario())


def test_single_oversized_progress_entry_is_skipped_with_bounded_marker() -> None:
    async def scenario() -> None:
        state = _session()
        env = SimulatorV2Environment(SyncClient([_completed(stop_reason="x" * 3000)]))  # type: ignore[arg-type]
        result = await env.execute(state, V2AdvanceTime(duration_seconds=300))
        assert result.ok
        progress = env.public_state(state)["advance_progress"]
        assert isinstance(progress, list)
        assert len(progress) == 1
        assert progress[0]["status"] == "invalid"
        assert progress[0]["reason"] == "progress_entry_over_budget"
        assert isinstance(progress[0]["source_id"], str)
        encoded = json.dumps({"advance_progress": progress}, separators=(",", ":")).encode()
        assert len(encoded) <= 2500

    asyncio.run(scenario())
