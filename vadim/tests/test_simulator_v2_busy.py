from __future__ import annotations

import asyncio

import httpx
import pytest

from uptick_agent.simulator.v2_client import SimulatorV2ApiError, SimulatorV2Client


def _client(response: httpx.Response) -> tuple[httpx.AsyncClient, SimulatorV2Client]:
    raw = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response),
        base_url="http://simulator/",
    )
    return raw, SimulatorV2Client(http_client=raw)


@pytest.mark.parametrize("http_status", [409, 429])
def test_run_busy_preserves_only_safe_operation_and_numeric_retry_after(http_status: int) -> None:
    async def scenario() -> None:
        operation_id = "Ab12Cd34Ef56Gh78"
        raw, client = _client(
            httpx.Response(
                http_status,
                headers={"Retry-After": "7"},
                json={
                    "error": "RUN_BUSY",
                    "message": "Time advance is running",
                    "details": {
                        "operation_id": operation_id,
                        "credential_id": "credential-secret-value",
                        "nested": {"password": "another-secret"},
                    },
                },
            )
        )
        try:
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.overview("R" * 20)
            error = raised.value
            assert error.status_code == http_status
            assert error.code == "RUN_BUSY"
            assert error.details == {"operation_id": operation_id}
            assert error.retry_after_seconds == 7
            assert "credential" not in repr(error.details)
            assert "secret" not in repr(error.details)
            assert operation_id not in str(error)
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("operation_id", "retry_after", "expected_details", "expected_retry_after"),
    [
        ("Ab12Cd34Ef56Gh78", "1.5", {"operation_id": "Ab12Cd34Ef56Gh78"}, None),
        ("bad-operation-id!!", "9", {}, 9),
        ("A" * 65, "-1", {}, None),
        ("bad-operation-id!!", " 7 ", {}, None),
        ("bad-operation-id!!", "1" * 5_000, {}, None),
    ],
)
@pytest.mark.parametrize("http_status", [409, 429])
def test_run_busy_discards_malformed_or_redacted_metadata(
    http_status: int,
    operation_id: str,
    retry_after: str,
    expected_details: dict[str, str],
    expected_retry_after: int | None,
) -> None:
    async def scenario() -> None:
        raw, client = _client(
            httpx.Response(
                http_status,
                headers={"Retry-After": retry_after},
                json={
                    "error": "RUN_BUSY",
                    "message": "Busy",
                    "details": {"operation_id": operation_id},
                },
            )
        )
        try:
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.overview("R" * 20)
            assert raised.value.details == expected_details
            assert raised.value.retry_after_seconds == expected_retry_after
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("http_status", [409, 429])
def test_run_busy_drops_an_operation_id_equal_to_a_registered_secret(http_status: int) -> None:
    async def scenario() -> None:
        secret = "Ab12Cd34Ef56Gh78"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/start":
                return httpx.Response(
                    201,
                    json={
                        "run_id": "R" * 20,
                        "status": "running",
                        "commands_markdown": "public commands",
                        "control_panel_auth": {"username": "panel", "password": secret},
                    },
                )
            return httpx.Response(
                http_status,
                headers={"Retry-After": "3"},
                json={
                    "error": "RUN_BUSY",
                    "message": "Busy",
                    "details": {"operation_id": secret},
                },
            )

        raw = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://simulator/"
        )
        client = SimulatorV2Client(http_client=raw)
        try:
            await client.start(seed=42, agent_id="agent", agent_version="v2", request_id="start")
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.overview("R" * 20)
            assert raised.value.details == {}
            assert raised.value.retry_after_seconds == 3
            assert secret not in repr(raised.value.details)
            assert secret not in str(raised.value)
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "http_status,code", [(409, "CONCURRENT_RUN_REQUEST"), (429, "RATE_LIMITED"), (503, "RUN_BUSY")]
)
def test_other_errors_and_legacy_constructor_keep_empty_busy_metadata(
    http_status: int, code: str
) -> None:
    async def scenario() -> None:
        raw, client = _client(
            httpx.Response(
                http_status,
                headers={"Retry-After": "5"},
                json={
                    "error": code,
                    "message": "Conflict",
                    "details": {"operation_id": "Ab12Cd34Ef56Gh78"},
                },
            )
        )
        try:
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.overview("R" * 20)
            assert raised.value.details == {}
            assert raised.value.retry_after_seconds is None
        finally:
            await client.aclose()
            await raw.aclose()

        legacy = SimulatorV2ApiError(400, "INVALID_REQUEST", "invalid")
        assert legacy.details == {}
        assert legacy.retry_after_seconds is None
        assert str(legacy) == "HTTP 400 INVALID_REQUEST: invalid"

    asyncio.run(scenario())


def test_time_advance_accepts_async_080_and_synchronous_071_shapes() -> None:
    async def scenario() -> None:
        responses = iter(
            [
                httpx.Response(
                    202,
                    json={
                        "run_id": "R" * 20,
                        "request_id": "advance-1",
                        "operation_id": "Ab12Cd34Ef56Gh78",
                        "status": "running",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "clock": {},
                        "previous_simulation_time": "2030-01-01T00:00:00Z",
                        "requested_duration_seconds": 300,
                        "processed_events": 1,
                        "new_logs": 2,
                        "stop_reason": "duration_elapsed",
                    },
                ),
            ]
        )
        raw = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: next(responses)),
            base_url="http://simulator/",
        )
        client = SimulatorV2Client(http_client=raw)
        try:
            accepted = await client.advance_time(
                "R" * 20, request_id="advance-1", duration_seconds=300
            )
            assert accepted["operation_id"] == "Ab12Cd34Ef56Gh78"
            completed = await client.advance_time(
                "R" * 20, request_id="advance-2", duration_seconds=300
            )
            assert completed["stop_reason"] == "duration_elapsed"
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (202, {"request_id": "advance", "operation_id": "Ab12Cd34Ef56Gh78", "status": "running"}),
        (
            200,
            {
                "clock": {},
                "previous_simulation_time": "2030-01-01T00:00:00Z",
                "requested_duration_seconds": 300,
                "processed_events": 1,
                "new_logs": 2,
            },
        ),
        (
            201,
            {
                "run_id": "R" * 20,
                "request_id": "advance",
                "operation_id": "Ab12Cd34Ef56Gh78",
                "status": "running",
            },
        ),
    ],
)
def test_time_advance_rejects_missing_fields_and_unexpected_success_status(
    status: int, body: dict[str, object]
) -> None:
    async def scenario() -> None:
        raw, client = _client(httpx.Response(status, json=body))
        try:
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.advance_time("R" * 20, request_id="advance", duration_seconds=300)
            assert raised.value.code == "INVALID_RESPONSE"
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


def test_operation_accepts_time_advance_without_outer_clock_and_legacy_control() -> None:
    async def scenario() -> None:
        responses = iter(
            [
                httpx.Response(
                    200,
                    json={
                        "run_id": "R" * 20,
                        "request_id": "advance-1",
                        "operation_id": "Ab12Cd34Ef56Gh78",
                        "type": "time.advance",
                        "status": "running",
                        "submitted_at": "2026-09-06T00:00:00Z",
                        "started_at": "2026-09-06T00:00:01Z",
                        "completed_at": None,
                        "result": None,
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "clock": {},
                        "operation_id": "Zy98Xw76Vu54Ts32",
                        "type": "control_command",
                        "command": "server.create",
                        "request_id": "command-1",
                        "status": "running",
                        "progress": 0,
                        "result": None,
                    },
                ),
            ]
        )
        raw = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: next(responses)),
            base_url="http://simulator/",
        )
        client = SimulatorV2Client(http_client=raw)
        try:
            advance = await client.operation("R" * 20, "Ab12Cd34Ef56Gh78")
            assert advance["type"] == "time.advance"
            assert "clock" not in advance
            control = await client.operation("R" * 20, "Zy98Xw76Vu54Ts32")
            assert control["command"] == "server.create"
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (
            200,
            {
                "run_id": "R" * 20,
                "request_id": "advance",
                "operation_id": "Ab12Cd34Ef56Gh78",
                "type": "time.advance",
                "status": "running",
                "submitted_at": "2026-09-06T00:00:00Z",
                "started_at": None,
                "result": None,
            },
        ),
        (
            201,
            {
                "run_id": "R" * 20,
                "request_id": "advance",
                "operation_id": "Ab12Cd34Ef56Gh78",
                "type": "time.advance",
                "status": "running",
                "submitted_at": "2026-09-06T00:00:00Z",
                "started_at": None,
                "completed_at": None,
                "result": None,
            },
        ),
    ],
)
def test_operation_rejects_missing_time_fields_and_unexpected_success_status(
    status: int, body: dict[str, object]
) -> None:
    async def scenario() -> None:
        raw, client = _client(httpx.Response(status, json=body))
        try:
            with pytest.raises(SimulatorV2ApiError) as raised:
                await client.operation("R" * 20, "Ab12Cd34Ef56Gh78")
            assert raised.value.code == "INVALID_RESPONSE"
        finally:
            await client.aclose()
            await raw.aclose()

    asyncio.run(scenario())
