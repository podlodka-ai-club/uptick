import asyncio
from copy import deepcopy
from typing import cast

import httpx
import pytest

from tests.helpers import make_context
from tests.test_uptickv2 import World, launcher, spec
from uptick_agent.core.models import CapabilityCall, EnvironmentProfileRef
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.environments.batch import BatchExecutionError
from uptick_agent.environments.discovered.session import DiscoveredSession
from uptick_agent.environments.programmatic import ProgrammableEnvironment, ProgrammableLauncher


def owner(env: ProgrammableEnvironment) -> DiscoveredSession:
    assert isinstance(env._environment, DiscoveredSession)
    return env._environment


def batch(name="inspectWorld", release=()):
    return CapabilityCall(
        name="execute_batch",
        arguments={
            "items": [{"name": name, "arguments": {}}],
            "release": list(release),
            "retain_results": False,
        },
    )


async def start(tmp_path):
    world = World()
    rows = [{"sample": i, "detail": "x" * 80} for i in range(5000)]

    def transport(request):
        response = world(request)
        if request.url.path.endswith("/mail"):
            return httpx.Response(200, json={"rows": rows})
        return response

    wrapper = ProgrammableLauncher(launcher(tmp_path, transport))
    env = await wrapper.run(spec())
    assert isinstance(env, ProgrammableEnvironment)
    state = await env.start(
        spec().model_copy(
            update={
                "environment_profile": EnvironmentProfileRef(
                    environment_id="uptickv2", version="test"
                )
            }
        )
    )
    return wrapper, env, state, world, rows


async def step(env, state, call):
    observation = await env.execute(call, state)
    return env.reduce(state, call, observation), observation


def test_release_reduces_next_prompt_and_does_not_resurrect(tmp_path):
    async def scenario():
        wrapper, env, state, world, rows = await start(tmp_path)
        try:
            state, first = await step(env, state, batch("readMail"))
            delivered = state.model_copy(deep=True)
            assert first.data["items"][0]["observation"]["data"]["rows"] == rows
            old_batch = first.data["batch_id"]
            context = make_context().model_copy(update={"environment_state": state})
            before = CurrentSGR().build_request(context).user_prompt
            call = batch(release=["recent:readMail", old_batch])
            requests = len(world.requests)
            response = await env.execute(call, state)
            # Release is local and commits only with the outer reduction.
            assert len(world.requests) == requests + 1
            assert "readMail" in owner(env)._recent
            state = env.reduce(state, call, response)
            assert "readMail" not in cast(dict, state.decision_view["recent_observations"])
            after = (
                CurrentSGR()
                .build_request(context.model_copy(update={"environment_state": state}))
                .user_prompt
            )
            assert len(before) - len(after) > 400_000
            assert state.latest_observation == response
            # An unrelated ordinary read must not reconstruct the released record.
            state, _ = await step(env, state, CapabilityCall(name="inspectWorld"))
            assert "readMail" not in cast(dict, state.decision_view["recent_observations"])
            assert (
                delivered.latest_observation.data["items"][0]["observation"]["data"]["rows"] == rows
            )
            assert context.agent_working_state == make_context().agent_working_state
        finally:
            await wrapper.aclose()

    asyncio.run(scenario())


def test_new_identical_same_name_response_survives_release(tmp_path):
    async def scenario():
        wrapper, env, state, _, rows = await start(tmp_path)
        try:
            state, _ = await step(env, state, CapabilityCall(name="readMail"))
            state, response = await step(env, state, batch("readMail", ["recent:readMail"]))
            assert (
                cast(dict, state.decision_view["recent_observations"])["readMail"]["observation"][
                    "rows"
                ]
                == rows
            )
            assert response.data["items"][0]["observation"]["data"]["rows"] == rows
        finally:
            await wrapper.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "release",
    [
        ["recent:readMail", "recent:missing"],
        ["recent:readMail"] * 2,
        ["recent:readMail", "batch-missing"],
    ],
)
def test_invalid_release_rejects_all_before_dispatch(tmp_path, release):
    async def scenario():
        wrapper, env, state, world, _ = await start(tmp_path)
        try:
            state, _ = await step(env, state, CapabilityCall(name="readMail"))
            before = deepcopy(owner(env)._recent)
            requests = len(world.requests)
            state, response = await step(env, state, batch(release=release))
            assert not response.ok
            assert response.data["code"] == "INVALID_BATCH"
            assert len(world.requests) == requests
            assert owner(env)._recent == before
        finally:
            await wrapper.aclose()

    asyncio.run(scenario())


def test_failed_reduction_keeps_both_evidence_owners(tmp_path, monkeypatch):
    async def scenario():
        wrapper, env, state, _, _ = await start(tmp_path)
        try:
            state, first = await step(env, state, batch("readMail"))
            recent_before = deepcopy(owner(env)._recent)
            assert env._batches.transient is not None
            ledger_before = env._batches.transient.model_copy(deep=True)

            def fail(*args):
                raise ValueError("failed reduction")

            monkeypatch.setattr(owner(env), "reduce", fail)
            call = batch(release=["recent:readMail", first.data["batch_id"]])
            response = await env.execute(call, state)
            with pytest.raises(BatchExecutionError):
                env.reduce(state, call, response)
            assert owner(env)._recent == recent_before
            assert env._batches.transient == ledger_before
        finally:
            await wrapper.aclose()

    asyncio.run(scenario())
