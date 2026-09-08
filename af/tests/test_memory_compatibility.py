import asyncio
import json

import httpx

from tests.helpers import decode_prompt_context
from tests.test_runner import _catalog, _output
from tests.test_uptickv2 import World, discovery_outputs, launcher, spec
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.contracts import MemoryCompatibilityOwner
from uptick_agent.core.memory_models import ConsolidationQuery, MemoryViewRequest
from uptick_agent.core.models import Observation, RunResult, RunSpec
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.environments.programmatic import ProgrammableEnvironment
from uptick_agent.environments.scripted import ScriptedEnvironment
from uptick_agent.learning import LearningOrchestrator, MemoryConsolidator
from uptick_agent.memory import SQLiteMemory
from uptick_agent.reasoners.scripted import ScriptedReasoner
from uptick_agent.runtime.bootstrap import ScriptedEnvironmentBootstrapper
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import InMemoryRunStore


def test_discovery_memory_scope_ignores_rephrasing_but_changes_with_contract(tmp_path):
    async def scenario():
        scopes = []
        sources = []
        for index in range(3):
            outputs = discovery_outputs()
            outputs[1]["objective"] += f" Description {index}."
            for tool in outputs[1]["tools"]:
                tool["notes"] = f"Different paraphrase {index}."
            world = World()

            def transport(request, world=world, index=index):
                response = world(request)
                if index == 2 and request.url.path == "/contract.yaml":
                    document = response.json()
                    document["info"]["version"] = "incompatible-contract"
                    return httpx.Response(200, json=document)
                return response

            env = launcher(tmp_path / str(index), transport, ScriptedReasoner(outputs))
            try:
                session = ProgrammableEnvironment(await env.run(spec()))
                assert isinstance(session, MemoryCompatibilityOwner)
                scopes.append(session.memory_profile_version())
                sources.append(await session.initialize())
            finally:
                await env.aclose()
        assert sources[0] != sources[1]
        assert scopes[0] == scopes[1]
        assert scopes[0] != scopes[2]

    asyncio.run(scenario())


def test_runs_share_memory_and_learning_without_merging_exact_profiles(tmp_path):
    async def scenario():
        memory = SQLiteMemory(tmp_path / "memory.sqlite")
        manifests = []
        learner = ScriptedReasoner(
            [{"proposal": None, "no_lesson": {"reason": "No new durable principle."}}]
        )
        stores = []
        decisions = []

        class CompatibleEnvironment(ScriptedEnvironment):
            def memory_profile_version(self):
                return "contract-v1-shared"

        for seed in [12, 15, 15]:
            index = len(stores)
            run_id = f"run-{index}"
            store = InMemoryRunStore()
            stores.append(store)
            env = CompatibleEnvironment(
                name="generic",
                catalog=_catalog(),
                bootstrap_text=f"A different generated profile for run {index}.",
                observations=[
                    Observation(action_kind="inspect", summary="capacity healthy"),
                    Observation(action_kind="finish", summary="done", terminal=True),
                ],
                result=RunResult(
                    run_id=run_id, status="completed", steps=0, duration_seconds=0, stop_reason=""
                ),
            )
            reasoner = ScriptedReasoner(
                [_output("inspect"), _output("finish", completed=True, previous=True)]
            )
            decisions.append(reasoner)
            # The first two runs exercise real after-run selection: one group skips,
            # two groups call the learner. The repeated world preserves independence.
            current_learner = (
                learner
                if index < 2
                else ScriptedReasoner(
                    [{"proposal": None, "no_lesson": {"reason": "Still no durable principle."}}]
                )
            )
            runner = AgentRunner(
                agent_core=AgentCore(reasoner=reasoner, sgr=CurrentSGR()),
                environment=ProgrammableEnvironment(env),
                memory=memory,
                run_store=store,
                policy=DecisionPolicy(),
                bootstrapper=ScriptedEnvironmentBootstrapper(),
                learning=LearningOrchestrator(
                    memory=memory,
                    run_store=store,
                    consolidator=MemoryConsolidator(reasoner=current_learner),
                    trigger="after_run",
                    min_evidence_groups=2,
                ),
            )
            await runner.run(RunSpec(run_id=run_id, environment="generic", world_id=str(seed)))
            manifests.append(store.manifests[run_id])

        assert len({m.environment_profile_version for m in manifests}) == 3
        assert {m.memory_profile_version for m in manifests} == {"contract-v1-shared"}
        assert len(learner.requests) == 1
        learning_input = json.loads(learner.requests[0].user_prompt.split("JSON follows:\n", 1)[1])
        assert "run-0" in json.dumps(learning_input) and "run-1" in json.dumps(learning_input)
        view = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="generic", environment_profile_version="contract-v1-shared"
            )
        )
        batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=view,
                environment_id="generic",
                environment_profile_version="contract-v1-shared",
                min_evidence_groups=2,
            )
        )
        assert len(batch.episodes) == 3
        assert len({e.evidence_group_id for e in batch.episodes}) == 2
        assert len({e.record_id for e in batch.episodes}) == 3
        second_context = decode_prompt_context(decisions[1].requests[0].user_prompt)
        assert second_context["memory_brief"]["similar_episodes"]
        assert "contract-v1-shared" not in decisions[1].requests[0].user_prompt
        assert (
            second_context["environment_profile"]["profile_version"]
            == manifests[1].environment_profile_version
        )
        isolated = await memory.resolve_view(
            MemoryViewRequest(
                environment_id="generic", environment_profile_version="contract-v1-changed"
            )
        )
        isolated_batch = await memory.load_consolidation_batch(
            ConsolidationQuery(
                trigger="after_run",
                view=isolated,
                environment_id="generic",
                environment_profile_version="contract-v1-changed",
                min_evidence_groups=2,
            )
        )
        assert isolated_batch.episodes == []

    asyncio.run(scenario())
