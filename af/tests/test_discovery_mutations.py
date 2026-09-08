import asyncio
import json
from copy import deepcopy

import pytest

from tests.test_uptickv2 import CATALOG, World, api, catalog, discovery_outputs, launcher, spec
from uptick_agent.environments.discovered.discovery import _hash, _validate_meaning
from uptick_agent.environments.discovered.models import HttpTool, ToolMeaning, WorldMeaning
from uptick_agent.environments.discovered.schema import compile_tools
from uptick_agent.reasoners.scripted import ScriptedReasoner


@pytest.mark.parametrize(
    "role,value,message",
    [("overview", True, "overview is a read"), ("advance", False, "time advancement")],
)
def test_inconsistent_effect_roles_stop_before_operational_reads(tmp_path, role, value, message):
    async def scenario():
        outputs = discovery_outputs()
        for tool in outputs[1]["tools"]:
            if tool["role"] == role:
                tool["mutates_state"] = value
        world = World()
        env = launcher(tmp_path, world, ScriptedReasoner(outputs))
        try:
            with pytest.raises(ValueError, match=message):
                await env.run(spec())
            assert not any(r.url.path.endswith("/status") for r in world.requests)
            assert not any(
                r.method == "POST" and r.url.path.endswith("/actions") for r in world.requests
            )
            assert not list(tmp_path.glob("registry-*.json"))
            receipt = json.loads((tmp_path / "launches/run-v2-test.json").read_text())
            assert receipt["status"] == "discovery_failed"
        finally:
            await env.aclose()

    asyncio.run(scenario())


def test_inconsistent_cached_effects_are_reclassified(tmp_path):
    async def scenario():
        world = World()
        first = launcher(tmp_path, world)
        try:
            await first.run(spec())
        finally:
            await first.aclose()
        path = next(tmp_path.glob("registry-*.json"))
        data = json.loads(path.read_text())
        for tool in data["meaning"]["tools"]:
            tool["mutates_state"] = True
        path.write_text(json.dumps(data))
        reasoner = ScriptedReasoner([discovery_outputs()[1]])
        second = launcher(tmp_path, world, reasoner)
        try:
            session = await second.run(spec())
            cap = (await session.bootstrap_capabilities()).find("inspectWorld")
            assert cap is not None and not cap.mutates_state
            assert len(reasoner.requests) == 1
        finally:
            await second.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_version", ["discovery-v1", "discovery-v2"])
def test_legacy_meaning_cache_is_not_reused_or_overwritten(tmp_path, legacy_version):
    async def scenario():
        world = World()
        first = launcher(tmp_path, world)
        try:
            await first.run(spec())
        finally:
            await first.aclose()
        current = next(tmp_path.glob("registry-*.json"))
        data = json.loads(current.read_text())
        data["version"] = legacy_version
        for tool in data["meaning"]["tools"]:
            tool["mutates_state"] = True
        key = _hash([legacy_version, data["instructions"], data["openapi"], data["catalog"]])
        legacy = tmp_path / f"registry-{key}.json"
        legacy.write_text(json.dumps(data))
        original = legacy.read_bytes()
        current.unlink()
        reasoner = ScriptedReasoner([discovery_outputs()[1]])
        second = launcher(tmp_path, world, reasoner)
        try:
            await second.run(spec())
            assert len(reasoner.requests) == 1
            assert legacy.read_bytes() == original
            assert current.exists()
        finally:
            await second.aclose()

    asyncio.run(scenario())


def test_http_method_does_not_override_documented_effects():
    tools = compile_tools(api(), catalog(), CATALOG)
    meaning = WorldMeaning.model_validate(discovery_outputs()[1])
    for name, method, mutates in [("unsafeRead", "GET", True), ("query", "POST", False)]:
        tools.append(
            HttpTool(
                name=name,
                description="Documented operation",
                method=method,
                path="/operation",
                schema={},
            )
        )
        meaning.tools.append(
            ToolMeaning(
                name=name, role="observation", mutates_state=mutates, notes="Published effects."
            )
        )
    before = deepcopy(meaning)
    _validate_meaning(tools, meaning)
    assert meaning == before
