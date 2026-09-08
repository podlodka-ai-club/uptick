import asyncio
import json
from pathlib import Path

from tests.helpers import decode_prompt_context, make_bootstrap_artifact, make_context
from tests.test_runner import _catalog, _output
from tests.test_sqlite_memory import _episode, _lesson, _recall, _view
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.bootstrap_models import EnvironmentBootstrapArtifact, EnvironmentFact
from uptick_agent.core.memory_models import (
    ConsolidationQuery,
    EpisodeRecord,
    MemoryViewRequest,
)
from uptick_agent.core.models import (
    AgentContext,
    MemoryBrief,
    Observation,
    RecalledLesson,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.core.trace_models import EpisodeClosedPayload
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.learning.consolidator import _input_json
from uptick_agent.memory import NoMemory, SQLiteMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def test_cached_bootstrap_facts_and_original_decision_evidence_reach_their_consumers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        facts = [f"fact-{index}: " + "x" * 480 for index in range(6)]
        first = _output("inspect")
        first["facts"] = facts
        first["strategy"] = "Resolve the observed bottleneck."
        second = _output("inspect")
        second["facts"] = ["Intervening observation, not the original decision's facts."]
        second["previous_verification"] = {"status": "pending", "evidence": []}
        third = _output("finish", completed=True, previous=True)
        reasoner = ScriptedReasoner([first, second, third])

        class VerboseEnvironment(ScriptedEnvironment):
            async def start(self, spec):
                state = await super().start(spec)
                state.latest_observation.summary = "json-prefix" * 100
                return state

        environment = VerboseEnvironment(
            name="fake",
            catalog=_catalog(),
            observations=[
                Observation(action_kind="inspect", summary="first observed result"),
                Observation(action_kind="inspect", summary="later observed result"),
                Observation(action_kind="finish", summary="done", terminal=True),
            ],
            result=RunResult(
                run_id="ignored", status="completed", steps=0, duration_seconds=0, stop_reason=""
            ),
        )
        # An old-style cached artifact has facts in profile and none in brief.
        source = await environment.initialize()
        artifact = await ScriptedEnvironmentBootstrapper().build(
            source=source, environment_id="fake", capabilities=_catalog()
        )
        # Build a valid identity for the custom fact via the same canonical version function.
        from uptick_agent.runtime.bootstrap import environment_profile_version

        fact = EnvironmentFact(category="economics", statement="Unit cost is 17 per second.")
        artifact.bundle.profile.facts = [fact]
        version = environment_profile_version(
            environment_id="fake",
            facts=[fact],
            guidance=artifact.bundle.brief.guidance,
            registry=artifact.bundle.registry,
            capability_catalog_sha256=artifact.bundle.capability_catalog_sha256,
        )
        artifact.bundle.profile.profile_version = version
        artifact.bundle.brief.profile_version = version
        cached = EnvironmentBootstrapArtifact.model_validate_json(artifact.model_dump_json())
        assert "facts" not in cached.bundle.brief.model_dump()
        store = InMemoryRunStore()
        await store.save_bootstrap(cached)
        runner = AgentRunner(
            agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
            environment=environment,
            memory=NoMemory(),
            run_store=store,
            policy=DecisionPolicy(),
            bootstrapper=ScriptedEnvironmentBootstrapper(),
        )
        await runner.run(RunSpec(run_id="run-fidelity", environment="fake", step_limit=3))
        first_context = decode_prompt_context(reasoner.requests[0].user_prompt)
        assert first_context["environment_profile"]["facts"] == [fact.model_dump()]
        pending_context = decode_prompt_context(reasoner.requests[2].user_prompt)
        assert pending_context["agent_working_state"]["open_decision"]["facts"] == facts
        closed = [e.payload for e in store.events if isinstance(e.payload, EpisodeClosedPayload)]
        assert len(closed) == 1
        episode = closed[0].episode
        assert episode.facts == facts
        assert episode.strategy == first["strategy"]
        assert episode.situation_summary.startswith("facts=fact-0:")
        assert "later observed result" in episode.observation_summary
        assert episode.verification.evidence == third["previous_verification"]["evidence"]
        path = tmp_path / "episodes.sqlite"
        memory = SQLiteMemory(path)
        view_request = MemoryViewRequest(environment_id="fake", environment_profile_version=version)
        view = await memory.resolve_view(view_request)
        assert view.revision is not None
        await memory.record_episode(episode, view.revision)
        # Reopen storage to exercise canonical JSON persistence and learner retrieval.
        restored_memory = SQLiteMemory(path)
        batch = await restored_memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=await restored_memory.resolve_view(view_request),
                environment_id="fake",
                environment_profile_version=version,
                min_evidence_groups=1,
            )
        )
        assert batch.episodes == [episode]
        learner_input = json.loads(_input_json(batch, cached.bundle.registry))
        assert learner_input["batch"]["episodes"][0]["facts"] == facts
        assert learner_input["batch"]["episodes"][0]["strategy"] == first["strategy"]

    asyncio.run(scenario())


def test_recall_preserves_distinct_conditions_and_exceptions_through_sqlite(tmp_path) -> None:
    async def scenario() -> None:
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        await _view(memory)
        await memory.record_episode(_episode("ep1", run_id="run-1"), 0)
        await memory.record_episode(_episode("ep2", run_id="run-2"), 1)
        one = _lesson("lesson-1", claim="Adjust capacity.", evidence=["ep1", "ep2"])
        one.exceptions = ["A provisioning operation is still pending."]
        two = _lesson("lesson-2", claim=one.claim, evidence=["ep1", "ep2"])
        two.applies_when = ["Load has fallen after a temporary peak."]
        two.exceptions = ["History does not cover the suspected peak."]
        await memory.activate_lesson(one, 2)
        await memory.activate_lesson(two, 3)
        packet = await _recall(memory, await _view(memory))
        assert len(packet.brief.lessons) == 2
        assert all(isinstance(value, RecalledLesson) for value in packet.brief.lessons)
        expected = [
            RecalledLesson(claim=x.claim, applies_when=x.applies_when, exceptions=x.exceptions)
            for x in (one, two)
        ]
        assert all(item in packet.brief.lessons for item in expected)
        context = make_context().model_copy(update={"memory_brief": packet.brief})
        wire = decode_prompt_context(CurrentSGR().build_request(context).user_prompt)
        assert wire["memory_brief"]["lessons"] == packet.brief.model_dump()["lessons"]

    asyncio.run(scenario())


def test_legacy_context_and_episode_defaults_do_not_rewrite_serialized_evidence() -> None:
    raw = make_context().model_dump(mode="json")
    raw["memory_brief"] = MemoryBrief(lessons=["Legacy lesson."]).model_dump(mode="json")
    assert AgentContext.model_validate(raw).model_dump(mode="json") == raw
    old_episode = _episode("old", run_id="run-old").model_dump(mode="json")
    assert "facts" not in old_episode and "strategy" not in old_episode
    assert EpisodeRecord.model_validate(old_episode).model_dump(mode="json") == old_episode
    artifact = make_bootstrap_artifact()
    assert artifact.bundle.decision_brief().facts == artifact.bundle.profile.facts
