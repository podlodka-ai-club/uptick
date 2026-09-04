import asyncio

from uptick_agent.models import RunResult
from uptick_agent.observers import JsonlObserver


def test_jsonl_observer_redacts_credential_shaped_output(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "trace.jsonl"
        observer = JsonlObserver(path)

        await observer.on_finish(
            RunResult(
                run_id="run",
                seed=1,
                agent_id="agent",
                agent_version="v1",
                status="failed",
                steps=0,
                duration_seconds=0,
                stop_reason="token=topsecret failed",
            )
        )

        persisted = path.read_text()
        assert "topsecret" not in persisted
        assert "<redacted> failed" in persisted

    asyncio.run(scenario())
