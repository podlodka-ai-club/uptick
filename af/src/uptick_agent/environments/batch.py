"""Pure schema, validation and evidence storage for bounded independent calls."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

from jsonschema import Draft202012Validator
from pydantic import Field, StrictBool

from uptick_agent.core.models import (
    AgentConstraints,
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentState,
    JsonObject,
    JsonValue,
    Observation,
    StrictModel,
)
from uptick_agent.core.policy import FORBIDDEN_GENERIC_CAPABILITIES

BATCH_CAPABILITY_NAME = "execute_batch"
COMPOSITE_CAPABILITIES = frozenset({BATCH_CAPABILITY_NAME, "execute_program"})
BATCH_STATE_KEY = "batch_results"
MAX_BATCH_ITEMS = 16
MAX_RETAINED_BATCHES = 4

OBSERVATION_RELEASE_INSTRUCTIONS = (
    " Optional evidence release: release also accepts recent:<capability_name> for a "
    "whole snapshot visible in recent_observations. Release evidence once it is no longer "
    "needed instead of carrying it into later decisions. Keep sources for required IDs, "
    "pending work and remaining verification. New facts in a pending decision do not "
    "replace the stored working facts. Release applies to the snapshot shown before this "
    "decision; a new same-name reply survives. It commits with batch reduction and adds no "
    "remote call. Ordinary capabilities remain usable without a batch."
)
OBSERVATION_RELEASE_DESCRIPTION = (
    "Batch IDs or recent:<capability_name> snapshots whose complete evidence is no longer "
    "needed. [] keeps it. Preserve required IDs and pending verification; newly read "
    "same-name responses survive."
)


class BatchInvocation(StrictModel):
    items: list[CapabilityCall] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)
    release: list[str] = Field(max_length=MAX_RETAINED_BATCHES + 1)
    retain_results: StrictBool


class BatchExecutionError(RuntimeError):
    """A child failed outside the Observation contract; reduction must stop the run."""


def call_violations(
    call: CapabilityCall,
    catalog: CapabilityCatalog,
    constraints: AgentConstraints,
    *,
    retain_results: bool,
) -> list[str]:
    capability = catalog.find(call.name)
    if (
        call.name in COMPOSITE_CAPABILITIES
        or call.name in FORBIDDEN_GENERIC_CAPABILITIES
        or call.name in constraints.forbidden_capabilities
    ):
        return [f"capability {call.name!r} is not permitted in a batch"]
    if capability is None:
        return [f"capability {call.name!r} is unavailable"]
    if capability.terminal:
        return [f"terminal capability {call.name!r} cannot be batched"]
    if not retain_results and capability.mutates_state:
        return ["retain_results=false requires every capability to be read-only"]
    # Unlike the small core validator, this also enforces array bounds and patterns.
    return [
        f"{call.name} arguments at {list(error.absolute_path)!r}: {error.validator} violated"
        for error in Draft202012Validator(capability.input_schema).iter_errors(call.arguments)
    ]


def batch_capability(
    catalog: CapabilityCatalog, *, release_observations: bool = False
) -> Capability | None:
    variants: list[JsonValue] = []
    for capability in catalog.items:
        if (
            capability.name in COMPOSITE_CAPABILITIES
            or capability.name in FORBIDDEN_GENERIC_CAPABILITIES
            or capability.terminal
        ):
            continue
        variants.append(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [capability.name]},
                    "arguments": deepcopy(capability.input_schema),
                },
                "required": ["name", "arguments"],
                "additionalProperties": False,
            }
        )
    if not variants:
        return None
    return Capability(
        name=BATCH_CAPABILITY_NAME,
        description=BATCH_INSTRUCTIONS
        + (OBSERVATION_RELEASE_INSTRUCTIONS if release_observations else ""),
        mutates_state=True,
        input_schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH_ITEMS,
                    "items": {"anyOf": variants},
                },
                "release": {
                    "type": "array",
                    "maxItems": MAX_RETAINED_BATCHES + 1,
                    "items": {"type": "string"},
                    "description": (
                        OBSERVATION_RELEASE_DESCRIPTION
                        if release_observations
                        else "Batch IDs whose evidence is no longer needed; [] keeps it."
                    ),
                },
                "retain_results": {
                    "type": "boolean",
                    "description": (
                        "true holds results until release; false is read-only and replaces "
                        "the single transient batch result, even when retained storage is full."
                    ),
                },
            },
            "required": ["items", "release", "retain_results"],
            "additionalProperties": False,
        },
    )


def item_record(
    index: int,
    call: CapabilityCall,
    *,
    status: str = "not_executed",
    observation: Observation | None = None,
    reason: str | None = None,
) -> JsonObject:
    return {
        "index": index,
        "call": cast(JsonObject, call.model_dump(mode="json")),
        "status": status,
        "observation": (
            cast(JsonObject, observation.model_dump(mode="json")) if observation else None
        ),
        "reason": reason,
    }


def batch_observation(
    batch_id: str | None,
    items: list[JsonObject],
    *,
    code: str | None = None,
    violations: list[str] | None = None,
    replaced_transient: str | None = None,
) -> Observation:
    received = []
    failed = []
    unknown = []
    terminal = False
    for item in items:
        response = item["observation"]
        if isinstance(response, dict):
            received.append(item["index"])
            if not response["ok"]:
                failed.append(item["index"])
            terminal |= bool(response["terminal"])
        if item["status"] == "execution_unknown":
            unknown.append(item["index"])
    skipped = [item["index"] for item in items if item["status"] == "not_executed"]
    ok = bool(items) and not (code or failed or unknown or skipped)
    summary = (
        f"Batch {batch_id or 'rejected'}: responses={received}, errors={failed}, "
        f"unknown={unknown}, not_executed={skipped}. "
        "Responses do not establish completion of asynchronous work or the intended effect."
    )
    if code:
        summary = f"{code}. " + summary
    if replaced_transient:
        summary += f" Replaced transient evidence {replaced_transient}."
    return Observation(
        action_kind=BATCH_CAPABILITY_NAME,
        ok=ok,
        summary=summary,
        terminal=terminal,
        data={
            "batch_id": batch_id,
            "items": cast(JsonValue, deepcopy(items)),
            "code": code,
            "violations": cast(JsonValue, violations or []),
            "replaced_transient": replaced_transient,
        },
    )


class BatchLedger:
    """Bound whole sanitized observations; never infer remote operation lifecycle."""

    def __init__(self) -> None:
        self.retained: dict[str, Observation] = {}
        self.transient: Observation | None = None
        self.sequence = 0

    def preflight(self, invocation: BatchInvocation) -> list[str]:
        known = set(self.retained)
        if self.transient is not None:
            known.add(cast(str, self.transient.data["batch_id"]))
        if len(set(invocation.release)) != len(invocation.release):
            return ["release IDs must be unique"]
        if set(invocation.release) - known:
            return ["release contains an unknown batch ID"]
        if (
            invocation.retain_results
            and len(set(self.retained) - set(invocation.release)) >= MAX_RETAINED_BATCHES
        ):
            return [
                "BATCH_LEDGER_FULL: release unneeded evidence or use read-only transient results"
            ]
        return []

    def next_id(self) -> str:
        self.sequence += 1
        return f"batch-{self.sequence}"

    def replaced_transient(self, invocation: BatchInvocation) -> str | None:
        if invocation.retain_results or self.transient is None:
            return None
        return cast(str, self.transient.data["batch_id"])

    def commit(self, invocation: BatchInvocation, observation: Observation) -> None:
        for identifier in invocation.release:
            self.retained.pop(identifier, None)
            if self.transient is not None and self.transient.data["batch_id"] == identifier:
                self.transient = None
        if invocation.retain_results:
            self.retained[cast(str, observation.data["batch_id"])] = observation.model_copy(
                deep=True
            )
        else:
            self.transient = observation.model_copy(deep=True)

    def project(self, state: EnvironmentState) -> EnvironmentState:
        if BATCH_STATE_KEY in state.decision_view:
            raise ValueError(f"base Environment uses reserved decision_view key {BATCH_STATE_KEY}")
        return state.model_copy(
            update={
                "decision_view": {
                    **state.decision_view,
                    BATCH_STATE_KEY: {
                        "retained": [x.model_dump(mode="json") for x in self.retained.values()],
                        "transient": (
                            self.transient.model_dump(mode="json") if self.transient else None
                        ),
                    },
                }
            },
            deep=True,
        )


BATCH_INSTRUCTIONS = """Execute 1–16 independent current catalog calls with known arguments
as one decision. Requests are sent in order, reducing each response, without waiting for
asynchronous completion between launches. Use only when the environment contract permits it;
arguments must not depend on another item's result. No programs, nested batches, terminal
capabilities, automatic waits, retries or rollback. All items are validated before dispatch
and rechecked before execution. Stop on the first error, unknown outcome or terminal response;
remaining items are not_executed. Account for the accepted prefix before planning more work.
Each item has a zero-based index, original call and full sanitized Observation. A successful
response may only accept work: verify its eventual outcome and the group's combined effect.
batch_results retains four full groups until explicit release. Release evidence only when no
longer needed, including verification of accepted work. Full storage rejects retained groups
before execution. retain_results=false requires read-only catalog calls and uses a separate
transient slot, so verification remains possible with full storage. That slot survives ordinary
calls but the next transient batch replaces it and reports its ID; use retained results for
evidence needed beyond that replacement. IDs are run-local evidence handles, not retry keys.
Evidence records are historical responses, not an automatically reconciled operation ledger.
""".strip()
