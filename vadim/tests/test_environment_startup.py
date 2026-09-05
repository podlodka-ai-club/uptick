"""External startup inputs are fixed before the provider is invoked."""

from __future__ import annotations

import asyncio
import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from uptick_agent import cli
from uptick_agent.decisions.contracts import V2NextStep
from uptick_agent.decisions.runtime import RuntimeDecisionContext, ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.llm.decision_model import StructuredDecisionModel
from uptick_agent.memory import legacy_memory_runtime
from uptick_agent.runs.config import AgentConfig
from uptick_agent.simulator.actions import GetMetrics


def test_cli_uses_actual_startup_text_before_constructing_model(tmp_path, monkeypatch):
    from test_simulator_v2_environment import FakeV2Client

    startup = "Externally supplied world instructions, fixed for this run.\n"
    events = []
    requests = []

    class Client(FakeV2Client):
        async def start(self, **kwargs):
            events.append("start")
            return {**await super().start(**kwargs), "commands_markdown": startup}

        async def aclose(self):
            events.append("client.closed")

    class Provider:
        model = "test"

        async def generate_structured(self, request):
            events.append("decision")
            requests.append(request)
            return SimpleNamespace(
                value=V2NextStep(
                    current_situation="Observe the world",
                    hypothesis="metrics show state",
                    remaining_steps=[],
                    task_completed=False,
                    action=GetMetrics(),
                )
            )

        async def aclose(self):
            events.append("model.closed")

    client = Client()
    monkeypatch.setattr(cli, "SimulatorV2Client", lambda _url: client)

    def factory(args, spec):
        assert events == ["start"]
        saved = list(tmp_path.glob("seed-42/artifacts/startup_spec/*.json"))
        assert len(saved) == 1
        value = json.loads(saved[0].read_text())["value"]
        assert value["spec"]["environment_briefing"] == startup
        assert value["spec_fingerprint"] == spec.fingerprint
        events.append("construct")
        return StructuredDecisionModel(
            Provider(),
            response_model=spec.response_model,
            environment_briefing=spec.environment_briefing,
        )

    monkeypatch.setattr(cli, "_decision_model", factory)
    args = cli._parser().parse_args(
        [
            "run",
            "--seed",
            "42",
            "--max-steps",
            "2",
            "--artifacts",
            str(tmp_path),
        ]
    )
    result = asyncio.run(
        cli._run_seed(
            args,
            AgentConfig(max_steps=2),
            legacy_memory_runtime(None),
            42,
        )
    )
    assert result.run_id == "run-1"
    assert len(requests) == 2
    assert all(request.messages[0].content.endswith(startup) for request in requests)
    assert requests[0].messages[0] == requests[1].messages[0]
    assert events == ["start", "construct", "decision", "decision", "model.closed", "client.closed"]


def test_model_startup_prompt_is_read_only_and_schema_mutation_fails_before_request(monkeypatch):
    from pydantic import BaseModel

    class Decision(BaseModel):
        value: int

    spec = EnvironmentDecisionSpec(Decision, "External instructions")
    model = StructuredDecisionModel(
        SimpleNamespace(model="test"),
        response_model=Decision,
        environment_briefing=spec.environment_briefing,
    )
    with pytest.raises(AttributeError):
        model.system_prompt = "changed"
    with pytest.raises(AttributeError):
        model.response_model = BaseModel
    monkeypatch.setattr(Decision, "model_json_schema", classmethod(lambda cls: {"changed": True}))
    context = RuntimeDecisionContext(
        objective="test",
        run_id="run",
        seed=1,
        iteration=1,
        max_steps=1,
        latest_result=ToolResult(action_kind="start", summary="start"),
    )
    with pytest.raises(ValueError, match="schema changed"):
        model.prompt_trace(context)
    with pytest.raises(ValueError, match="schema changed"):
        spec.public_input()


def test_preregistered_prompt_mismatch_never_constructs_provider(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr(cli, "LlmProviderRegistry", unexpected)
    profile = SimpleNamespace(
        provider=SimpleNamespace(prompt_fingerprint=cli._prompt_fingerprint("expected"))
    )
    with pytest.raises(ValueError, match="differs from the preregistered"):
        cli._v2_model_factory(
            profile, SimpleNamespace(), EnvironmentDecisionSpec(V2NextStep, "changed")
        )


@pytest.mark.parametrize("startup_available", [True, False])
def test_startup_mismatch_retains_physical_ids_and_actual_inputs(startup_available):
    from test_evaluation_runtime import _binding_factory, _Environment, _manifest, _Memory

    from uptick_agent.evaluation.artifacts import InMemoryEvaluationArtifactStore
    from uptick_agent.evaluation.execution import EvaluationRuntime
    from uptick_agent.evaluation.lifecycle import EvaluationJournal

    class Environment(_Environment):
        @property
        def decision_spec(self):
            if not startup_available:
                raise RuntimeError("startup document missing")
            return EnvironmentDecisionSpec(V2NextStep, "different external startup input")

        def public_state(self, session):
            return {}

    manifest = _manifest()
    artifacts = InMemoryEvaluationArtifactStore()
    events = []

    def factory(block, condition, attempt, run_id, spec):
        assert ("startup_spec", attempt.attempt_id) in artifacts.artifacts
        return cli._v2_model_factory(manifest.profile, SimpleNamespace(), spec)

    runtime = EvaluationRuntime(
        manifest,
        environment_factory=lambda *args: Environment(events),
        model_factory=factory,
        memory_factory=lambda *args: _Memory(),
        binding_factory=_binding_factory(manifest, []),
        journal=EvaluationJournal(manifest, artifacts=artifacts),
    )
    report = asyncio.run(runtime.run())
    assert len(report.retained_attempts) == 4
    started = [row for row in report.retained_attempts if row.run_id is not None]
    assert len(started) == 4
    assert all(row.status == "failed" for row in started)
    assert all(("startup_observation", row.attempt_id) in artifacts.artifacts for row in started)
    assert all(
        (("startup_spec", row.attempt_id) in artifacts.artifacts) == startup_available
        for row in started
    )
    assert not any(event[0] == "model.decide" for event in events)

    verifier = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_v2_experiment.py"))
    verifier["_verify_artifact_links"](artifacts.artifacts, manifest, report)
    changed = copy.deepcopy(artifacts.artifacts)
    evidence = changed[("startup_observation", started[0].attempt_id)]
    evidence["value"]["observation"] = {"replaced": True}
    evidence["hash"] = verifier["sha256_json"](evidence["value"])
    with pytest.raises(ValueError, match="hash does not match"):
        verifier["_verify_artifact_links"](changed, manifest, report)


def test_missing_spec_after_start_finalizes_failed_memory_outcome(tmp_path):
    from test_simulator_v2_environment import FakeV2Client

    from uptick_agent.composition.memory import compose_experimental_runtime
    from uptick_agent.memory.config import MemoryConfiguration
    from uptick_agent.memory.stores import SqliteStructuredStore
    from uptick_agent.runs.execute import AgentRunner
    from uptick_agent.simulator.v2_environment import SimulatorV2Environment

    class Client(FakeV2Client):
        async def start(self, **kwargs):
            response = await super().start(**kwargs)
            response.pop("commands_markdown", None)
            return response

    async def scenario():
        store = SqliteStructuredStore(tmp_path / "failed-start.sqlite")
        memory = compose_experimental_runtime(
            MemoryConfiguration.episodic_only(),
            store,
            namespace="startup-failure",
        )
        with pytest.raises(RuntimeError, match="commands_markdown"):
            await AgentRunner(
                config=AgentConfig(max_steps=1),
                model=object(),
                memory=memory,
                environment=SimulatorV2Environment(Client()),
            ).run(42)
        records = await store.list(namespace="startup-failure")
        outcomes = [record for record in records if record.record_type == "run-outcome"]
        assert len(outcomes) == 1
        assert outcomes[0].payload["run_id"] == "run-1"
        assert outcomes[0].payload["status"] == "failed"

    asyncio.run(scenario())
