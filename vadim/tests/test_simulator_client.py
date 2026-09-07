import asyncio

import httpx

from uptick_agent.simulator.client import SimulatorApiError, SimulatorClient


def test_start_uses_public_simulator_contract() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/v1/start"
            assert b'"seed":42' in request.content
            return httpx.Response(
                201,
                json={
                    "run_id": "A" * 20,
                    "seed": 42,
                    "agent_id": "agent",
                    "agent_version": "v1",
                    "status": "running",
                    "simulation_time": "2026-08-01T00:00:00Z",
                    "simulation_ends_at": "2026-09-01T00:00:00Z",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://simulator/"
        ) as raw:
            client = SimulatorClient(client=raw)
            response = await client.start(
                seed=42, agent_id="agent", agent_version="v1", request_id="start-1"
            )
            assert response.run_id == "A" * 20

    asyncio.run(scenario())


def test_api_errors_keep_machine_readable_code() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={"error": "RUN_COMPLETED", "message": "Run is already complete"},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://simulator/"
        ) as raw:
            client = SimulatorClient(client=raw)
            with pytest.raises(SimulatorApiError) as raised:
                await client.overview("A" * 20)
            assert raised.value.code == "RUN_COMPLETED"

    import pytest

    asyncio.run(scenario())
