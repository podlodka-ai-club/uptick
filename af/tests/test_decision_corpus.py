import asyncio

import pytest

from uptick_agent import composition
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.errors import ReasonerFailure, RunExecutionError
from uptick_agent.core.models import (
    DEFAULT_OBJECTIVE,
    Capability,
    CapabilityCatalog,
    Observation,
    ReasonerConfig,
    ReasoningTelemetry,
    RunMetadata,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.decision_corpus import (
    export_ad_hoc_corpus,
    export_decision_corpus,
    replay_decision,
)
from uptick_agent.environments.discovered.session import DiscoveredRunResult
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.experiments import ExperimentAttempt, ExperimentSpec, summarize_experiment
from uptick_agent.memory import NoMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def _catalog() -> CapabilityCatalog:
    schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    return CapabilityCatalog(
        items=[
            Capability(name="inspect", description="inspect", input_schema=schema),
            Capability(
                name="finish",
                description="finish",
                input_schema=schema,
                terminal=True,
            ),
        ]
    )


def _output(
    name: str,
    *,
    situation: str = "ready",
    completed: bool = False,
    previous: bool = False,
) -> dict:
    return {
        "phase": "finish" if completed else "observe",
        "facts": [situation],
        "competing_hypotheses": [f"try {name}"],
        "contradicting_evidence": [],
        "previous_verification": {
            "status": "confirmed" if previous else "not_applicable",
            "evidence": ["prior observation arrived"] if previous else [],
        },
        "strategy": "follow the replayable scripted sequence",
        "selected_action": {"name": name, "arguments": {}},
        "expected_result": ["the scripted observation arrives"],
        "verification": ["compare the observation with the expected script"],
        "task_completed": completed,
    }


def _run_spec() -> RunSpec:
    return RunSpec(
        run_id="run-corpus",
        environment="scripted",
        parameters={"seed": 1},
        step_limit=2,
        metadata=RunMetadata(
            reasoner=ReasonerConfig(provider="scripted", model="scripted", thread_mode="stateless"),
            memory_mode="none",
            git_revision="a" * 40,
            git_dirty=False,
            uv_lock_sha256="b" * 64,
            python_version="3.12.0",
            simulator_endpoint_sha256="c" * 64,
        ),
    )


async def _completed_corpus(operator_guidance: str | None = None):
    store = InMemoryRunStore()
    environment = ScriptedEnvironment(
        name="scripted",
        catalog=_catalog(),
        observations=[
            Observation(action_kind="inspect", summary="observed"),
            Observation(action_kind="finish", summary="done", terminal=True),
        ],
        result=DiscoveredRunResult(
            run_id="corpus-run",
            status="completed",
            steps=0,
            duration_seconds=0,
            stop_reason="done",
            simulator_run_id="sim-corpus-run",
            objective=DEFAULT_OBJECTIVE,
            final_state={
                "evaluation": {"score": 87},
                "costs": {"total_cost_minor": 42},
                "availability": {"uptime_ratio": 0.999, "slo_passed": True},
            },
        ),
    )
    runner = AgentRunner(
        agent_core=AgentCore(
            reasoner=ScriptedReasoner(
                [_output("inspect"), _output("finish", completed=True, previous=True)]
            ),
            sgr=CurrentSGR(operator_guidance=operator_guidance),
        ),
        environment=environment,
        memory=NoMemory(),
        run_store=store,
        policy=DecisionPolicy(),
        bootstrapper=ScriptedEnvironmentBootstrapper(),
    )
    run_spec = _run_spec()
    run_spec.metadata.operator_guidance = operator_guidance
    result = await runner.run(run_spec)
    assert isinstance(result, DiscoveredRunResult)
    return await export_ad_hoc_corpus(result.run_id, store)


async def _completed_experiment_report():
    bootstrapper = ScriptedEnvironmentBootstrapper()
    bootstrap = await bootstrapper.build(
        source="Environment uptickv2 exposes scripted tools.",
        environment_id="uptickv2",
        capabilities=_catalog(),
    )
    reasoner_config = ReasonerConfig(
        provider="scripted",
        model="scripted",
        thread_mode="stateless",
    )
    spec = ExperimentSpec(
        experiment_id="corpus-export",
        environment_profile_hash=bootstrap.bundle.profile.profile_version,
        reasoner=reasoner_config,
        seeds=[7],
        max_iterations=2,
    )
    run_id = "run-experiment-corpus"
    store = InMemoryRunStore()
    runner = AgentRunner(
        agent_core=AgentCore(
            reasoner=ScriptedReasoner(
                [_output("inspect"), _output("finish", completed=True, previous=True)]
            ),
            sgr=CurrentSGR(),
        ),
        environment=ScriptedEnvironment(
            name="uptickv2",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary="observed"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=DiscoveredRunResult(
                run_id=run_id,
                status="completed",
                steps=0,
                duration_seconds=0,
                stop_reason="done",
                simulator_run_id="sim-experiment-corpus",
                objective=DEFAULT_OBJECTIVE,
                final_state={
                    "evaluation": {"score": 91},
                    "costs": {"total_cost_minor": 25},
                    "availability": {"uptime_ratio": 1.0, "slo_passed": True},
                },
            ),
        ),
        memory=NoMemory(),
        run_store=store,
        policy=DecisionPolicy(),
        bootstrapper=bootstrapper,
        bootstrap_artifact=bootstrap,
    )
    result = await runner.run(
        RunSpec(
            run_id=run_id,
            environment="uptickv2",
            parameters={"seed": 7},
            step_limit=2,
            metadata=RunMetadata(
                reasoner=reasoner_config,
                memory_mode="none",
                experiment_id=spec.experiment_id,
                repeat_index=0,
                resolved_spec_sha256=spec.sha256(),
                uv_lock_sha256="b" * 64,
                python_version="3.12.0",
                simulator_endpoint_sha256="c" * 64,
            ),
        )
    )
    assert isinstance(result, DiscoveredRunResult)
    report = summarize_experiment(
        spec,
        [ExperimentAttempt(seed=7, repeat_index=0, run_id=run_id, result=result)],
    )
    return report, store


@pytest.mark.parametrize("guidance", [None, "Saved guidance, even after its file changes.\r\n"])
def test_composed_replay_uses_manifest_guidance_snapshot(monkeypatch, guidance: str | None) -> None:
    corpus = asyncio.run(_completed_corpus(guidance))
    assert corpus.runs[0].manifest.operator_guidance == guidance
    # Exercise persisted JSON, not just a shared in-memory string.
    corpus = type(corpus).model_validate_json(corpus.model_dump_json())
    source = corpus.runs[0].decisions[0]

    class ReplayReasoner(ScriptedReasoner):
        async def aclose(self) -> None:
            return None

    reasoner = ReplayReasoner([_output("inspect")])
    monkeypatch.setattr(composition, "_load_codex_reasoner", lambda: lambda **kwargs: reasoner)
    config = ReasonerConfig(provider="codex", model="test", thread_mode="ephemeral")
    report = asyncio.run(
        composition.run_replay(
            corpus=corpus,
            decision_id=source.decision_id,
            reasoner_config=config,
            repeats=1,
            live=True,
        )
    )
    assert report.stability.schema_valid == 1
    assert (
        reasoner.requests[0].system_prompt
        == CurrentSGR(operator_guidance=guidance).build_request(source.context).system_prompt
    )
    if guidance is not None:
        assert guidance not in reasoner.requests[0].user_prompt


def test_replay_rejects_changed_guidance_before_model_call() -> None:
    corpus = asyncio.run(_completed_corpus("Saved guidance."))
    reasoner = ScriptedReasoner([])
    with pytest.raises(ValueError, match="saved system prompt hash"):
        asyncio.run(
            replay_decision(
                corpus,
                decision_id=corpus.runs[0].decisions[0].decision_id,
                repeats=1,
                reasoner_config=ReasonerConfig(
                    provider="scripted", model="scripted", thread_mode="stateless"
                ),
                agent_core=AgentCore(
                    reasoner=reasoner, sgr=CurrentSGR(operator_guidance="Changed guidance.")
                ),
                policy=DecisionPolicy(),
            )
        )
    assert reasoner.requests == []


def test_corpus_links_context_decision_observation_and_final_outcome_once() -> None:
    corpus = asyncio.run(_completed_corpus())

    assert len(corpus.runs) == 1
    run = corpus.runs[0]
    assert run.final_outcome is not None
    final_state = run.final_outcome["final_state"]
    assert isinstance(final_state, dict)
    evaluation = final_state["evaluation"]
    assert isinstance(evaluation, dict)
    assert evaluation["score"] == 87
    assert corpus.spec is None
    assert corpus.resolved_spec_sha256 is None
    assert run.final_failure is None
    assert len(run.decisions) == 2
    assert len({item.decision_id for item in run.decisions}) == 2
    first = run.decisions[0]
    assert first.context.memory_brief.lessons == []
    assert first.context.memory_brief.similar_episodes == []
    assert [item.name for item in first.context.capabilities.items] == ["inspect", "finish"]
    assert first.decision is not None
    assert first.following_observation is not None
    assert first.following_observation.summary == "observed"


def test_experiment_report_exports_linked_offline_corpus() -> None:
    async def scenario() -> None:
        report, store = await _completed_experiment_report()

        corpus = await export_decision_corpus(report, store)

        assert corpus.spec == report.spec
        assert corpus.resolved_spec_sha256 == report.resolved_spec_sha256
        assert len(corpus.runs) == 1
        run = corpus.runs[0]
        assert run.run_id == "run-experiment-corpus"
        assert run.seed == 7
        assert run.repeat_index == 0
        assert run.manifest.experiment_id == report.spec.experiment_id
        assert len(run.decisions) == 2
        assert run.final_outcome is not None
        assert run.final_outcome["final_state"] == {
            "evaluation": {"score": 91},
            "costs": {"total_cost_minor": 25},
            "availability": {"uptime_ratio": 1.0, "slo_passed": True},
        }

    asyncio.run(scenario())


def test_experiment_report_export_rejects_mismatched_manifest_provenance() -> None:
    async def scenario() -> None:
        report, store = await _completed_experiment_report()
        run_id = "run-experiment-corpus"
        manifest = await store.load_manifest(run_id)
        assert manifest is not None
        mismatches = [
            ({"experiment_id": "other"}, "experiment_id"),
            ({"seed": 8}, "run key"),
            ({"repeat_index": 1}, "run key"),
            ({"resolved_spec_sha256": "d" * 64}, "spec hash"),
        ]
        for update, message in mismatches:
            store.manifests[run_id] = manifest.model_copy(update=update)
            with pytest.raises(ValueError, match=message):
                await export_decision_corpus(report, store)

    asyncio.run(scenario())


def test_corpus_keeps_provider_failed_decision_context() -> None:
    class FailingReasoner:
        async def reason(self, request):
            del request
            raise ReasonerFailure(
                "failed",
                category="transient",
                telemetry=ReasoningTelemetry(
                    provider="scripted",
                    requested_model="scripted",
                    thread_mode="stateless",
                    attempts=1,
                    duration_seconds=0,
                    sdk_name="scripted",
                    sdk_version="1",
                ),
            )

    async def scenario() -> None:
        store = InMemoryRunStore()
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=FailingReasoner(), sgr=CurrentSGR()),
            environment=ScriptedEnvironment(
                name="scripted",
                catalog=_catalog(),
                observations=[],
                result=DiscoveredRunResult(
                    run_id="run-corpus",
                    status="failed",
                    steps=0,
                    duration_seconds=0,
                    stop_reason="failed",
                    simulator_run_id="sim-run-corpus",
                    objective=DEFAULT_OBJECTIVE,
                    final_state={
                        "costs": {"total_cost_minor": 0},
                        "availability": {"uptime_ratio": None, "slo_passed": None},
                    },
                ),
            ),
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        with pytest.raises(RunExecutionError):
            await runner.run(_run_spec())
        corpus = await export_ad_hoc_corpus("run-corpus", store)

        assert corpus.runs[0].final_failure is not None
        assert len(corpus.runs[0].decisions) == 1
        decision = corpus.runs[0].decisions[0]
        assert decision.decision is None
        assert decision.failure is not None
        assert decision.failure.category == "transient"

    asyncio.run(scenario())


def test_replay_measures_action_behavior_and_full_envelope_stability() -> None:
    async def scenario() -> None:
        corpus = await _completed_corpus()
        source = corpus.runs[0].decisions[0]
        reasoner = ScriptedReasoner(
            [
                _output("inspect", situation="one"),
                _output("inspect", situation="two"),
                _output("unknown", situation="three"),
            ]
        )
        report = await replay_decision(
            corpus,
            decision_id=source.decision_id,
            repeats=3,
            reasoner_config=reasoner.config,
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            policy=DecisionPolicy(),
        )

        stability = report.stability
        assert stability.requested == 3
        assert stability.schema_valid == 3
        assert stability.schema_failures == 0
        assert stability.provider_failures == 0
        assert stability.policy_rejections == 1
        assert stability.capability_call.modal_count == 2
        assert stability.capability_call.unique_count == 2
        assert stability.capability_call.agreement_among_valid == 2 / 3
        assert stability.capability_call.agreement_among_requested == 2 / 3
        assert stability.behavioral_signature.modal_count == 2
        assert stability.full_envelope.modal_count == 1
        assert len(stability.full_envelope.modal_signatures) == 3

    asyncio.run(scenario())


def test_replay_counts_schema_and_provider_failures_and_guards_hashes() -> None:
    async def scenario() -> None:
        corpus = await _completed_corpus()
        source = corpus.runs[0].decisions[0]
        reasoner = ScriptedReasoner([_output("inspect"), {"not": "an envelope"}])
        report = await replay_decision(
            corpus,
            decision_id=source.decision_id,
            repeats=3,
            reasoner_config=reasoner.config,
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            policy=DecisionPolicy(),
        )

        assert report.stability.schema_valid == 1
        assert report.stability.schema_failures == 1
        assert report.stability.provider_failures == 1
        assert report.stability.capability_call.agreement_among_valid is None
        assert report.stability.capability_call.agreement_among_requested is None

        tampered = corpus.model_copy(deep=True)
        tampered.runs[0].decisions[0].context.objective = "tampered"
        untouched_reasoner = ScriptedReasoner([_output("inspect")])
        with pytest.raises(ValueError, match="AgentContext hash"):
            await replay_decision(
                tampered,
                decision_id=source.decision_id,
                repeats=1,
                reasoner_config=untouched_reasoner.config,
                agent_core=AgentCore(reasoner=untouched_reasoner, sgr=CurrentSGR()),
                policy=DecisionPolicy(),
            )
        assert untouched_reasoner.requests == []

    asyncio.run(scenario())
