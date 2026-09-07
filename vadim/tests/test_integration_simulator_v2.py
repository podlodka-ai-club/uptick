import asyncio
import os
from uuid import uuid4

import pytest

from uptick_agent.models import GetMetrics, GetOverview, GetResources, V2AdvanceTime
from uptick_agent.simulator.v2_client import SimulatorV2Client
from uptick_agent.simulator.v2_environment import SimulatorV2Environment

SIMULATOR_V2_URL = os.getenv("SIMULATOR_V2_URL")


@pytest.mark.skipif(not SIMULATOR_V2_URL, reason="set SIMULATOR_V2_URL for the opt-in live check")
def test_simulator_v2_environment_lifecycle_is_sanitized() -> None:
    async def scenario() -> None:
        client = SimulatorV2Client(SIMULATOR_V2_URL or "")
        environment = SimulatorV2Environment(client)
        request_prefix = uuid4().hex[:12]
        try:
            session, started = await environment.start(
                seed=918_273,
                agent_id="uptick-v2-integration",
                agent_version="environment-v2",
                request_id=f"integration-{request_prefix}-start",
            )
            assert session.run_id
            assert "control_panel_auth" not in started.data
            assert "password" not in str(started.data).lower()

            overview = await environment.execute(session, GetOverview())
            metrics = await environment.execute(session, GetMetrics())
            resources = await environment.execute(session, GetResources())
            advanced = await environment.execute(
                session,
                V2AdvanceTime(duration_seconds=300, stop_when=None),
            )
            final = await environment.finish(
                session,
                steps=4,
                duration_seconds=0,
                stop_reason="integration check",
            )

            assert overview.ok
            assert metrics.ok
            assert resources.ok
            assert advanced.ok
            assert final.objective_kind == "uptime_cost"
            assert final.total_cost_minor is not None
            assert {item.name for item in overview.objective_metrics} >= {
                "total_cost_minor",
                "observed_seconds",
                "available_seconds",
                "downtime_seconds",
            }
            assert "password" not in str(overview.data).lower()
        finally:
            await client.aclose()

    asyncio.run(scenario())
