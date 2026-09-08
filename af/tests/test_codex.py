import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("openai_codex")
from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from tests.helpers import make_context
from uptick_agent.core.models import (
    AgentContext,
    Capability,
    CapabilityCatalog,
    ReasonerConfig,
    ReasoningEffortName,
)
from uptick_agent.core.sgr import DEFAULT_SYSTEM_PROMPT, CurrentSGR
from uptick_agent.reasoners.codex import (
    CODEX_CONFIG_OVERRIDES,
    DECISION_ONLY_INSTRUCTIONS,
    CodexReasoner,
    CodexReasoningError,
)


class FakeEvent:
    def __init__(self, event_type: str) -> None:
        self.type = event_type


class FakeResult:
    def __init__(
        self,
        *,
        status: str = "completed",
        final_response: str | None = None,
        items: list[FakeEvent] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.final_response = final_response
        self.items = items or []
        self.usage = usage
        self.error = None


class FakeThread:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.run_calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, prompt: str, **kwargs: Any) -> FakeResult:
        self.run_calls.append((prompt, kwargs))
        return self.result


class FakeAccount:
    def __init__(self, account_type: str) -> None:
        self.type = account_type


class FakeAccountRoot:
    def __init__(self, account_type: str) -> None:
        self.root = FakeAccount(account_type)


class FakeAccountResponse:
    def __init__(self, account_type: str | None) -> None:
        self.account = FakeAccountRoot(account_type) if account_type is not None else None


class FakeCodex:
    def __init__(self, result: FakeResult, *, account_type: str | None = "chatgpt") -> None:
        self.result = result
        self.account_type = account_type
        self.account_calls = 0
        self.thread_start_calls: list[dict[str, Any]] = []
        self.threads: list[FakeThread] = []
        self.closed = False

    async def account(self, **kwargs: Any) -> FakeAccountResponse:
        self.account_calls += 1
        return FakeAccountResponse(self.account_type)

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.thread_start_calls.append(kwargs)
        thread = FakeThread(self.result)
        self.threads.append(thread)
        return thread

    async def close(self) -> None:
        self.closed = True


def _context() -> AgentContext:
    schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    return make_context(
        environment="test",
        objective="keep the service healthy",
        step_limit=5,
        capabilities=CapabilityCatalog(
            items=[Capability(name="get_overview", description="inspect", input_schema=schema)]
        ),
    )


def _request():
    return CurrentSGR().build_request(_context())


def _valid_response() -> str:
    return json.dumps(
        {
            "phase": "observe",
            "facts": ["the run has started"],
            "competing_hypotheses": ["an overview will establish the baseline"],
            "contradicting_evidence": [],
            "previous_verification": {"status": "not_applicable", "evidence": []},
            "strategy": "inspect the overview before changing state",
            "selected_action": {"name": "get_overview", "arguments": {}},
            "expected_result": ["the overview establishes the baseline"],
            "verification": ["the observation contains site and capacity state"],
            "task_completed": False,
        }
    )


def _as_codex_client(client: Any) -> AsyncCodex:
    return cast(AsyncCodex, client)


def _config(
    *,
    model: str = "test-codex-model",
    retries: int = 1,
    effort: ReasoningEffortName | None = None,
) -> ReasonerConfig:
    return ReasonerConfig(
        provider="codex",
        model=model,
        effort=effort,
        thread_mode="ephemeral",
        retries=retries,
    )


def _schema_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_nodes(child)


def test_prompts_are_pinned_byte_for_byte() -> None:
    assert hashlib.sha256(_request().system_prompt.encode()).hexdigest() == (
        "0607348fab79ae4aabac0a8e27796008f92298bec2f5f2b98780c3f439795f60"
    )
    assert hashlib.sha256(DECISION_ONLY_INSTRUCTIONS.encode()).hexdigest() == (
        "86365ece8aac8ab5d4a1e78973668a09f2bbb8d2db4bf7342de6c1839d9dc2b9"
    )
    assert "DDoS" not in DEFAULT_SYSTEM_PROMPT
    assert "scale_backend" not in DEFAULT_SYSTEM_PROMPT
    assert "advance_time" not in DEFAULT_SYSTEM_PROMPT


def test_codex_output_schema_uses_supported_strict_subset() -> None:
    reasoner = CodexReasoner(
        config=_config(),
        client=_as_codex_client(FakeCodex(FakeResult(final_response=_valid_response()))),
    )
    schema = reasoner._run_kwargs(_request())["output_schema"]

    for node in _schema_nodes(schema):
        assert "default" not in node
        assert "discriminator" not in node
        assert "oneOf" not in node
        assert "const" not in node
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert node.get("additionalProperties") is False
            assert node.get("required") == list(properties)

    assert schema["title"] == "SGREnvelope"
    assert set(schema["properties"]) == {
        "phase",
        "facts",
        "competing_hypotheses",
        "contradicting_evidence",
        "previous_verification",
        "strategy",
        "selected_action",
        "expected_result",
        "verification",
        "task_completed",
    }
    assert "anyOf" in schema["properties"]["selected_action"]
    action = schema["properties"]["selected_action"]["anyOf"][0]
    assert action["properties"]["name"]["enum"] == ["get_overview"]


def test_codex_uses_fresh_ephemeral_read_only_thread_and_schema(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeCodex(
            FakeResult(
                final_response=_valid_response(),
                usage={
                    "total": {
                        "input_tokens": 8,
                        "output_tokens": 5,
                        "total_tokens": 13,
                        "cached_input_tokens": 2,
                        "reasoning_output_tokens": 3,
                        "cache_write_input_tokens": 1,
                    }
                },
            )
        )
        workspace = tmp_path / "fake-codex-workspace"
        workspace.mkdir()
        reasoner = CodexReasoner(
            config=_config(effort="high"),
            client=_as_codex_client(fake),
            workspace_dir=workspace,
        )
        request = _request()

        first = await reasoner.reason(request)
        second = await reasoner.reason(request)

        assert first.output["selected_action"] == {"name": "get_overview", "arguments": {}}
        assert second.output == first.output
        assert first.telemetry.provider == "codex"
        assert first.telemetry.requested_model == "test-codex-model"
        assert first.telemetry.reported_model is None
        assert first.telemetry.requested_effort == "high"
        assert first.telemetry.reported_effort is None
        assert first.telemetry.thread_mode == "ephemeral"
        assert first.telemetry.token_usage.total_tokens == 13
        assert first.telemetry.token_usage.cache_write_input_tokens == 1
        assert (
            first.telemetry.provider_instructions_sha256
            == hashlib.sha256(DECISION_ONLY_INSTRUCTIONS.encode("utf-8")).hexdigest()
        )
        assert fake.account_calls == 2
        assert len(fake.threads) == 2
        for call, thread in zip(fake.thread_start_calls, fake.threads, strict=True):
            assert call["approval_mode"] is ApprovalMode.deny_all
            assert call["sandbox"] is Sandbox.read_only
            assert call["ephemeral"] is True
            assert call["model"] == "test-codex-model"
            assert call["cwd"] == str(workspace)
            assert DEFAULT_SYSTEM_PROMPT in call["developer_instructions"]
            assert "decision-only" in call["developer_instructions"].lower()
            prompt, run_kwargs = thread.run_calls[0]
            assert request.user_prompt == prompt
            assert run_kwargs["approval_mode"] is ApprovalMode.deny_all
            assert run_kwargs["sandbox"] is Sandbox.read_only
            assert run_kwargs["output_schema"]["title"] == "SGREnvelope"
            assert run_kwargs["model"] == "test-codex-model"
            assert run_kwargs["effort"] == "high"
            assert run_kwargs["cwd"] == str(workspace)

        await reasoner.aclose()
        assert not fake.closed
        assert workspace.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("account_type", [None, "apiKey", "amazonBedrock"])
def test_codex_rejects_non_subscription_auth_before_turn(
    account_type: str | None,
) -> None:
    async def scenario() -> None:
        fake = FakeCodex(FakeResult(final_response=_valid_response()), account_type=account_type)
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(fake))
        with pytest.raises(CodexReasoningError, match="requires a ChatGPT/Codex subscription"):
            await reasoner.reason(_request())
        assert fake.thread_start_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result, message",
    [
        (FakeResult(status="failed", final_response=_valid_response()), "did not complete"),
        (FakeResult(final_response=None), "without a final schema response"),
        (FakeResult(final_response="not json"), "invalid structured response"),
        (
            FakeResult(final_response='{"phase":"observe"}'),
            "invalid structured response",
        ),
    ],
)
def test_codex_rejects_incomplete_or_invalid_results(result: FakeResult, message: str) -> None:
    async def scenario() -> None:
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(FakeCodex(result)))
        with pytest.raises(CodexReasoningError, match=message):
            await reasoner.reason(_request())

    asyncio.run(scenario())


def test_codex_retries_once_with_validation_feedback() -> None:
    invalid_response = json.dumps(
        {
            "phase": "observe",
            "facts": ["inspect"],
            "competing_hypotheses": ["probe"],
            "contradicting_evidence": [],
            "previous_verification": {"status": "not_applicable", "evidence": []},
            "strategy": "inspect",
            "selected_action": {"name": "get_overview", "arguments": []},
            "expected_result": ["overview returned"],
            "verification": ["inspect the overview"],
            "task_completed": False,
        }
    )

    class SequencedCodex(FakeCodex):
        def __init__(self) -> None:
            super().__init__(FakeResult(final_response=invalid_response))
            self.results = [
                FakeResult(final_response=invalid_response),
                FakeResult(final_response=_valid_response()),
            ]

        async def thread_start(self, **kwargs: Any) -> FakeThread:
            self.thread_start_calls.append(kwargs)
            thread = FakeThread(self.results[len(self.threads)])
            self.threads.append(thread)
            return thread

    async def scenario() -> None:
        fake = SequencedCodex()
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(fake))

        result = await reasoner.reason(_request())

        assert result.output["selected_action"] == {
            "name": "get_overview",
            "arguments": {},
        }
        assert result.telemetry.attempts == 2
        assert len(fake.threads) == 2
        retry_prompt = fake.threads[1].run_calls[0][0]
        assert "previous response failed application validation" in retry_prompt.lower()
        assert "dict_type" in retry_prompt

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event_type",
    ["commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView"],
)
def test_codex_rejects_tool_events(event_type: str) -> None:
    async def scenario() -> None:
        result = FakeResult(final_response=_valid_response(), items=[FakeEvent(event_type)])
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(FakeCodex(result)))
        with pytest.raises(CodexReasoningError, match="forbidden tool-use event"):
            await reasoner.reason(_request())

    asyncio.run(scenario())


def test_codex_request_failure_directs_operator_to_local_login() -> None:
    class UnauthenticatedCodex:
        async def account(self, **kwargs: Any) -> FakeAccountResponse:
            raise RuntimeError("subscription session is unavailable")

    async def scenario() -> None:
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(UnauthenticatedCodex()))
        with pytest.raises(CodexReasoningError, match=r"run `codex login`"):
            await reasoner.reason(_request())

    asyncio.run(scenario())


def test_codex_post_auth_failure_does_not_blame_local_login() -> None:
    class FailingThreadCodex(FakeCodex):
        async def thread_start(self, **kwargs: Any) -> FakeThread:
            raise RuntimeError("runtime transport failed")

    async def scenario() -> None:
        reasoner = CodexReasoner(
            config=_config(),
            client=_as_codex_client(
                FailingThreadCodex(FakeResult(final_response=_valid_response()))
            ),
        )
        with pytest.raises(CodexReasoningError) as captured:
            await reasoner.reason(_request())
        assert "after ChatGPT subscription authentication" in str(captured.value)
        assert "codex login" not in str(captured.value)

    asyncio.run(scenario())


def test_codex_owned_client_uses_and_cleans_isolated_workspace(monkeypatch) -> None:
    class FakeOwnedCodex(FakeCodex):
        instance: Any = None

        def __init__(self, config: Any) -> None:
            super().__init__(FakeResult(final_response=_valid_response()))
            self.config = config
            FakeOwnedCodex.instance = self

    monkeypatch.setattr("uptick_agent.reasoners.codex.AsyncCodex", FakeOwnedCodex)

    async def scenario() -> None:
        reasoner = CodexReasoner(config=_config())
        workspace = reasoner._workspace_dir
        assert workspace is not None
        assert (workspace / ".git").is_dir()
        assert FakeOwnedCodex.instance.config.cwd == str(workspace)
        assert FakeOwnedCodex.instance.config.config_overrides == CODEX_CONFIG_OVERRIDES

        await reasoner.aclose()
        assert FakeOwnedCodex.instance.closed
        assert not workspace.exists()

    asyncio.run(scenario())


def test_codex_cleans_workspace_when_client_construction_fails(monkeypatch) -> None:
    captured_workspace: Path | None = None

    class FailingCodex:
        def __init__(self, config: Any) -> None:
            nonlocal captured_workspace
            captured_workspace = Path(config.cwd)
            raise RuntimeError("no local Codex runtime")

    monkeypatch.setattr("uptick_agent.reasoners.codex.AsyncCodex", FailingCodex)
    with pytest.raises(RuntimeError, match="no local Codex runtime"):
        CodexReasoner(config=_config())
    assert captured_workspace is not None
    assert not captured_workspace.exists()


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_owned_codex_refuses_api_key_environment(monkeypatch, variable: str) -> None:
    monkeypatch.setenv(variable, "not-a-real-key")
    with pytest.raises(ValueError, match="Unset OPENAI_API_KEY and CODEX_API_KEY"):
        CodexReasoner(config=_config())


@pytest.mark.parametrize("extra", [0, 1])
def test_codex_checks_actual_user_characters_before_thread(monkeypatch, extra: int) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("uptick_agent.reasoners.codex.MAX_USER_INPUT_CHARACTERS", 100)
        request = replace(_request(), user_prompt="я" * (100 + extra))
        fake = FakeCodex(FakeResult(final_response=_valid_response()))
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(fake))
        if extra:
            with pytest.raises(CodexReasoningError) as caught:
                await reasoner.reason(request)
            assert caught.value.category == "input_too_large"
            assert caught.value.telemetry.attempts == 0
            assert fake.thread_start_calls == []
            assert "я" not in str(caught.value)
            telemetry = caught.value.telemetry
        else:
            telemetry = (await reasoner.reason(request)).telemetry
            assert len(fake.thread_start_calls) == 1
        sizes = telemetry.provider_runtime["input_sizes"]
        assert isinstance(sizes, dict)
        assert sizes["user_prompt_characters"] == 100 + extra
        assert sizes["user_prompt_utf8_bytes"] == (100 + extra) * 2
        assert sizes["developer_instructions_characters"] == len(
            request.system_prompt + "\n\n" + DECISION_ONLY_INSTRUCTIONS
        )
        assert sizes["user_prompt_character_limit"] == 100
        assert fake.account_calls == 1

    asyncio.run(scenario())


def test_codex_rechecks_limit_with_validation_feedback(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr("uptick_agent.reasoners.codex.MAX_USER_INPUT_CHARACTERS", 100)
        request = replace(_request(), user_prompt="x" * 100)
        fake = FakeCodex(FakeResult(final_response=""))
        reasoner = CodexReasoner(config=_config(), client=_as_codex_client(fake))
        with pytest.raises(CodexReasoningError) as caught:
            await reasoner.reason(request)
        assert caught.value.category == "input_too_large"
        assert caught.value.telemetry.attempts == 1
        assert len(fake.thread_start_calls) == 1
        sizes = caught.value.telemetry.provider_runtime["input_sizes"]
        assert isinstance(sizes, dict)
        expected = reasoner._decision_prompt(
            request.user_prompt, "The previous response was empty."
        )
        assert sizes["user_prompt_characters"] == len(expected)

    asyncio.run(scenario())
