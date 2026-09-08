import asyncio

import pytest

from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.models import (
    Capability,
    CapabilityCatalog,
    Observation,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import (
    ReasonerEnvironmentBootstrapper,
    ScriptedEnvironmentBootstrapper,
    validate_strict_bootstrap_artifact,
)
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore, JsonlRunStore


def _catalog() -> CapabilityCatalog:
    return CapabilityCatalog(
        items=[
            Capability(
                name="finish",
                description="finish",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                terminal=True,
            )
        ]
    )


def _finish() -> dict:
    return {
        "phase": "finish",
        "facts": ["the scripted run is complete"],
        "competing_hypotheses": ["finish now"],
        "contradicting_evidence": [],
        "previous_verification": {"status": "not_applicable", "evidence": []},
        "strategy": "finish the scripted run",
        "selected_action": {"name": "finish", "arguments": {}},
        "expected_result": ["the run becomes terminal"],
        "verification": ["the observation is terminal"],
        "task_completed": True,
    }


def _environment(run_id: str) -> ScriptedEnvironment:
    return ScriptedEnvironment(
        name="scripted",
        catalog=_catalog(),
        observations=[Observation(action_kind="finish", summary="done", terminal=True)],
        result=RunResult(
            run_id=run_id,
            status="completed",
            steps=0,
            duration_seconds=0,
            stop_reason="done",
        ),
        bootstrap_text="Scripted environment instructions.",
    )


def _runner(*, environment, store, bootstrapper, artifact=None) -> AgentRunner:
    return AgentRunner(
        agent_core=AgentCore(reasoner=ScriptedReasoner([_finish()]), sgr=CurrentSGR()),
        environment=environment,
        memory=NoMemory(),
        run_store=store,
        policy=DecisionPolicy(),
        bootstrapper=bootstrapper,
        bootstrap_artifact=artifact,
    )


def test_reasoner_bootstrapper_builds_compact_artifact_without_model_generated_hashes() -> None:
    async def scenario() -> None:
        source = "Inspect before changing state."
        reasoner = ScriptedReasoner(
            [
                {
                    "facts": [
                        {
                            "category": "workflow",
                            "statement": "inspect before changing state",
                        }
                    ],
                    "guidance": ["Inspect before changing state."],
                    "tools": [
                        {
                            "capability_name": "finish",
                            "purpose": "finish",
                            "operational_notes": [],
                        }
                    ],
                }
            ]
        )
        artifact = await ReasonerEnvironmentBootstrapper(reasoner=reasoner).build(
            source=source,
            environment_id="scripted",
            capabilities=_catalog(),
        )

        assert artifact.validation.accepted
        assert artifact.bundle.profile.environment_id == "scripted"
        assert artifact.reasoner_telemetry is not None
        assert "credential" not in str(reasoner.requests[0].output_schema).lower()
        assert "sha256" not in str(reasoner.requests[0].output_schema).lower()
        assert "byte_start" not in str(reasoner.requests[0].output_schema)

    asyncio.run(scenario())


def test_strict_bootstrap_validator_accepts_exact_artifact() -> None:
    async def scenario() -> None:
        artifact = await ScriptedEnvironmentBootstrapper().build(
            source="Exact instructions.",
            environment_id="scripted",
            capabilities=_catalog(),
        )

        validate_strict_bootstrap_artifact(
            artifact,
            environment_id="scripted",
            environment_profile_hash=artifact.bundle.profile.profile_version,
        )

    asyncio.run(scenario())


def test_strict_bootstrap_validator_rejects_wrong_environment_or_profile() -> None:
    async def scenario() -> None:
        artifact = await ScriptedEnvironmentBootstrapper().build(
            source="Exact instructions.",
            environment_id="scripted",
            capabilities=_catalog(),
        )

        with pytest.raises(ValueError, match="bootstrap environment does not match"):
            validate_strict_bootstrap_artifact(
                artifact,
                environment_id="other",
                environment_profile_hash=artifact.bundle.profile.profile_version,
            )
        with pytest.raises(ValueError, match="bootstrap profile does not match"):
            validate_strict_bootstrap_artifact(
                artifact,
                environment_id="scripted",
                environment_profile_hash="profile-" + "0" * 64,
            )

    asyncio.run(scenario())


def test_strict_bootstrap_validator_rejects_tampered_source_or_profile_hash() -> None:
    async def scenario() -> None:
        artifact = await ScriptedEnvironmentBootstrapper().build(
            source="Exact instructions.",
            environment_id="scripted",
            capabilities=_catalog(),
        )
        tampered_source = artifact.model_copy(update={"source_text": "Changed instructions."})
        invalid_profile_hash = "profile-" + "0" * 64
        tampered_profile = artifact.model_copy(
            update={
                "bundle": artifact.bundle.model_copy(
                    update={
                        "profile": artifact.bundle.profile.model_copy(
                            update={"profile_version": invalid_profile_hash}
                        )
                    }
                )
            }
        )

        with pytest.raises(ValueError, match="bootstrap source hash is invalid"):
            validate_strict_bootstrap_artifact(
                tampered_source,
                environment_id="scripted",
                environment_profile_hash=artifact.bundle.profile.profile_version,
            )
        with pytest.raises(ValueError, match="bootstrap profile hash is invalid"):
            validate_strict_bootstrap_artifact(
                tampered_profile,
                environment_id="scripted",
                environment_profile_hash=invalid_profile_hash,
            )

    asyncio.run(scenario())


def test_runner_reuses_bootstrap_cache_before_environment_start() -> None:
    class CountingBootstrapper(ScriptedEnvironmentBootstrapper):
        def __init__(self) -> None:
            self.build_calls = 0

        async def build(self, **kwargs):
            self.build_calls += 1
            return await super().build(**kwargs)

    async def scenario() -> None:
        store = InMemoryRunStore()
        bootstrapper = CountingBootstrapper()
        first = _runner(
            environment=_environment("ignored-first"),
            store=store,
            bootstrapper=bootstrapper,
        )
        second = _runner(
            environment=_environment("ignored-second"),
            store=store,
            bootstrapper=bootstrapper,
        )

        await first.run(RunSpec(run_id="run-first", environment="scripted"))
        await second.run(RunSpec(run_id="run-second", environment="scripted"))

        assert bootstrapper.build_calls == 1
        first_manifest = await store.load_manifest("run-first")
        second_manifest = await store.load_manifest("run-second")
        assert first_manifest is not None and not first_manifest.bootstrap_cache_hit
        assert second_manifest is not None and second_manifest.bootstrap_cache_hit
        assert first_manifest.environment_profile_version == (
            second_manifest.environment_profile_version
        )

    asyncio.run(scenario())


def test_strict_artifact_skips_initialize_and_bootstrap_failure_precedes_start() -> None:
    class RecordingEnvironment(ScriptedEnvironment):
        def __init__(self) -> None:
            super().__init__(
                name="scripted",
                catalog=_catalog(),
                observations=[Observation(action_kind="finish", summary="done", terminal=True)],
                result=RunResult(
                    run_id="ignored",
                    status="completed",
                    steps=0,
                    duration_seconds=0,
                    stop_reason="done",
                ),
                bootstrap_text="Strict instructions.",
            )
            self.initialize_calls = 0
            self.start_calls = 0

        async def initialize(self) -> str:
            self.initialize_calls += 1
            return await super().initialize()

        async def start(self, spec: RunSpec):
            self.start_calls += 1
            return await super().start(spec)

    class FailingBootstrapper(ScriptedEnvironmentBootstrapper):
        async def build(self, **kwargs):
            del kwargs
            raise RuntimeError("bootstrap failed")

    async def scenario() -> None:
        bootstrapper = ScriptedEnvironmentBootstrapper()
        artifact = await bootstrapper.build(
            source="Strict instructions.",
            environment_id="scripted",
            capabilities=_catalog(),
        )
        strict_environment = RecordingEnvironment()
        strict_store = InMemoryRunStore()
        result = await _runner(
            environment=strict_environment,
            store=strict_store,
            bootstrapper=bootstrapper,
            artifact=artifact,
        ).run(RunSpec(run_id="run-strict", environment="scripted"))

        assert result.run_id == "run-strict"
        assert strict_environment.initialize_calls == 0
        assert strict_environment.start_calls == 1

        failing_environment = RecordingEnvironment()
        failing_store = InMemoryRunStore()
        with pytest.raises(RuntimeError, match="bootstrap failed"):
            await _runner(
                environment=failing_environment,
                store=failing_store,
                bootstrapper=FailingBootstrapper(),
            ).run(RunSpec(run_id="run-failed-bootstrap", environment="scripted"))
        assert failing_environment.start_calls == 0
        assert failing_store.events == []
        assert failing_store.manifests == {}

    asyncio.run(scenario())


def test_jsonl_bootstrap_artifact_round_trip_is_atomic(tmp_path) -> None:
    async def scenario() -> None:
        bootstrapper = ScriptedEnvironmentBootstrapper()
        artifact = await bootstrapper.build(
            source="Stored instructions.",
            environment_id="scripted",
            capabilities=_catalog(),
        )
        store = JsonlRunStore(tmp_path)
        await store.save_bootstrap(artifact)
        await store.save_bootstrap(artifact)

        assert await store.load_bootstrap(artifact.identity) == artifact
        assert len(list((tmp_path / "bootstrap").glob("*.json"))) == 1
        assert not list((tmp_path / "bootstrap").glob("*.tmp"))

    asyncio.run(scenario())


def test_revised_extractor_does_not_reuse_previous_prompt_cache(tmp_path) -> None:
    async def scenario() -> None:
        source = "Inspect before changing state."
        draft = {
            "facts": [{"category": "workflow", "statement": source}],
            "guidance": [source],
            "tools": [{"capability_name": "finish", "purpose": "finish", "operational_notes": []}],
        }
        bootstrapper = ReasonerEnvironmentBootstrapper(reasoner=ScriptedReasoner([draft]))
        current = await bootstrapper.build(
            source=source, environment_id="scripted", capabilities=_catalog()
        )
        previous = current.model_copy(
            update={
                "identity": current.identity.model_copy(
                    update={"bootstrap_prompt_version": "environment-bootstrap-v2"}
                )
            }
        )
        store = JsonlRunStore(tmp_path)
        await store.save_bootstrap(previous)

        assert await store.load_bootstrap(current.identity) is None
        assert await store.load_bootstrap(previous.identity) == previous
        await store.save_bootstrap(current)
        assert await store.load_bootstrap(current.identity) == current
        assert await store.load_bootstrap(previous.identity) == previous
        assert len(list((tmp_path / "bootstrap").glob("*.json"))) == 2

    asyncio.run(scenario())
