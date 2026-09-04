import asyncio
import base64
import json
from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest

from uptick_agent.simulator.v2_client import SimulatorV2ApiError, SimulatorV2Client

RUN_ID = "R" * 20
PANEL_USER = "panel-user"
PANEL_PASSWORD = "panel-password"
SERVER_USER = "server-user"
SERVER_PASSWORD = "server-password"
COMMANDS = [
    "firewall.rules.list",
    "firewall.rules.upsert",
    "firewall.rules.delete",
    "server.types.list",
    "server.create",
    "server.inspect",
    "server.delete",
    "database.create",
    "database.inspect",
    "database.backup",
    "database.backups.list",
    "database.restore",
    "site.config.get",
    "site.stop",
    "site.start",
    "site.database.set",
    "disk.usage",
    "disk.cleanup",
]
TARGET_COMMANDS = {
    "database.create",
    "database.inspect",
    "database.backup",
    "database.restore",
    "disk.usage",
    "disk.cleanup",
}
ASYNC_COMMANDS = {
    "server.create",
    "server.delete",
    "database.backup",
    "database.restore",
    "site.stop",
}


def _clock() -> dict[str, object]:
    return {
        "simulation_time": "2030-01-01T00:00:00Z",
        "simulation_ends_at": "2030-01-08T00:00:00Z",
        "remaining_seconds": 604800,
        "real_elapsed_seconds": 0,
        "applied_advance_seconds": 0,
    }


def _start_body() -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "seed": 42,
        "agent_id": "agent",
        "agent_version": "v2",
        "status": "running",
        "simulation_time": "2030-01-01T00:00:00Z",
        "simulation_ends_at": "2030-01-08T00:00:00Z",
        "commands_markdown": "Commands\npassword=unknown-server-password",
        "control_panel_auth": {
            "scheme": "basic",
            "username": PANEL_USER,
            "password": PANEL_PASSWORD,
            "instructions": "Use Basic auth",
        },
    }


def _resources_body(credential_id: str = "cred-server") -> dict[str, object]:
    return {
        "clock": _clock(),
        "active_instances": 2,
        "total_capacity_units": 200,
        "used_load_units": 0,
        "total_cost_per_hour_minor": 10,
        "servers": [
            {
                "server_id": "db-server",
                "name": "db",
                "role": "database",
                "instance_type": "db.small",
                "status": "active",
                "capacity_units": 100,
                "used_load_units": 0,
                "cost_per_hour_minor": 5,
                "disk": {},
                "database_ids": ["db-main", "db-target"],
                "credential_id": credential_id,
            },
            {
                "server_id": "backend-server",
                "name": "backend",
                "role": "backend",
                "instance_type": "backend.standard",
                "status": "active",
                "capacity_units": 100,
                "used_load_units": 0,
                "cost_per_hour_minor": 5,
                "disk": {},
                "database_ids": [],
                "credential_id": "cred-backend",
            },
        ],
    }


def _credential_body(credential_id: str, password: str = SERVER_PASSWORD) -> dict[str, object]:
    return {
        "clock": _clock(),
        "credential": {
            "credential_id": credential_id,
            "resource_id": "backend-server" if credential_id == "cred-backend" else "db-server",
            "version": 1,
            "username": SERVER_USER,
            "password": password,
            "valid_from": "2029-01-01T00:00:00Z",
            "expires_at": "2031-01-01T00:00:00Z",
        },
    }


def _json_response(status: int, body: dict[str, object]) -> httpx.Response:
    return httpx.Response(status, json=body)


def _new_client(handler):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://simulator/")
    return raw, SimulatorV2Client(http_client=raw)


def test_all_18_commands_use_v2_payload_and_private_target_auth() -> None:
    async def scenario() -> None:
        calls: list[dict[str, object]] = []
        credential_calls: Counter[str] = Counter()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/start":
                return _json_response(201, _start_body())
            if request.url.path.endswith("/resources"):
                return _json_response(200, _resources_body())
            if "/credentials/" in request.url.path:
                credential_id = request.url.path.rsplit("/", 1)[-1]
                credential_calls[credential_id] += 1
                return _json_response(200, _credential_body(credential_id))
            if request.url.path.endswith("/control/commands"):
                assert (
                    request.headers["authorization"]
                    == "Basic "
                    + base64.b64encode(f"{PANEL_USER}:{PANEL_PASSWORD}".encode()).decode()
                )
                payload = json.loads(request.content)
                calls.append(payload)
                command = payload["command"]
                body = {"clock": _clock(), "request_id": payload["request_id"], "command": command}
                if command in ASYNC_COMMANDS:
                    body |= {"operation_id": "O" * 16, "status": "queued"}
                    return _json_response(202, body)
                body["result"] = {"ok": True}
                return _json_response(200, body)
            raise AssertionError(request.url)

        raw, client = _new_client(handler)
        try:
            started = await client.start(
                seed=42, agent_id="agent", agent_version="v2", request_id="start-1"
            )
            assert "control_panel_auth" not in started
            assert PANEL_PASSWORD not in json.dumps(started)
            assert "<redacted>" in started["commands_markdown"]

            params_by_command: dict[str, dict[str, object]] = {
                "firewall.rules.list": {},
                "firewall.rules.upsert": {
                    "rule_id": "deny-cameras",
                    "priority": 100,
                    "action": "deny",
                    "match": {"region_code": "RU"},
                    "enabled": True,
                },
                "firewall.rules.delete": {"rule_id": "deny-cameras"},
                "server.types.list": {},
                "server.create": {
                    "name": "new-backend",
                    "role": "backend",
                    "instance_type": "backend.standard",
                },
                "server.inspect": {"server_id": "backend-server"},
                "server.delete": {"server_id": "backend-server"},
                "database.create": {"server_id": "db-server", "name": "new-db"},
                "database.inspect": {"database_id": "db-main"},
                "database.backup": {"database_id": "db-main"},
                "database.backups.list": {},
                "database.restore": {"database_id": "db-target", "backup_id": "backup-1"},
                "site.config.get": {},
                "site.stop": {},
                "site.start": {},
                "site.database.set": {
                    "database_id": "db-target",
                    "expected_current_database_id": "db-main",
                },
                "disk.usage": {"server_id": "backend-server"},
                "disk.cleanup": {"server_id": "backend-server"},
            }
            for index, command in enumerate(COMMANDS):
                result = await client.execute_command(
                    RUN_ID,
                    request_id=f"command-{index}",
                    command=command,
                    params=params_by_command[command],
                )
                assert result["command"] == command

            assert [item["command"] for item in calls] == COMMANDS
            for item in calls:
                if item["command"] in TARGET_COMMANDS:
                    assert item["target_auth"] == {
                        "username": SERVER_USER,
                        "password": SERVER_PASSWORD,
                    }
                else:
                    assert "target_auth" not in item
            assert credential_calls["cred-server"] >= 3
            assert credential_calls["cred-backend"] >= 2
        finally:
            await client.aclose()
            assert not raw.is_closed
            await raw.aclose()

    asyncio.run(scenario())


def test_read_methods_and_inbox_projection_keep_unknown_credentials_private() -> None:
    async def scenario() -> None:
        paths: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append((request.method, request.url.path))
            if request.url.path == "/v2/start":
                return _json_response(200, _start_body())
            if request.url.path.endswith("/resources"):
                return _json_response(200, _resources_body())
            if "/credentials/" in request.url.path:
                return _json_response(200, _credential_body(request.url.path.rsplit("/", 1)[-1]))
            if request.url.path.endswith("/overview"):
                return _json_response(
                    200, {"clock": _clock(), "run_id": RUN_ID, "status": "running"}
                )
            if request.url.path.endswith("/metrics"):
                return _json_response(200, {"clock": _clock(), "current": {}, "series": []})
            if request.url.path.endswith("/logs"):
                return _json_response(200, {"clock": _clock(), "logs": [], "next_cursor": None})
            if request.url.path.endswith("/probes"):
                return _json_response(
                    200,
                    {
                        "clock": _clock(),
                        "request_id": "probe-1",
                        "page": "product_list",
                        "status": 200,
                    },
                )
            if request.url.path.endswith("/inbox"):
                return _json_response(
                    200,
                    {
                        "clock": _clock(),
                        "messages": [
                            {
                                "message_id": "credential-message",
                                "sender_email": "credentials@example.test",
                                "sent_at": "2030-01-01T00:00:00Z",
                                "subject": "New credentials for server db-server",
                                "description": (
                                    "username=old-user password=historic-expired-secret "
                                    "credential_id=cred-old server_id=db-server"
                                ),
                            },
                            {
                                "message_id": "task-message",
                                "sender_email": "ops@example.test",
                                "sent_at": "2030-01-01T00:00:00Z",
                                "subject": "Service task",
                                "description": "Keep this useful operational message",
                            },
                        ],
                        "next_cursor": None,
                    },
                )
            raise AssertionError(request.url)

        raw, client = _new_client(handler)
        try:
            await client.start(seed=42, agent_id="agent", agent_version="v2", request_id="start-1")
            assert (await client.overview(RUN_ID))["run_id"] == RUN_ID
            assert (await client.metrics(RUN_ID))["series"] == []
            assert (await client.logs(RUN_ID))["next_cursor"] is None
            assert (await client.resources(RUN_ID))["servers"]
            assert (await client.probe(RUN_ID, request_id="probe-1", page="product_list"))[
                "status"
            ] == 200
            inbox = await client.inbox(RUN_ID)
            serialized = json.dumps(inbox)
            assert "historic-expired-secret" not in serialized
            assert "old-user" not in serialized
            assert "credential-message" in serialized
            assert "db-server" in serialized
            assert "Keep this useful operational message" in serialized
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


def test_target_auth_error_refreshes_credentials_once_with_same_request_id() -> None:
    async def scenario() -> None:
        command_payloads: list[dict[str, object]] = []
        resource_reads = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal resource_reads
            if request.url.path == "/v2/start":
                return _json_response(201, _start_body())
            if request.url.path.endswith("/resources"):
                resource_reads += 1
                credential_id = "cred-old" if resource_reads == 1 else "cred-new"
                return _json_response(200, _resources_body(credential_id))
            if "/credentials/" in request.url.path:
                credential_id = request.url.path.rsplit("/", 1)[-1]
                password = "old-password" if credential_id == "cred-old" else "new-password"
                return _json_response(200, _credential_body(credential_id, password))
            if request.url.path.endswith("/control/commands"):
                payload = json.loads(request.content)
                command_payloads.append(payload)
                if len(command_payloads) == 1:
                    return _json_response(
                        403,
                        {"error": "CREDENTIALS_EXPIRED", "message": "old-password was rejected"},
                    )
                return _json_response(
                    200,
                    {
                        "clock": _clock(),
                        "request_id": payload["request_id"],
                        "command": payload["command"],
                        "result": {"message": "new-password accepted"},
                    },
                )
            raise AssertionError(request.url)

        raw, client = _new_client(handler)
        try:
            await client.start(seed=42, agent_id="agent", agent_version="v2", request_id="start-1")
            result = await client.execute_command(
                RUN_ID,
                request_id="same-request-id",
                command="database.inspect",
                params={"database_id": "db-main"},
            )
            assert result["request_id"] == "same-request-id"
            assert len(command_payloads) == 2
            assert (
                command_payloads[0]["request_id"]
                == command_payloads[1]["request_id"]
                == "same-request-id"
            )
            assert command_payloads[0]["target_auth"]["password"] == "old-password"
            assert command_payloads[1]["target_auth"]["password"] == "new-password"
            assert "new-password" not in json.dumps(result)
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


def test_errors_are_safe_and_panel_unauthorized_is_not_automatically_retried() -> None:
    async def scenario() -> None:
        requests = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if request.url.path == "/v2/start":
                return _json_response(201, _start_body())
            if request.url.path.endswith("/control/commands"):
                return _json_response(
                    401,
                    {
                        "error": "CONTROL_UNAUTHORIZED",
                        "message": "panel-password must not be exposed",
                        "details": {"password": "another-secret"},
                    },
                )
            raise AssertionError(request.url)

        raw, client = _new_client(handler)
        try:
            await client.start(seed=42, agent_id="agent", agent_version="v2", request_id="start-1")
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.list_commands(RUN_ID)
            error = raised.value
            assert error.status_code == 401
            assert error.code == "CONTROL_UNAUTHORIZED"
            assert PANEL_PASSWORD not in str(error)
            assert "another-secret" not in str(error)
            assert requests == 2
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


def test_injected_client_is_not_closed_but_owned_client_is_closed() -> None:
    async def scenario() -> None:
        raw = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _json_response(200, {})))
        close = AsyncMock(wraps=raw.aclose)
        raw.aclose = close  # type: ignore[method-assign]
        client = SimulatorV2Client(http_client=raw)
        await client.aclose()
        close.assert_not_awaited()
        await raw.aclose()

        owned = SimulatorV2Client(base_url="http://simulator")
        await owned.aclose()
        assert owned.client.is_closed

    asyncio.run(scenario())
