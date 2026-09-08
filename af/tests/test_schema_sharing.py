import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from tests.helpers import make_context
from tests.test_models import _decision
from tests.test_runner import _output
from uptick_agent.core.models import Capability, CapabilityCall, CapabilityCatalog, JsonObject
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.prompt_serialization import (
    deserialize_context_prompt,
    serialize_context_prompt,
)
from uptick_agent.core.schema_sharing import share_call_schemas
from uptick_agent.core.sgr import CurrentSGR, normalized_output_schema
from uptick_agent.environments.batch import batch_capability, call_violations


def _context():
    native = Capability(
        name="resize",
        description="Resize a generic resource.",
        mutates_state=True,
        input_schema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Documented sizing guidance. " * 20,
                },
                "tier": {"type": "string", "enum": ["small", "large"]},
            },
            "required": ["count", "tier"],
            "additionalProperties": False,
        },
    )
    catalog = CapabilityCatalog(items=[native])
    batch = batch_capability(catalog)
    assert batch is not None
    return make_context(capabilities=CapabilityCatalog(items=[native, batch]))


def test_user_schema_sharing_is_lossless_and_never_uses_observation_as_contract_source() -> None:
    context = _context()
    context.environment_state.latest_observation.data = deepcopy(
        context.capabilities.items[0].input_schema
    )
    original = context.model_dump(mode="json")
    encoded = serialize_context_prompt(context)
    wire = json.loads(encoded)
    refs = wire["_context_references"]
    assert any(ref["path"][0] == "capabilities" for ref in refs)
    assert all(ref["source"][0] == ref["path"][0] for ref in refs)
    assert (
        wire["environment_state"]["latest_observation"]
        == original["environment_state"]["latest_observation"]
    )
    assert deserialize_context_prompt(encoded) == original
    assert context.model_dump(mode="json") == original
    assert len(encoded) < len(json.dumps(original, ensure_ascii=False, separators=(",", ":")))
    forged = deepcopy(wire)
    forged["_context_references"][0]["source"] = ["environment_state", "latest_observation", "data"]
    with pytest.raises(ValueError, match="cross context sections"):
        deserialize_context_prompt(json.dumps(forged))


def test_output_schema_shares_native_and_grouped_call_and_keeps_argument_validation() -> None:
    context = _context()
    original = context.model_dump(mode="json")
    request = CurrentSGR().build_request(context)
    schema = normalized_output_schema(request.output_schema)
    assert "$defs" in schema
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for arguments, valid in [
        ({"count": 2, "tier": "small"}, True),
        ({"count": 0, "tier": "small"}, False),
        ({"count": 21, "tier": "small"}, False),
        ({"count": "2", "tier": "small"}, False),
        ({"count": 2, "tier": "unknown"}, False),
        ({"count": 2}, False),
        ({"count": 2, "tier": "small", "extra": True}, False),
    ]:
        call = {"name": "resize", "arguments": arguments}
        for selected in [
            call,
            {
                "name": "execute_batch",
                "arguments": {
                    "items": [call],
                    "retain_results": True,
                    "release": [],
                },
            },
        ]:
            envelope = _output("resize")
            envelope["selected_action"] = selected
            assert validator.is_valid(envelope) == valid
            decision = _decision(selected["name"], selected["arguments"], completed=False)
            assert DecisionPolicy().validate(context, decision).accepted == valid
        assert (
            not call_violations(
                CapabilityCall.model_validate(call),
                context.capabilities,
                context.constraints,
                retain_results=True,
            )
        ) == valid
    assert context.model_dump(mode="json") == original
    assert normalized_output_schema(schema) == schema


def test_sharing_does_not_reinterpret_literal_values_or_existing_reference_scopes() -> None:
    call = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": ["inspect"]},
            "arguments": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        "required": ["name", "arguments"],
        "additionalProperties": False,
        "description": "Long call description. " * 30,
    }
    schema = {
        "type": "object",
        "properties": {
            "one": deepcopy(call),
            "two": deepcopy(call),
            "literal": {"enum": [deepcopy(call), {"$ref": "literal-not-a-reference"}]},
        },
    }
    before = deepcopy(schema)
    shared = share_call_schemas(schema, [call])
    assert "$defs" in shared
    properties = shared["properties"]
    assert isinstance(properties, dict)
    assert properties["literal"] == schema["properties"]["literal"]
    assert schema == before
    scoped = deepcopy(schema)
    scoped["properties"]["one"]["$id"] = "https://example.test/schema"
    assert share_call_schemas(scoped, [call]) is scoped
    assert share_call_schemas(shared, [call]) is shared


def test_v1_state_references_remain_readable() -> None:
    context = make_context()
    value: JsonObject = {"large": "x" * 2000}
    context.environment_state.latest_observation.data = value
    context.environment_state.decision_view = {"old": deepcopy(value)}
    wire = json.loads(serialize_context_prompt(context))
    wire["_context_format"] = "shared-values-v1"
    assert deserialize_context_prompt(json.dumps(wire)) == context.model_dump(mode="json")
