import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("openai_codex")
from openai_codex import ApprovalMode, Sandbox

from uptick_agent.llm.codex import (
    CODEX_CONFIG_OVERRIDES,
    CodexDecisionError,
    CodexSGRModel,
)
from uptick_agent.llm.openai import DEFAULT_SYSTEM_PROMPT
from uptick_agent.models import DecisionContext, NextStep, ToolResult


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
    ) -> None:
        self.status = status
        self.final_response = final_response
        self.items = items or []
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
    """Mirrors openai_codex.types.Account: a RootModel around the concrete account."""

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


def _context() -> DecisionContext:
    return DecisionContext(
        objective="keep the service healthy",
        run_id="run-123",
        seed=7,
        iteration=1,
        max_steps=5,
        latest_result=ToolResult(action_kind="start", summary="simulation started"),
    )


def _valid_response() -> str:
    return NextStep.model_validate(
        {
            "current_situation": "the run has started",
            "hypothesis": "an overview will establish the baseline",
            "remaining_steps": ["inspect the overview"],
            "task_completed": False,
            "action": {"kind": "get_overview"},
        }
    ).model_dump_json()


def test_codex_decide_uses_a_fresh_ephemeral_read_only_thread_and_schema(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake = FakeCodex(FakeResult(final_response=_valid_response()))
        workspace = tmp_path / "fake-codex-workspace"
        workspace.mkdir()
        model = CodexSGRModel(client=fake, model="test-codex-model", workspace_dir=workspace)
        context = _context()

        first = await model.decide(context)
        second = await model.decide(context)

        assert first.action.kind == "get_overview"
        assert second == first
        assert fake.account_calls == 2
        assert len(fake.thread_start_calls) == 2
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
            assert context.model_dump_json() in prompt
            assert run_kwargs["approval_mode"] is ApprovalMode.deny_all
            assert run_kwargs["sandbox"] is Sandbox.read_only
            assert run_kwargs["output_schema"] == NextStep.model_json_schema()
            assert run_kwargs["model"] == "test-codex-model"
            assert run_kwargs["cwd"] == str(workspace)

        await model.aclose()
        assert not fake.closed
        assert workspace.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("account_type", [None, "apiKey", "amazonBedrock"])
def test_codex_rejects_non_subscription_auth_before_starting_a_turn(
    account_type: str | None,
) -> None:
    async def scenario() -> None:
        fake = FakeCodex(FakeResult(final_response=_valid_response()), account_type=account_type)
        model = CodexSGRModel(client=fake)

        with pytest.raises(CodexDecisionError, match=r"requires a ChatGPT/Codex subscription"):
            await model.decide(_context())

        assert fake.thread_start_calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "result, message",
    [
        (FakeResult(status="failed", final_response=_valid_response()), "did not complete"),
        (FakeResult(final_response=None), "without a final schema response"),
        (FakeResult(final_response="not json"), "invalid NextStep"),
        (
            FakeResult(
                final_response='{"current_situation":"missing required fields"}',
            ),
            "invalid NextStep",
        ),
    ],
)
def test_codex_rejects_incomplete_or_invalid_results(result: FakeResult, message: str) -> None:
    async def scenario() -> None:
        model = CodexSGRModel(client=FakeCodex(result))
        with pytest.raises(CodexDecisionError, match=message):
            await model.decide(_context())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "event_type",
    ["commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView"],
)
def test_codex_rejects_tool_events(event_type: str) -> None:
    async def scenario() -> None:
        result = FakeResult(final_response=_valid_response(), items=[FakeEvent(event_type)])
        model = CodexSGRModel(client=FakeCodex(result))

        with pytest.raises(CodexDecisionError, match="forbidden tool-use event"):
            await model.decide(_context())

    asyncio.run(scenario())


def test_codex_request_failure_directs_operator_to_local_login() -> None:
    class UnauthenticatedCodex:
        async def account(self, **kwargs: Any) -> FakeAccountResponse:
            raise RuntimeError("subscription session is unavailable")

    async def scenario() -> None:
        model = CodexSGRModel(client=UnauthenticatedCodex())
        with pytest.raises(CodexDecisionError, match=r"run `codex login`"):
            await model.decide(_context())

    asyncio.run(scenario())


def test_codex_owned_client_uses_and_cleans_an_isolated_git_workspace(monkeypatch) -> None:
    class FakeOwnedCodex(FakeCodex):
        instance: Any = None

        def __init__(self, config: Any) -> None:
            super().__init__(FakeResult(final_response=_valid_response()))
            self.config = config
            FakeOwnedCodex.instance = self

    monkeypatch.setattr("uptick_agent.llm.codex.AsyncCodex", FakeOwnedCodex)

    async def scenario() -> None:
        model = CodexSGRModel()
        workspace = model._workspace_dir
        assert workspace is not None
        assert (workspace / ".git").is_dir()
        assert FakeOwnedCodex.instance is not None
        assert FakeOwnedCodex.instance.config.cwd == str(workspace)
        assert FakeOwnedCodex.instance.config.config_overrides == CODEX_CONFIG_OVERRIDES

        await model.aclose()

        assert FakeOwnedCodex.instance.closed
        assert not workspace.exists()

    asyncio.run(scenario())


def test_codex_cleans_owned_workspace_when_client_construction_fails(monkeypatch) -> None:
    captured_workspace: Path | None = None

    class FailingCodex:
        def __init__(self, config: Any) -> None:
            nonlocal captured_workspace
            captured_workspace = Path(config.cwd)
            raise RuntimeError("no local Codex runtime")

    monkeypatch.setattr("uptick_agent.llm.codex.AsyncCodex", FailingCodex)

    with pytest.raises(RuntimeError, match="no local Codex runtime"):
        CodexSGRModel()

    assert captured_workspace is not None
    assert not captured_workspace.exists()


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_owned_codex_refuses_api_key_environment(monkeypatch, variable: str) -> None:
    monkeypatch.setenv(variable, "not-a-real-key")

    with pytest.raises(ValueError, match="Unset OPENAI_API_KEY and CODEX_API_KEY"):
        CodexSGRModel()
