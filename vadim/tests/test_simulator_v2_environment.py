import asyncio
from datetime import UTC, datetime, timedelta

from uptick_agent.memory.contracts import DecisionMemoryContext
from uptick_agent.models import (
    AgentConfig,
    ApplyFix,
    FinishRun,
    GetInbox,
    GetLogs,
    GetMetrics,
    GetOperation,
    NextStep,
    V2AdvanceTime,
    V2ProbePage,
)
from uptick_agent.runner import AgentRunner
from uptick_agent.simulator.v2_environment import SimulatorV2Environment, SimulatorV2Session
from uptick_agent.v2_actions import ControlCommand, ServerDeleteRequest


def _clock(*, remaining: int = 600, minute: int = 0) -> dict:
    now = datetime(2033, 3, 1, tzinfo=UTC) + timedelta(minutes=minute)
    return {
        "simulation_time": now.isoformat(),
        "simulation_ends_at": (now + timedelta(hours=1)).isoformat(),
        "remaining_seconds": remaining,
        "real_elapsed_seconds": 0,
        "applied_advance_seconds": 0,
    }


def _session() -> SimulatorV2Session:
    now = datetime(2033, 3, 1, tzinfo=UTC)
    return SimulatorV2Session(
        run_id="run-1",
        seed=42,
        agent_id="agent",
        agent_version="v2",
        status="running",
        simulation_time=now,
        logs_from=now,
    )


class FakeV2Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.log_call = 0
        self.inbox_call = 0

    async def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {
            "run_id": "run-1",
            "seed": kwargs["seed"],
            "agent_id": kwargs["agent_id"],
            "agent_version": kwargs["agent_version"],
            "status": "running",
            "simulation_time": "2033-03-01T00:00:00Z",
            "simulation_ends_at": "2033-03-01T01:00:00Z",
            "control_panel_auth": {
                "scheme": "basic",
                "username": "panel-user",
                "password": "panel-password",
                "instructions": "private",
            },
        }

    async def overview(self, run_id):
        self.calls.append(("overview", {"run_id": run_id}))
        return {
            "clock": _clock(),
            "run_id": run_id,
            "status": "running",
            "site_status": "healthy",
            "server_count": 2,
            "capacity_utilization": 0.2,
            "error_rate": 0,
            "availability": {
                "uptime_target": 0.99,
                "observed_seconds": 3600,
                "available_seconds": 3590,
                "downtime_seconds": 10,
                "uptime_ratio": 3590 / 3600,
                "slo_passed": None,
            },
            "costs": {
                "currency": "USD",
                "server_cost_minor": 12,
                "backup_storage_cost_minor": 3,
                "total_cost_minor": 15,
                "current_cost_per_hour_minor": 20,
            },
        }

    async def metrics(self, run_id):
        self.calls.append(("metrics", {"run_id": run_id}))
        return {
            "clock": _clock(),
            "current": {
                "uptime_ratio": 0.995,
                "downtime_seconds": 18,
                "observed_seconds": 3600,
                "available_seconds": 3582,
                "server_cost_minor": 10,
                "backup_storage_cost_minor": 2,
                "total_cost_minor": 12,
                "current_cost_per_hour_minor": 20,
            },
            "series": [],
        }

    async def logs(self, run_id, **kwargs):
        self.log_call += 1
        call = self.log_call
        self.calls.append(("logs", kwargs))
        return {
            "clock": _clock(minute=call),
            "logs": [
                {
                    "timestamp": f"2033-03-01T00:{call:02d}:00Z",
                    "request_id": f"request-{call}",
                    "source": "visitor",
                    "source_ip": f"192.0.2.{call}",
                    "user_agent": f"agent-{call}",
                    "region_code": "RU-SVE",
                    "page": "product_list",
                    "status": 500,
                    "load_units": 1,
                    "error": "SERVER_CAPACITY_EXCEEDED",
                    "message": "capacity",
                }
            ],
            "next_cursor": f"cursor-{call}" if call < 6 else None,
        }

    async def probe(self, run_id, **kwargs):
        self.calls.append(("probe", kwargs))
        return {
            "clock": _clock(),
            "request_id": kwargs["request_id"],
            "page": kwargs["page"],
            "product_id": kwargs.get("product_id"),
            "status": 503,
            "source_ip": "192.0.2.1",
            "user_agent": "probe",
            "region_code": "RU-SVE",
            "firewall_rule_id": None,
            "latency_ms": 2,
            "load_units": 1,
            "error": "SITE_STOPPED",
            "message": "stopped",
        }

    async def operation(self, run_id, operation_id):
        return {
            "clock": _clock(),
            "operation_id": operation_id,
            "type": "control_command",
            "command": "site.stop",
            "request_id": "command-1",
            "status": "failed",
            "progress": 0.5,
            "submitted_at": "2033-03-01T00:00:00Z",
            "result": None,
            "error": {"error": "TARGET_UNAVAILABLE", "message": "unavailable"},
        }

    async def inbox(self, run_id, **kwargs):
        self.inbox_call += 1
        self.calls.append(("inbox", kwargs))
        if self.inbox_call == 1:
            messages = [{"message_id": "message-1", "subject": "one", "description": "safe"}]
            next_cursor = "inbox-cursor-1"
        elif self.inbox_call == 2:
            messages = [
                {"message_id": "message-1", "subject": "one", "description": "safe"},
                {"message_id": "message-2", "subject": "two", "description": "password: hidden"},
            ]
            next_cursor = None
        else:
            messages = [
                {"message_id": "message-1", "subject": "one", "description": "safe"},
                {"message_id": "message-2", "subject": "two", "description": "safe"},
                {"message_id": "message-3", "subject": "three", "description": "future"},
            ]
            next_cursor = None
        return {
            "clock": _clock(minute=self.inbox_call),
            "messages": messages,
            "next_cursor": next_cursor,
        }

    async def advance_time(self, run_id, **kwargs):
        self.calls.append(("advance_time", kwargs))
        clock = _clock(minute=2)
        clock["applied_advance_seconds"] = 420
        return {
            "clock": clock,
            "previous_simulation_time": "2033-03-01T00:00:00Z",
            "requested_duration_seconds": kwargs["duration_seconds"],
            "processed_events": 4,
            "new_logs": 2,
            "stop_reason": "log_error",
        }


def test_start_and_metrics_are_sanitized_and_use_v2_objective_metrics() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session, started = await environment.start(
            seed=42,
            agent_id="agent",
            agent_version="v2",
            request_id="start-1",
        )
        assert session.run_id == "run-1"
        assert "password" not in started.data
        assert "control_panel_auth" not in started.data
        assert client.calls[0][1]["request_id"] == "start-1"

        result = await environment.execute(session, GetMetrics())
        assert [(item.name, item.unit, item.value) for item in result.objective_metrics] == [
            ("uptime_ratio", "ratio", 0.995),
            ("downtime_seconds", "seconds", 18.0),
            ("observed_seconds", "seconds", 3600.0),
            ("available_seconds", "seconds", 3582.0),
            ("total_cost_minor", "minor", 12.0),
            ("server_cost_minor", "minor", 10.0),
            ("backup_storage_cost_minor", "minor", 2.0),
            ("current_cost_per_hour_minor", "minor", 20.0),
        ]

    asyncio.run(scenario())


def test_logs_keep_pending_cursor_and_deduplicate_on_next_step() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        first = await environment.execute(session, GetLogs(status=500))
        assert first.data["truncated"] is True
        assert first.data["total_logs"] == 1
        assert len(first.data["logs"]) == 1
        assert first.data["logs"][0]["source_ip"] == "192.0.2.1"
        assert session.logs_cursor == "cursor-1"

        second = await environment.execute(session, GetLogs(status=500))
        assert second.data["truncated"] is True
        assert second.data["total_logs"] == 1
        assert session.logs_cursor == "cursor-2"
        for _ in range(4):
            second = await environment.execute(session, GetLogs(status=500))
        assert second.data["truncated"] is False
        assert session.logs_cursor is None
        assert session.logs_from == datetime(2033, 3, 1, 0, 6, tzinfo=UTC)
        assert [kwargs["cursor"] for name, kwargs in client.calls if name == "logs"] == [
            None,
            "cursor-1",
            "cursor-2",
            "cursor-3",
            "cursor-4",
            "cursor-5",
        ]

    asyncio.run(scenario())


def test_probe_logical_failure_and_operation_failure_do_not_claim_terminal_run() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        probe = await environment.execute(session, V2ProbePage(page="product_list"))
        assert probe.ok is False
        assert probe.terminal is False

        operation = await environment.execute(session, GetOperation(operation_id="op-1"))
        assert operation.ok is False
        assert operation.terminal is False
        assert operation.operation_links[0].relation == "observed"

    asyncio.run(scenario())


def test_inbox_cursor_deduplicates_and_later_reads_retain_future_messages() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()

        first = await environment.execute(session, GetInbox())
        assert first.data["truncated"] is True
        assert [item["message_id"] for item in first.data["messages"]] == ["message-1"]
        assert session.inbox_cursor == "inbox-cursor-1"

        second = await environment.execute(session, GetInbox())
        assert second.data["truncated"] is False
        assert [item["message_id"] for item in second.data["messages"]] == ["message-2"]
        assert "hidden" not in str(second.data)

        third = await environment.execute(session, GetInbox())
        assert [item["message_id"] for item in third.data["messages"]] == ["message-3"]

    asyncio.run(scenario())


def test_advance_uses_explicit_v2_stop_condition_and_reports_actual_clock_delta() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()
        result = await environment.execute(
            session,
            V2AdvanceTime(duration_seconds=900, stop_when=None),
        )
        assert "Advanced 420s" in result.summary
        assert "requested 900s" in result.summary
        assert client.calls[-1][1]["stop_when"] is None

    asyncio.run(scenario())


def test_finish_reads_authoritative_overview_and_preserves_running_status() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        session = _session()
        result = await environment.finish(
            session,
            steps=3,
            duration_seconds=12.5,
            stop_reason="agent chose to finish",
        )
        assert result.status == "running"
        assert result.objective_kind == "uptime_cost"
        assert result.uptime_ratio == 3590 / 3600
        assert result.slo_passed is None
        assert result.total_cost_minor == 15
        assert session.status == "running"

    asyncio.run(scenario())


def test_finish_action_is_rejected_while_run_is_running() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        result = await environment.execute(_session(), FinishRun(reason="finish early"))
        assert result.ok is False
        assert result.terminal is False
        assert "full simulation horizon" in result.summary
        assert "SLO has not been decided yet" in result.summary
        assert result.objective_metrics
        assert result.data["costs"]["total_cost_minor"] == 15

    asyncio.run(scenario())


def test_finish_action_is_terminal_only_after_authoritative_run_completion() -> None:
    class TerminalOverviewClient(FakeV2Client):
        def __init__(self, status: str) -> None:
            super().__init__()
            self.terminal_status = status

        async def overview(self, run_id):
            value = await super().overview(run_id)
            value["status"] = self.terminal_status
            value["clock"]["remaining_seconds"] = 0
            value["availability"]["uptime_ratio"] = 1.0
            value["availability"]["slo_passed"] = self.terminal_status == "completed"
            return value

    async def scenario() -> None:
        for status in ("completed", "failed"):
            result = await SimulatorV2Environment(TerminalOverviewClient(status)).execute(
                _session(), FinishRun(reason="finish complete")
            )
            assert result.ok is True
            assert result.terminal is True
            assert status in result.summary

    asyncio.run(scenario())


def test_runner_continues_after_early_finish_rejection_until_time_completes() -> None:
    class GuardClient(FakeV2Client):
        def __init__(self) -> None:
            super().__init__()
            self.overview_calls = 0

        async def overview(self, run_id):
            self.overview_calls += 1
            value = await super().overview(run_id)
            if self.overview_calls > 1:
                value["status"] = "completed"
                value["clock"]["remaining_seconds"] = 0
                value["availability"]["uptime_ratio"] = 1.0
                value["availability"]["slo_passed"] = True
            return value

        async def advance_time(self, run_id, **kwargs):
            value = await super().advance_time(run_id, **kwargs)
            value["clock"]["remaining_seconds"] = 0
            value["clock"]["applied_advance_seconds"] = 300
            return value

    class ScriptedModel:
        def __init__(self) -> None:
            self.calls = 0

        async def decide(self, context):
            self.calls += 1
            if self.calls == 1:
                return NextStep(
                    current_situation="the run is not yet complete",
                    hypothesis="finish must wait for the horizon",
                    remaining_steps=["advance time"],
                    task_completed=True,
                    action=FinishRun(reason="premature finish attempt"),
                )
            return NextStep(
                current_situation="advance to the end",
                hypothesis="the horizon will complete",
                remaining_steps=[],
                task_completed=False,
                action=V2AdvanceTime(duration_seconds=300, stop_when=None),
            )

    class Memory:
        def __init__(self) -> None:
            self.steps = []

        async def build_context(self, request):
            return DecisionMemoryContext()

        async def remember(self, entry):
            return None

        async def record_transition(self, transition):
            self.steps.append(transition)

        async def clear(self, run_id=None):
            return None

        async def finalize_run(self, outcome):
            return None

        async def record_trace(self, write):
            return None

        @property
        def context_diagnostics(self):
            return {}

    class Observer:
        def __init__(self) -> None:
            self.steps = []

        async def on_step(self, record):
            self.steps.append(record)

        async def on_finish(self, result):
            return None

    async def scenario() -> None:
        client = GuardClient()
        observer = Observer()
        result = await AgentRunner(
            config=AgentConfig(agent_id="guard-test", agent_version="v2", max_steps=3),
            model=ScriptedModel(),
            memory=Memory(),
            environment=SimulatorV2Environment(client),
            observer=observer,
        ).run(42)
        assert result.status == "completed"
        assert result.steps == 2
        assert observer.steps[0].decision.action.kind == "finish"
        assert observer.steps[0].result.ok is False
        assert observer.steps[0].result.terminal is False
        assert observer.steps[1].decision.action.kind == "advance_time_v2"
        assert observer.steps[1].result.terminal is True
        assert client.overview_calls == 2

    asyncio.run(scenario())


def test_unsupported_legacy_mutation_is_local_and_never_calls_v1_wire() -> None:
    async def scenario() -> None:
        client = FakeV2Client()
        environment = SimulatorV2Environment(client)  # type: ignore[arg-type]
        result = await environment.execute(_session(), ApplyFix(message="fix"))
        assert result.ok is False
        assert result.data["code"] == "UNSUPPORTED_V2_ACTION"
        assert not client.calls

    asyncio.run(scenario())


def test_typed_control_command_uses_request_params_and_initiates_operation() -> None:
    async def scenario() -> None:
        client = FakeV2Client()

        async def execute_command(run_id, **kwargs):
            client.calls.append(("execute_command", kwargs))
            return {
                "clock": _clock(),
                "request_id": kwargs["request_id"],
                "command": kwargs["command"],
                "operation_id": "operation-1",
                "status": "accepted",
            }

        client.execute_command = execute_command
        action = ControlCommand(
            request=ServerDeleteRequest(
                command="server.delete",
                params={"server_id": "backend-1"},
            )
        )
        result = await SimulatorV2Environment(client).execute(_session(), action)
        assert result.ok is True
        assert result.operation_links[0].relation == "initiated"
        assert client.calls[-1][1]["params"] == {"server_id": "backend-1"}

    asyncio.run(scenario())
