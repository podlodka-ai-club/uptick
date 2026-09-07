import pytest
from pydantic import ValidationError

from uptick_agent.memory.config import MemoryConfiguration


def _payload():
    payload = MemoryConfiguration.episodic_only().model_dump(mode="json")
    payload.update(
        schema_version="1.5",
        profile_kind="experiment",
        observed_world_policy="observed-pattern-summary-v1@1.0",
        world_query_settings={
            "scope_paths": ["observation.ok"],
            "action_path": "action.kind",
            "result_path": "result.ok",
        },
    )
    payload["world_model"]["enabled"] = True
    return payload


def test_observed_policy_is_explicit_and_fingerprint_bound():
    payload = _payload()
    enabled = MemoryConfiguration.model_validate(payload)
    payload.pop("observed_world_policy")
    disabled = MemoryConfiguration.model_validate(payload)
    assert enabled.fingerprint != disabled.fingerprint
    assert "observed_world_policy" not in disabled.model_dump(mode="json")
    assert "observed_world_policy" not in MemoryConfiguration().model_dump(mode="json")


@pytest.mark.parametrize("change", ["old_schema", "default", "disabled_world", "missing_query"])
def test_observed_policy_cannot_silently_enable_without_experimental_dependencies(change):
    payload = _payload()
    if change == "old_schema":
        payload["schema_version"] = "1.4"
    elif change == "default":
        payload["profile_kind"] = "default"
    elif change == "disabled_world":
        payload["world_model"]["enabled"] = False
    else:
        payload["world_query_settings"] = None
    with pytest.raises(ValidationError, match="observed_world_policy"):
        MemoryConfiguration.model_validate(payload)
