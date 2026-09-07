import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("openai_codex")


def test_cancelled_async_turn_keeps_router_queue_until_transport_close() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from openai_codex.api import AsyncTurnHandle
        from openai_codex.client import CodexClient
        from openai_codex.errors import TransportClosedError
        from uptick_agent.llm.codex import CodexLlmClient
        from uptick_agent.llm.contracts import LlmMessage, StructuredGenerationRequest
        from uptick_agent.models import V1NextStep

        class Stdin:
            def close(self):
                pass

        class Process:
            def __init__(self, router):
                self.stdin = Stdin()
                self.router = router

            def terminate(self):
                self.router.fail_all(TransportClosedError("fake transport closed"))

            def wait(self, *, timeout):
                return 0

            def kill(self):
                self.terminate()

        class AsyncTransport:
            def __init__(self, sync):
                self._sync = sync

            def register_turn_notifications(self, turn_id):
                self._sync.register_turn_notifications(turn_id)

            def unregister_turn_notifications(self, turn_id):
                self._sync.unregister_turn_notifications(turn_id)

            async def next_turn_notification(self, turn_id):
                return await asyncio.to_thread(self._sync.next_turn_notification, turn_id)

            async def close(self):
                await asyncio.to_thread(self._sync.close)

        class CodexOwner:
            def __init__(self, client):
                self._client = client

            async def _ensure_initialized(self):
                pass

        class Thread:
            def __init__(self, owner):
                self.handle = AsyncTurnHandle(owner, "thread-1", "turn-1")

            async def run(self, *args, **kwargs):
                return await self.handle.run()

        class Account:
            type = "chatgpt"

        class AccountRoot:
            root = Account()

        class AccountResponse:
            account = AccountRoot()

        class AsyncSdk:
            def __init__(self):
                self.sync = CodexClient()
                self.sync._proc = Process(self.sync._router)
                self._client = AsyncTransport(self.sync)
                self.owner = CodexOwner(self._client)

            async def account(self, **kwargs):
                return AccountResponse()

            async def thread_start(self, **kwargs):
                return Thread(self.owner)

            async def close(self):
                await self._client.close()

        async def scenario():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
            sdk = AsyncSdk()
            model = CodexLlmClient(client=sdk)
            model._owns_client = True
            request = StructuredGenerationRequest(
                response_model=V1NextStep,
                messages=(LlmMessage(role="user", content="choose"),),
            )
            task = asyncio.create_task(model.generate_structured(request))
            for _ in range(100):
                if "turn-1" in sdk.sync._router._turn_notifications:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("turn notification waiter did not start")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await model.aclose()

        asyncio.run(scenario())
        """
    )
    env = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH")) if part
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_cancelled_borrowed_codex_client_keeps_lifecycle_with_cancellation() -> None:
    import asyncio

    from uptick_agent.llm.codex import CodexLlmClient
    from uptick_agent.llm.contracts import LlmMessage, StructuredGenerationRequest
    from uptick_agent.models import V1NextStep

    class Account:
        type = "chatgpt"

    class AccountRoot:
        root = Account()

    class AccountResponse:
        account = AccountRoot()

    class PendingThread:
        async def run(self, *args, **kwargs):
            await asyncio.sleep(60)

    class BorrowedSdk:
        def __init__(self):
            self.closed = False

        async def account(self, **kwargs):
            return AccountResponse()

        async def thread_start(self, **kwargs):
            return PendingThread()

        async def close(self):
            self.closed = True

    async def scenario() -> None:
        sdk = BorrowedSdk()
        model = CodexLlmClient(client=sdk)
        request = StructuredGenerationRequest(
            response_model=V1NextStep,
            messages=(LlmMessage(role="user", content="choose"),),
        )
        task = asyncio.create_task(model.generate_structured(request))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert not sdk.closed

    asyncio.run(scenario())
