import asyncio
import os
from uuid import uuid4

import pytest

from uptick_agent.simulator import SimulatorClient

SIMULATOR_URL = os.getenv("UPTICK_INTEGRATION_SIMULATOR_URL")


@pytest.mark.skipif(not SIMULATOR_URL, reason="set UPTICK_INTEGRATION_SIMULATOR_URL")
def test_public_simulator_contract_end_to_end() -> None:
    async def scenario() -> None:
        client = SimulatorClient(SIMULATOR_URL or "")
        prefix = uuid4().hex[:12]
        try:
            started = await client.start(
                seed=918_273,
                agent_id="uptick-integration",
                agent_version="contract-v1",
                request_id=f"integration-{prefix}-start",
            )
            overview = await client.overview(started.run_id)
            metrics = await client.metrics(started.run_id)
            logs = await client.logs(started.run_id, limit=10)
            resources = await client.resources(started.run_id)
            deployments = await client.deployments(started.run_id)
            advanced = await client.advance(
                started.run_id,
                request_id=f"integration-{prefix}-advance",
                duration_seconds=300,
            )
            economy = await client.economy(started.run_id)

            assert overview.run_id == started.run_id
            assert metrics.current.server_count >= 0
            assert logs.next_cursor is None or isinstance(logs.next_cursor, str)
            assert resources.desired_instances >= 0
            assert isinstance(deployments.deployments, list)
            assert advanced.requested_duration_seconds == 300
            assert economy.currency
        finally:
            await client.aclose()

    asyncio.run(scenario())
