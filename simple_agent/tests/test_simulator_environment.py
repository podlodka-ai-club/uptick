import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.models import GetLogs
from uptick_agent.simulator.environment import SimulatorEnvironment, SimulatorSession
from uptick_agent.simulator.models import Clock, LogsResponse, RequestLog


class FakePagedLogsClient:
    def __init__(self) -> None:
        self.cursors = []
        self.calls = 0

    async def logs(self, run_id, *, from_time, status, cursor, limit=200):
        self.cursors.append(cursor)
        self.calls += 1
        now = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=self.calls)
        next_cursor = f"cursor-{self.calls}" if self.calls < 6 else None
        return LogsResponse(
            clock=Clock(
                simulation_time=now,
                simulation_ends_at=datetime(2026, 9, 1, tzinfo=UTC),
                remaining_seconds=100,
                real_elapsed_seconds=0,
                applied_advance_seconds=0,
            ),
            logs=[
                RequestLog(
                    timestamp=now,
                    request_id=f"request-{self.calls}",
                    source="visitor",
                    page="product_list",
                    status=500,
                    load_units=1,
                    error="PAGE_BUG",
                )
            ],
            next_cursor=next_cursor,
        )


def test_log_pagination_continues_without_losing_entries() -> None:
    async def scenario() -> None:
        client = FakePagedLogsClient()
        environment = SimulatorEnvironment(client)  # type: ignore[arg-type]
        start = datetime(2026, 8, 1, tzinfo=UTC)
        session = SimulatorSession(
            run_id="run",
            seed=1,
            agent_id="agent",
            agent_version="v1",
            status="running",
            simulation_time=start,
            logs_from=start,
        )

        first = await environment.execute(session, GetLogs(status=500))
        assert first.data["truncated"] is True
        assert first.data["total_logs"] == 5
        assert first.data["logs"][0]["occurrences"] == 5
        assert session.logs_from == start
        assert session.logs_cursor == "cursor-5"

        second = await environment.execute(session, GetLogs(status=500))
        assert second.data["truncated"] is False
        assert session.logs_cursor is None
        assert session.logs_from == datetime(2026, 8, 1, 0, 6, tzinfo=UTC)
        assert client.cursors == [None, "cursor-1", "cursor-2", "cursor-3", "cursor-4", "cursor-5"]

    asyncio.run(scenario())
