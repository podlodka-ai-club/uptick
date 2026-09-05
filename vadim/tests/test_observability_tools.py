from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from uptick_agent.decisions.contracts import V2NextStep
from uptick_agent.environment.contracts import EnvironmentDecisionSpec, validate_decision
from uptick_agent.simulator.actions import QueryLogs, QueryMetrics
from uptick_agent.simulator.decisions import SimulatorV2Decision
from uptick_agent.simulator.v2_client import SimulatorV2ApiError, SimulatorV2Client
from uptick_agent.simulator.v2_environment import SimulatorV2Environment, SimulatorV2Session

RUN_ID = "run-observability"
START = datetime(2033, 3, 1, tzinfo=UTC)
END = datetime(2033, 3, 1, 1, tzinfo=UTC)


def _clock() -> dict[str, object]:
    return {
        "simulation_time": END.isoformat(),
        "simulation_ends_at": "2033-03-02T00:00:00+00:00",
        "remaining_seconds": 86_400,
        "real_elapsed_seconds": 0,
        "applied_advance_seconds": 0,
    }


def _session() -> SimulatorV2Session:
    return SimulatorV2Session(
        run_id=RUN_ID,
        seed=42,
        agent_id="agent",
        agent_version="v2",
        status="running",
        simulation_time=START,
        logs_from=START,
        logs_initial_from=START,
    )


def _logs_response() -> dict[str, object]:
    return {
        "clock": _clock(),
        "logs": [
            {
                "timestamp": END.isoformat(),
                "request_id": "request-1",
                "source": "visitor",
                "source_ip": "203.0.113.10",
                "user_agent": "shop-bot",
                "region_code": "US",
                "firewall_rule_id": None,
                "page": "product_page",
                "status": 500,
                "load_units": 1,
                "error": "SERVER_CAPACITY_EXCEEDED",
            }
        ],
        "next_cursor": "cursor-2",
        "window": {"from": START.isoformat(), "to": END.isoformat()},
    }


def _metrics_response() -> dict[str, object]:
    return {"clock": _clock(), "current": {"uptime_ratio": 0.995}, "series": []}


def _client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(base_url="http://simulator.test", transport=transport)
    return SimulatorV2Client(http_client=http_client), http_client


def test_canonical_v2_decision_adds_queries_without_changing_legacy_schema() -> None:
    decision = SimulatorV2Decision.model_validate(
        {
            "current_situation": "inspect a historical error window",
            "hypothesis": "capacity errors are concentrated in one page",
            "remaining_steps": [],
            "task_completed": False,
            "action": {
                "kind": "query_logs",
                "from": START.isoformat(),
                "to": END.isoformat(),
                "page": "product_page",
                "has_error": True,
                "limit": 25,
            },
        }
    )

    assert isinstance(decision.action, QueryLogs)
    assert "query_logs" in str(SimulatorV2Decision.model_json_schema())
    assert "query_metrics" in str(SimulatorV2Decision.model_json_schema())
    assert "query_logs" not in str(V2NextStep.model_json_schema())
    schema = str(SimulatorV2Decision.model_json_schema())
    assert "ipv4network" not in schema
    assert "ipv6network" not in schema


def test_query_logs_accepts_canonical_ipv4_and_ipv6_cidrs() -> None:
    assert QueryLogs(source_cidr="203.0.113.0/24").source_cidr == "203.0.113.0/24"
    assert QueryLogs(source_cidr="2001:db8::/32").source_cidr == "2001:db8::/32"

    with pytest.raises(ValidationError):
        QueryLogs(source_cidr="203.0.113.1/24")
    with pytest.raises(ValidationError):
        QueryLogs(source_cidr="not-a-network")


def test_query_timestamps_preserve_nanoseconds_from_decision_to_http() -> None:
    requests: list[httpx.Request] = []
    raw_from = "2030-01-14T04:13:50.524467912Z"
    raw_to = "2030-01-14T04:13:50.524467913Z"

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/logs"):
                return httpx.Response(200, json=_logs_response())
            return httpx.Response(200, json=_metrics_response())

        client, http_client = _client(handler)
        try:
            spec = EnvironmentDecisionSpec(
                response_model=SimulatorV2Decision,
                environment_briefing="public startup instructions",
            )
            environment = SimulatorV2Environment(client)
            session = _session()
            for kind in ("query_logs", "query_metrics"):
                decision = SimulatorV2Decision.model_validate(
                    {
                        "current_situation": "inspect an exact public time window",
                        "hypothesis": "the boundary may contain one event",
                        "remaining_steps": [],
                        "task_completed": False,
                        "action": {
                            "kind": kind,
                            "from": raw_from,
                            "to": raw_to,
                        },
                    }
                )
                validated = validate_decision(spec, decision)
                action = validated.action
                assert action.from_time == raw_from
                assert action.to_time == raw_to
                assert action.model_dump(mode="json", by_alias=True)["from"] == raw_from
                await environment.execute(session, action)

            assert dict(requests[0].url.params)["from"] == raw_from
            assert dict(requests[0].url.params)["to"] == raw_to
            assert dict(requests[1].url.params)["from"] == raw_from
            assert dict(requests[1].url.params)["to"] == raw_to
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_query_timestamps_reject_one_nanosecond_reversed_windows() -> None:
    later = "2030-01-14T04:13:50.524467913Z"
    earlier = "2030-01-14T04:13:50.524467912Z"
    for action_type in (QueryLogs, QueryMetrics):
        with pytest.raises(ValidationError, match="from must not be later"):
            action_type.model_validate({"from": later, "to": earlier})

    offset_equivalent = "2030-01-14T05:13:50.524467912+01:00"
    with pytest.raises(ValidationError, match="from must not be later"):
        QueryLogs.model_validate({"from": later, "to": offset_equivalent})


def test_client_rejects_one_nanosecond_reversed_windows_before_http() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_metrics_response())

        client, http_client = _client(handler)
        try:
            for method in (client.query_logs, client.query_metrics):
                with pytest.raises(SimulatorV2ApiError, match="from must not be later"):
                    await method(
                        RUN_ID,
                        from_time="2030-01-14T04:13:50.524467913Z",
                        to_time="2030-01-14T04:13:50.524467912Z",
                    )
            assert requests == []
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_v2_environment_publishes_canonical_decision_schema_after_start() -> None:
    class Client:
        async def start(self, **kwargs):
            return {
                "run_id": RUN_ID,
                "status": "running",
                "simulation_time": START.isoformat(),
                "commands_markdown": "public startup instructions",
            }

    async def scenario() -> None:
        environment = SimulatorV2Environment(Client())
        with pytest.raises(RuntimeError, match="has not started"):
            _ = environment.decision_spec
        await environment.start(seed=42, agent_id="agent", agent_version="v2")
        assert environment.decision_spec.response_model is SimulatorV2Decision

    asyncio.run(scenario())


def test_query_logs_forwards_all_filters_and_preserves_incremental_state() -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_logs_response())

        client, http_client = _client(handler)
        try:
            environment = SimulatorV2Environment(client)
            session = _session()
            session.logs_from_by_status["500"] = START
            session.logs_cursor = "incremental-cursor"
            session.logs_cursor_status = 500
            session.logs_cursor_by_status["500"] = "incremental-cursor"
            session.seen_log_ids.add("already-seen")
            before = (
                dict(session.logs_from_by_status),
                dict(session.logs_cursor_by_status),
                session.logs_cursor,
                session.logs_cursor_status,
                set(session.seen_log_ids),
            )

            action = QueryLogs(
                from_time=START,
                to_time=END,
                page="product_page",
                status=500,
                has_error=False,
                source_ip="203.0.113.10",
                source_cidr="203.0.113.0/24",
                user_agent="shop-bot",
                region_code="US",
                firewall_rule_id="allow-shop",
                cursor="history-cursor",
                limit=37,
            )
            first = await environment.execute(session, action)
            second = await environment.execute(session, action)

            params = dict(requests[0].url.params)
            assert params == {
                "from": START.isoformat(),
                "to": END.isoformat(),
                "page": "product_page",
                "status": "500",
                "has_error": "false",
                "source_ip": "203.0.113.10",
                "source_cidr": "203.0.113.0/24",
                "user_agent": "shop-bot",
                "region_code": "US",
                "firewall_rule_id": "allow-shop",
                "cursor": "history-cursor",
                "limit": "37",
            }
            assert first.data == second.data == _logs_response()
            assert first.data["next_cursor"] == "cursor-2"
            assert (
                dict(session.logs_from_by_status),
                dict(session.logs_cursor_by_status),
                session.logs_cursor,
                session.logs_cursor_status,
                set(session.seen_log_ids),
            ) == before
            assert len(requests) == 2
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_query_logs_omits_optional_parameters_but_keeps_false() -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_logs_response())

        client, http_client = _client(handler)
        try:
            await client.query_logs(RUN_ID, has_error=False)
            assert dict(requests[0].url.params) == {"has_error": "false", "limit": "100"}
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_query_logs_supports_one_sided_bounds_and_filter_isolation() -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            response = _logs_response()
            if request.url.params.get("status") == "200":
                response["logs"] = [
                    {
                        **response["logs"][0],
                        "request_id": "request-success",
                        "status": 200,
                        "error": None,
                    }
                ]
            return httpx.Response(200, json=response)

        client, http_client = _client(handler)
        try:
            environment = SimulatorV2Environment(client)
            session = _session()
            before = (
                dict(session.logs_from_by_status),
                dict(session.logs_cursor_by_status),
                session.logs_cursor,
                session.logs_cursor_status,
                set(session.seen_log_ids),
            )
            first = await environment.execute(
                session,
                QueryLogs(from_time=START, status=500, limit=7),
            )
            second = await environment.execute(
                session,
                QueryLogs(to_time=END, status=200, limit=7),
            )

            assert dict(requests[0].url.params) == {
                "from": START.isoformat(),
                "status": "500",
                "limit": "7",
            }
            assert dict(requests[1].url.params) == {
                "to": END.isoformat(),
                "status": "200",
                "limit": "7",
            }
            assert first.data["logs"][0]["request_id"] == "request-1"
            assert second.data["logs"][0]["request_id"] == "request-success"
            assert (
                dict(session.logs_from_by_status),
                dict(session.logs_cursor_by_status),
                session.logs_cursor,
                session.logs_cursor_status,
                set(session.seen_log_ids),
            ) == before
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


def test_query_metrics_forwards_window_step_names_and_page() -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_metrics_response())

        client, http_client = _client(handler)
        try:
            result = await SimulatorV2Environment(client).execute(
                _session(),
                QueryMetrics(
                    from_time=START,
                    to_time=END,
                    step_seconds=15,
                    names=["uptime_ratio", "error_rate"],
                    page="product_page",
                ),
            )
            assert result.data == _metrics_response()
            assert dict(requests[0].url.params) == {
                "from": START.isoformat(),
                "to": END.isoformat(),
                "step_seconds": "15",
                "names": "uptime_ratio,error_rate",
                "page": "product_page",
            }
            await client.query_metrics(RUN_ID)
            assert dict(requests[1].url.params) == {"step_seconds": "60"}
        finally:
            await http_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"from_time": START.replace(tzinfo=None)},
        {"to_time": END.replace(tzinfo=None)},
        {"from_time": END, "to_time": START},
        {"has_error": False, "error": "DISK_FULL"},
        {"source_cidr": "203.0.113.1/24"},
    ],
)
def test_invalid_query_params_are_rejected_before_http_execution(kwargs) -> None:
    with pytest.raises(ValidationError):
        QueryLogs(**kwargs)

    with pytest.raises(ValidationError):
        SimulatorV2Decision.model_validate(
            {
                "current_situation": "inspect",
                "hypothesis": "invalid query must not execute",
                "remaining_steps": [],
                "task_completed": False,
                "action": {"kind": "query_logs", **kwargs},
            }
        )


def test_query_metrics_rejects_invalid_ranges_before_http_execution() -> None:
    with pytest.raises(ValidationError):
        QueryMetrics(from_time=START)
    with pytest.raises(ValidationError):
        QueryMetrics(from_time=START, to_time=END, step_seconds=0)
    with pytest.raises(ValidationError):
        QueryMetrics(from_time=START, to_time=END, names=[])
