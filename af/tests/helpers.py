from __future__ import annotations

from typing import Any, cast

from uptick_agent.core.bootstrap_models import (
    BootstrapValidationResult,
    EnvironmentBootstrapArtifact,
    EnvironmentBootstrapBundle,
    EnvironmentFact,
    EnvironmentProfile,
    ToolMetadata,
    ToolRegistry,
)
from uptick_agent.core.models import (
    AgentConstraints,
    AgentContext,
    AgentWorkingState,
    Capability,
    CapabilityCatalog,
    ContextProgress,
    EnvironmentBrief,
    EnvironmentProfileRef,
    EnvironmentState,
    MemoryBrief,
    Observation,
)
from uptick_agent.core.prompt_serialization import deserialize_context_prompt
from uptick_agent.runtime.bootstrap import (
    BOOTSTRAP_SCHEMA_VERSION,
    BOOTSTRAP_VALIDATOR_VERSION,
    bootstrap_identity,
    environment_profile_version,
)


def inspect_catalog(*, terminal_finish: bool = False) -> CapabilityCatalog:
    items = [
        Capability(
            name="inspect",
            description="inspect",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
    ]
    if terminal_finish:
        items.append(
            Capability(
                name="finish",
                description="finish",
                input_schema={
                    "type": "object",
                    "properties": {"reason": {"type": "string", "minLength": 1}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
                terminal=True,
            )
        )
    return CapabilityCatalog(items=items)


def make_context(
    *,
    environment: str = "fake",
    objective: str = "test",
    step: int = 1,
    step_limit: int | None = 3,
    terminal: bool = False,
    capabilities: CapabilityCatalog | None = None,
) -> AgentContext:
    observation = Observation(
        action_kind="start",
        summary=f"{environment} ready",
        terminal=terminal,
    )
    return AgentContext(
        objective=objective,
        environment_profile=EnvironmentBrief(
            environment_id=environment,
            profile_version="profile-1",
            guidance=["Inspect before changing state."],
        ),
        environment_state=EnvironmentState(
            profile=EnvironmentProfileRef(
                environment_id=environment,
                version="profile-1",
            ),
            status="terminal" if terminal else "active",
            decision_view={"environment": environment},
            latest_observation=observation,
        ),
        agent_working_state=AgentWorkingState(),
        memory_brief=MemoryBrief(),
        capabilities=capabilities or inspect_catalog(),
        constraints=AgentConstraints(),
        progress=(
            ContextProgress(step=step, step_limit=step_limit) if step_limit is not None else None
        ),
    )


def make_bootstrap_artifact(
    *,
    environment_id: str = "uptick",
    capabilities: CapabilityCatalog | None = None,
) -> EnvironmentBootstrapArtifact:
    catalog = capabilities or inspect_catalog(terminal_finish=True)
    source = f"Environment {environment_id} exposes only its declared capabilities."
    identity = bootstrap_identity(
        source=source,
        environment_id=environment_id,
        capabilities=catalog,
        bootstrap_prompt_version="scripted-bootstrap-v1",
        bootstrap_schema_version=BOOTSTRAP_SCHEMA_VERSION,
        validator_version=BOOTSTRAP_VALIDATOR_VERSION,
    )
    fact = EnvironmentFact(
        category="instructions",
        statement=source,
    )
    registry = ToolRegistry(
        items=[
            ToolMetadata(capability_name=item.name, purpose=item.description)
            for item in catalog.items
        ]
    )
    guidance = ["Use only declared capabilities."]
    profile_version = environment_profile_version(
        environment_id=environment_id,
        facts=[fact],
        guidance=guidance,
        registry=registry,
        capability_catalog_sha256=identity.capability_catalog_sha256,
    )
    bundle = EnvironmentBootstrapBundle(
        profile=EnvironmentProfile(
            environment_id=environment_id,
            profile_version=profile_version,
            facts=[fact],
        ),
        brief=EnvironmentBrief(
            environment_id=environment_id,
            profile_version=profile_version,
            guidance=guidance,
        ),
        registry=registry,
        capability_catalog_sha256=identity.capability_catalog_sha256,
    )
    return EnvironmentBootstrapArtifact(
        identity=identity,
        source_text=source,
        bundle=bundle,
        validation=BootstrapValidationResult(
            accepted=True,
            validator_version=BOOTSTRAP_VALIDATOR_VERSION,
        ),
    )


def decode_prompt_context(prompt: str) -> dict[str, Any]:
    return cast(dict[str, Any], deserialize_context_prompt(prompt.split("\n", 1)[1]))
