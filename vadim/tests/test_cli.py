import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from uptick_agent import cli
from uptick_agent.llm import StructuredGenerationRequest, serialize_structured_generation_request
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.models import DecisionContext, NextStep, ToolResult


def test_cli_uses_codex_model_default_and_cli_override_without_api_keys(monkeypatch) -> None:
    created: list[object] = []

    class FakeCodexClient:
        def __init__(self, model: str | None) -> None:
            self.model = model

    class FakeCodexFactory:
        def create(self, config):
            client = FakeCodexClient(config.model)
            created.append(client)
            return client

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_MODEL", "codex-from-env")
    monkeypatch.setattr(cli, "_load_codex_factory", lambda: FakeCodexFactory)

    args = cli._parser().parse_args(["run", "--seed", "1", "--decision-provider", "codex"])
    model = cli._decision_model(args)
    assert model.model == "codex-from-env"

    overridden_args = cli._parser().parse_args(
        ["run", "--seed", "1", "--decision-provider", "codex", "--model", "chosen-codex"]
    )
    overridden_model = cli._decision_model(overridden_args)
    assert overridden_model.model == "chosen-codex"
    assert len(created) == 2


def test_cli_prompt_trace_matches_the_neutral_request_sent_to_client() -> None:
    class FakeClient:
        model = "cli-model"

        def __init__(self) -> None:
            self.requests: list[StructuredGenerationRequest[Any]] = []

        async def generate_structured(self, request: StructuredGenerationRequest[Any]):
            self.requests.append(request)
            return SimpleNamespace(
                value=NextStep.model_validate(
                    {
                        "current_situation": "started",
                        "hypothesis": "inspect",
                        "remaining_steps": [],
                        "task_completed": False,
                        "action": {"kind": "get_overview"},
                    }
                )
            )

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        client = FakeClient()
        model = cli.StructuredDecisionModel(client)
        context = DecisionContext(
            objective="keep healthy",
            run_id="run-1",
            seed=1,
            iteration=1,
            max_steps=2,
            latest_result=ToolResult(action_kind="start", summary="started"),
        )

        trace = model.prompt_trace(context)
        await model.decide(context)

        assert client.requests
        assert trace == serialize_structured_generation_request(client.requests[0])
        assert trace["model"] == "cli-model"
        assert trace["messages"][1]["content"].endswith(context.model_dump_json(indent=2))

    asyncio.run(scenario())


def test_cli_selects_openai_through_registry_with_cli_over_environment(monkeypatch) -> None:
    created = []

    class FakeClient:
        def __init__(self, model: str | None) -> None:
            self.model = model

    class FakeFactory:
        def __init__(self, **kwargs) -> None:
            self.options = kwargs

        def create(self, config):
            client = FakeClient(config.model)
            created.append(client)
            return client

    monkeypatch.setattr(cli, "OpenAIProviderFactory", FakeFactory)
    monkeypatch.setenv("OPENAI_MODEL", "openai-from-env")

    args = cli._parser().parse_args(["run", "--seed", "1"])
    assert cli._decision_model(args).model == "openai-from-env"

    overridden = cli._parser().parse_args(["run", "--seed", "1", "--model", "chosen"])
    assert cli._decision_model(overridden).model == "chosen"
    assert len(created) == 2


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_cli_refuses_codex_provider_when_an_api_key_is_set(monkeypatch, variable: str) -> None:
    monkeypatch.setenv(variable, "not-a-real-key")
    args = cli._parser().parse_args(["run", "--seed", "1", "--decision-provider", "codex"])

    with pytest.raises(ValueError, match="Unset OPENAI_API_KEY and CODEX_API_KEY"):
        cli._decision_model(args)


@pytest.mark.parametrize("provider", ["not-a-provider", " codex "])
def test_cli_rejects_invalid_or_whitespace_padded_provider_environment(
    monkeypatch, provider: str
) -> None:
    monkeypatch.setenv("DECISION_PROVIDER", provider)

    with pytest.raises(ValueError, match="DECISION_PROVIDER must be exactly"):
        cli._parser()


def test_cli_rejects_unknown_provider_internally(monkeypatch) -> None:
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    args = cli._parser().parse_args(["run", "--seed", "1"])
    args.decision_provider = "unexpected"

    with pytest.raises(ValueError, match="Unsupported decision provider"):
        cli._decision_model(args)


def test_cli_validates_config_before_constructing_a_codex_model(monkeypatch) -> None:
    created: list[object] = []

    class FakeCodexFactory:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_codex_factory", lambda: FakeCodexFactory)
    args = cli._parser().parse_args(
        ["run", "--seed", "1", "--max-steps", "0", "--decision-provider", "codex"]
    )

    with pytest.raises(ValidationError):
        asyncio.run(cli._main(args))

    assert created == []


def test_cli_builds_trace_names_for_run_and_benchmark() -> None:
    run_args = cli._parser().parse_args(["run", "--seed", "7"])
    benchmark_args = cli._parser().parse_args(
        ["benchmark", "--name", "memory-study", "--seeds", "1,2"]
    )

    assert cli._trace_name(run_args) == "seed-7"
    assert cli._trace_name(benchmark_args) == "memory-study"


def test_cli_rejects_invalid_benchmark_seeds_before_constructing_clients(monkeypatch) -> None:
    constructed = False

    def decision_model(args):
        nonlocal constructed
        constructed = True
        raise AssertionError("must not construct")

    monkeypatch.setattr(cli, "_decision_model", decision_model)
    args = cli._parser().parse_args(["benchmark", "--name", "invalid", "--seeds", ","])

    with pytest.raises(ValueError, match="at least one seed"):
        asyncio.run(cli._main(args))

    assert constructed is False


@pytest.mark.parametrize(
    "memory_configuration",
    [
        MemoryConfiguration.legacy_baseline(),
        MemoryConfiguration(xmemory=None),
        MemoryConfiguration(
            schema_version="1.3",
            xmemory=ModuleConfig(enabled=False),
        ),
    ],
)
def test_old_or_disabled_memory_profiles_pass_xmemory_preflight(memory_configuration) -> None:
    profile = SimpleNamespace(
        conditions=(
            SimpleNamespace(condition_id="legacy", memory_configuration=memory_configuration),
        )
    )

    cli._reject_unsupported_xmemory(profile)


def test_evaluate_v2_rejects_enabled_xmemory_before_clients_or_artifacts(
    monkeypatch, tmp_path
) -> None:
    memory_configuration = MemoryConfiguration(
        schema_version="1.3",
        xmemory=ModuleConfig(enabled=True, version="xmemory-1.0"),
    )
    profile = SimpleNamespace(
        simulator_api_version="v2",
        conditions=(
            SimpleNamespace(
                condition_id="external-memory",
                memory_configuration=memory_configuration,
            ),
        ),
    )
    manifest = SimpleNamespace(profile=profile)
    monkeypatch.setattr(cli, "_load_v2_manifest", lambda path: manifest)

    def unexpected_call(*args, **kwargs):
        raise AssertionError("evaluate-v2 preflight must run before this dependency")

    for name in (
        "_verify_v2_pins",
        "FilesystemEvaluationArtifactStore",
        "SqliteStructuredStore",
        "DefaultEvaluationMemoryFactory",
        "EvaluationRuntime",
        "SimulatorV2Client",
        "_v2_model_factory",
    ):
        monkeypatch.setattr(cli, name, unexpected_call)

    args = cli._parser().parse_args(
        [
            "evaluate-v2",
            "--profile",
            str(tmp_path / "profile.json"),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )

    with pytest.raises(ValueError, match="immutable snapshot export is unsupported"):
        asyncio.run(cli._evaluate_v2(args))
