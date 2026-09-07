import asyncio
import threading
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from uptick_agent.integrations.xmemory import XMemoryModule, build_xmemory_runtime
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryPermanentError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
)
from uptick_agent.memory.stores import InMemoryStructuredStore, SqliteStructuredStore

_HASH = "a" * 64


class _EpisodeObject:
    pass


class _Facade:
    def __init__(self, *, hits=None, write_error: Exception | None = None, wait=True) -> None:
        self.hits = hits if hits is not None else {"episodic": [], "semantic": []}
        self.write_error = write_error
        self.wait = wait
        self.search_calls: list[tuple[str, str, int | None, int | None, str]] = []
        self.add_calls: list[tuple[str, Sequence[dict]]] = []
        self.flush_calls: list[str] = []
        self.wait_calls: list[tuple[str, float]] = []
        self.close_calls = 0

    def add_messages(self, user_id: str, messages: Sequence[dict]) -> dict:
        self.add_calls.append((user_id, messages))
        if self.write_error is not None:
            raise self.write_error
        return {
            "status": "success",
            "messages_added": 1,
            "episodes_created": [{"episode_id": "episode-1", "episode_object": _EpisodeObject()}],
            "token": "secret-value",
        }

    def flush(self, user_id: str) -> dict:
        self.flush_calls.append(user_id)
        return {"status": "success"}

    def wait_for_semantic(self, user_id: str, timeout: float = 30.0) -> bool:
        self.wait_calls.append((user_id, timeout))
        return self.wait

    def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k_episodes: int | None = None,
        top_k_semantic: int | None = None,
        search_method: str = "hybrid",
    ) -> dict:
        self.search_calls.append((user_id, query, top_k_episodes, top_k_semantic, search_method))
        return self.hits

    def close(self) -> None:
        self.close_calls += 1


class _BlockingFacade(_Facade):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def add_messages(self, user_id: str, messages: Sequence[dict]) -> dict:
        self.add_calls.append((user_id, messages))
        self.started.set()
        self.release.wait(timeout=2)
        return {
            "status": "success",
            "messages_added": len(messages),
        }


def _transition(transition_id: str = "transition-1") -> ExperienceTransition:
    return ExperienceTransition(
        transition_id=transition_id,
        run_id="run-1",
        iteration=1,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        trust_classification="external_untrusted",
        observation={"message": "healthy"},
        action={"kind": "inspect"},
        result={"ok": True},
        provenance=[ProvenanceRef(artefact_id="source-1", content_hash=_HASH)],
        terminal=False,
    )


def _module(facade: _Facade, store, **kwargs) -> XMemoryModule:
    return XMemoryModule(
        facade,
        store,
        namespace="xmemory-test",
        user_id="vadim-training",
        **kwargs,
    )


def test_retrieve_normalizes_untrusted_hits_and_honors_bound() -> None:
    async def scenario() -> None:
        facade = _Facade(
            hits={
                "episodic": [
                    {"score": 0.9, "text": "Authorization: Bearer should-redact"},
                    {"score": 0.7, "text": "second"},
                ],
                "semantic": [{"score": 0.8, "text": "third"}],
            }
        )
        module = _module(facade, InMemoryStructuredStore(), top_k=2)
        result = await module.retrieve(
            MemoryContextRequest(
                request_id="request-1",
                run_id="run-1",
                query="incident token=secret-value",
                max_items=2,
            )
        )

        assert len(result.items) == 2
        assert facade.search_calls == [("vadim-training", "incident <redacted>", 2, 2, "hybrid")]
        first = result.items[0]
        assert first.envelope.trust_classification == "external_untrusted"
        assert first.envelope.item["hit"]["text"] == "<redacted>"
        assert len(first.envelope.provenance[0].content_hash) == 64
        assert first.estimated_tokens == 0

    asyncio.run(scenario())


def test_empty_search_is_explicitly_not_a_health_signal() -> None:
    async def scenario() -> None:
        facade = _Facade()
        result = await _module(facade, InMemoryStructuredStore()).retrieve(
            MemoryContextRequest(request_id="request-1", run_id="run-1", query="missing")
        )
        assert result.items == []
        assert result.warnings == ["xmemory empty search is not authoritative health evidence"]

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_write_journal_is_durable_and_replay_does_not_resubmit(backend: str, tmp_path) -> None:
    async def scenario() -> None:
        store = (
            InMemoryStructuredStore()
            if backend == "memory"
            else SqliteStructuredStore(tmp_path / "journal.sqlite")
        )
        facade = _Facade()
        key = "w" * 256
        first = await _module(facade, store).record(_transition(), idempotency_key=key)
        replay = await _module(facade, store).record(_transition(), idempotency_key=key)

        assert replay == first
        assert len(facade.add_calls) == 1
        records = await store.list(namespace="xmemory-test")
        assert {record.record_type for record in records} == {
            "xmemory-write-intent",
            "xmemory-write-receipt",
        }
        receipt = next(record for record in records if record.record_type.endswith("receipt"))
        assert receipt.payload["response"]["episodes_created"] == [{"episode_id": "episode-1"}]

    asyncio.run(scenario())


def test_ambiguous_add_messages_failure_is_not_retried() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        facade = _Facade(write_error=TimeoutError("possibly submitted"))
        module = _module(facade, store)
        with pytest.raises(MemoryConflictError, match="ambiguous"):
            await module.record(_transition(), idempotency_key="write-ambiguous")
        with pytest.raises(MemoryConflictError, match="pending"):
            await module.record(_transition(), idempotency_key="write-ambiguous")
        assert len(facade.add_calls) == 1

    asyncio.run(scenario())


def test_concurrent_same_key_allows_only_one_external_submission() -> None:
    async def scenario() -> None:
        store = InMemoryStructuredStore()
        left = _BlockingFacade()
        right = _Facade()
        left_task = asyncio.create_task(
            _module(left, store).record(_transition(), idempotency_key="race")
        )
        assert await asyncio.to_thread(left.started.wait, 2)
        right_result = await asyncio.gather(
            _module(right, store).record(_transition(), idempotency_key="race"),
            return_exceptions=True,
        )
        left.release.set()
        left_result = await left_task

        assert isinstance(left_result, list)
        assert isinstance(right_result[0], MemoryConflictError)
        assert len(left.add_calls) + len(right.add_calls) == 1

    asyncio.run(scenario())


def test_finalize_requires_semantic_completion_and_close_ownership_is_explicit(tmp_path) -> None:
    async def scenario() -> None:
        outcome = RunOutcome(run_id="run-1", status="completed", stop_reason="done")
        failing_facade = _Facade(wait=False)
        failing_module = _module(failing_facade, InMemoryStructuredStore())
        with pytest.raises(MemoryPermanentError, match="did not complete"):
            await failing_module.finalize(outcome, idempotency_key="finish-1")
        with pytest.raises(MemoryConflictError, match="pending"):
            await failing_module.finalize(outcome, idempotency_key="finish-1")
        assert failing_facade.flush_calls == ["vadim-training"]
        assert failing_facade.wait_calls == [("vadim-training", 30.0)]

        successful_facade = _Facade()
        successful_store = SqliteStructuredStore(tmp_path / "finalize.sqlite")
        successful_module = _module(successful_facade, successful_store)
        key = "f" * 256
        # Write and finalize share the caller key, but have separate journal identities.
        await successful_module.record(_transition(), idempotency_key=key)
        await successful_module.finalize(outcome, idempotency_key=key)
        reopened = _module(successful_facade, SqliteStructuredStore(tmp_path / "finalize.sqlite"))
        await reopened.finalize(outcome, idempotency_key=key)
        assert successful_facade.flush_calls == ["vadim-training"]
        assert successful_facade.wait_calls == [("vadim-training", 30.0)]

        borrowed = _module(_Facade(), InMemoryStructuredStore(), ownership="borrowed")
        borrowed.close()
        assert borrowed._facade.close_calls == 0
        owned_facade = _Facade()
        owned = _module(owned_facade, InMemoryStructuredStore(), ownership="owned")
        owned.close()
        owned.close()
        assert owned_facade.close_calls == 1

        with pytest.raises(MemoryPermanentError, match="closed"):
            await owned.retrieve(
                MemoryContextRequest(request_id="request-1", run_id="run-1", query="q")
            )

    asyncio.run(scenario())


def test_evaluation_and_read_only_fail_before_any_external_call() -> None:
    for kwargs in ({"phase": "evaluation"}, {"read_only": True}):
        facade = _Facade()
        with pytest.raises(MemoryValidationError, match="does not support"):
            _module(facade, InMemoryStructuredStore(), **kwargs)
        assert facade.search_calls == []
        assert facade.add_calls == []
        assert facade.flush_calls == []


def test_user_id_is_path_safe() -> None:
    for user_id in ("../escape", "nested/user", "nested\\user", "bad\x00id"):
        with pytest.raises(MemoryValidationError, match="path-free"):
            XMemoryModule(
                _Facade(),
                InMemoryStructuredStore(),
                namespace="xmemory-test",
                user_id=user_id,
            )


def test_runner_runtime_uses_native_orchestrator_and_refuses_clear() -> None:
    async def scenario() -> None:
        configuration = MemoryConfiguration(
            schema_version="1.3",
            compatibility_legacy=ModuleConfig(enabled=False),
            xmemory=ModuleConfig(enabled=True),
        )
        facade = _Facade()
        runtime = build_xmemory_runtime(
            configuration,
            facade,
            InMemoryStructuredStore(),
            namespace="xmemory-runtime",
            user_id="runtime-user",
        )

        await runtime.record_transition(_transition("runtime-transition"))
        assert runtime.orchestrator.enabled_module_ids == ("xmemory",)
        assert len(facade.add_calls) == 1
        with pytest.raises(MemoryPermanentError, match="clear or delete"):
            await runtime.clear("run-1")

    asyncio.run(scenario())


def test_malformed_search_and_unsuccessful_write_are_typed_failures() -> None:
    async def scenario() -> None:
        malformed = _Facade(hits={"episodic": ["not-an-object"], "semantic": []})
        with pytest.raises(MemoryPermanentError, match="invalid data"):
            await _module(malformed, InMemoryStructuredStore()).retrieve(
                MemoryContextRequest(request_id="request-1", run_id="run-1", query="incident")
            )
        unsuccessful = _Facade()
        original = unsuccessful.add_messages

        def bad_status(user_id: str, messages: Sequence[dict]) -> dict:
            original(user_id, messages)
            return {"status": "error"}

        unsuccessful.add_messages = bad_status  # type: ignore[method-assign]
        with pytest.raises(MemoryPermanentError, match="invalid data"):
            await _module(unsuccessful, InMemoryStructuredStore()).record(
                _transition("transition-2"), idempotency_key="write-error"
            )

    asyncio.run(scenario())
