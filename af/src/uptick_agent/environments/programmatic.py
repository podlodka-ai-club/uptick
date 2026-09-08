from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from jsonschema import Draft202012Validator
from pydantic import Field, StrictFloat, StrictInt, ValidationError, model_validator

from uptick_agent.core.contracts import (
    Environment,
    EnvironmentLauncher,
    MemoryCompatibilityOwner,
    ObservationReleaseOwner,
)
from uptick_agent.core.models import (
    AgentConstraints,
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentState,
    EnvironmentTelemetry,
    JsonObject,
    JsonScalar,
    JsonValue,
    Observation,
    RunCompletion,
    RunResult,
    RunSpec,
    StrictModel,
)
from uptick_agent.core.policy import FORBIDDEN_GENERIC_CAPABILITIES
from uptick_agent.environments.batch import (
    BATCH_CAPABILITY_NAME,
    BATCH_INSTRUCTIONS,
    BATCH_STATE_KEY,
    COMPOSITE_CAPABILITIES,
    BatchExecutionError,
    BatchInvocation,
    BatchLedger,
    batch_capability,
    batch_observation,
    call_violations,
    item_record,
)

PROGRAM_CAPABILITY_NAME = "execute_program"
_MAX_PROGRAMS = 8
_MAX_COMPACT_OUTPUT_BYTES = 32_768


class ProgramArgument(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    value: JsonValue


class _ProgramConditionPath(StrictModel):
    path: list[str] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Path from the primary Observation root. Tool response fields are under data: "
            '["data", "clock", "simulation_time"], not ["clock", "simulation_time"]. '
            'Observation status flags use ["ok"] and ["terminal"]. '
            "Use field names from the actual tool response."
        ),
    )


class ProgramExistsCondition(_ProgramConditionPath):
    operator: Literal["exists"]
    value: bool = Field(strict=True, description="true requires presence; false requires absence.")


class ProgramEqualityCondition(_ProgramConditionPath):
    operator: Literal["eq", "ne"]
    value: JsonScalar


class ProgramNumericCondition(_ProgramConditionPath):
    operator: Literal["gt", "gte", "lt", "lte"]
    value: StrictInt | StrictFloat


type ProgramCondition = ProgramExistsCondition | ProgramEqualityCondition | ProgramNumericCondition


class ProgramDefinition(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=300)
    primary_capability: str = Field(min_length=1, max_length=128)
    followup_capability: str | None = Field(default=None, min_length=1, max_length=128)
    condition: ProgramCondition | None = Field(
        description=(
            "Optional predicate on the primary Observation. null runs the follow-up after "
            "a successful, non-terminal primary call without an additional condition."
        )
    )
    primary_output_paths: list[list[str]] = Field(
        default_factory=list,
        max_length=12,
        description='Paths from the primary Observation root, e.g. ["data", "clock"].',
    )
    followup_output_paths: list[list[str]] = Field(
        default_factory=list,
        max_length=12,
        description='Paths from the follow-up Observation root, e.g. ["data", "status"].',
    )

    @model_validator(mode="after")
    def validate_structure(self) -> ProgramDefinition:
        if self.followup_capability is None:
            if self.condition is not None:
                raise ValueError("a condition requires a follow-up capability")
            if self.followup_output_paths:
                raise ValueError("follow-up output paths require a follow-up capability")
        _validate_output_paths(self.primary_output_paths, "primary")
        _validate_output_paths(self.followup_output_paths, "follow-up")
        return self


class ProgramInvocation(StrictModel):
    mode: Literal["define_and_execute", "execute"]
    definition: ProgramDefinition | None
    program_id: str | None = Field(default=None, min_length=1, max_length=80)
    primary_arguments: list[ProgramArgument] = Field(default_factory=list, max_length=16)
    followup_arguments: list[ProgramArgument] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_mode_and_arguments(self) -> ProgramInvocation:
        if self.mode == "define_and_execute":
            if self.definition is None or self.program_id is not None:
                raise ValueError("define_and_execute requires definition and null program_id")
        elif self.definition is not None or self.program_id is None:
            raise ValueError("execute requires program_id and null definition")
        for label, arguments in (
            ("primary", self.primary_arguments),
            ("follow-up", self.followup_arguments),
        ):
            names = [item.name for item in arguments]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} argument names must be unique")
        return self


@dataclass(frozen=True, slots=True)
class _RegisteredProgram:
    program_id: str
    definition: ProgramDefinition


@dataclass(slots=True)
class _PendingReduction:
    call: CapabilityCall
    final_state: EnvironmentState
    compact_observation: Observation
    batch: BatchInvocation | None = None
    failure: Exception | None = None
    release_observations: Callable[[EnvironmentState], EnvironmentState] | None = None


class ProgrammableLauncher:
    """Wrap the session after a run-first environment has discovered its tools."""

    def __init__(self, launcher: EnvironmentLauncher) -> None:
        self.launcher = launcher
        self.session: ProgrammableEnvironment | None = None

    async def run(self, spec: RunSpec) -> Environment:
        if self.session is not None:
            raise RuntimeError("launcher already owns a run")
        self.session = ProgrammableEnvironment(await self.launcher.run(spec))
        return self.session

    async def aclose(self) -> None:
        if self.session is not None:
            await self.session.aclose()
        else:
            close = getattr(self.launcher, "aclose", None)
            if close is not None:
                await close()


class ProgrammableEnvironment:
    """Compose bounded programs and independent batches over Environment capabilities."""

    def __init__(self, environment: Environment) -> None:
        self._environment = environment
        self._programs: dict[str, _RegisteredProgram] = {}
        self._versions: dict[str, int] = {}
        self._constraints = AgentConstraints()
        self._active_run_id: str | None = None
        self._completed_run_id: str | None = None
        self._program_executions = 0
        self._program_subcalls = 0
        self._completed_counts: tuple[int, int] = (0, 0)
        self._pending: _PendingReduction | None = None
        self._batches = BatchLedger()
        self._broken = False

    def memory_profile_version(self) -> str | None:
        if isinstance(self._environment, MemoryCompatibilityOwner):
            return self._environment.memory_profile_version()
        return None

    async def initialize(self) -> str:
        source = await self._environment.initialize()
        return source.rstrip() + "\n\n" + _PROGRAM_INSTRUCTIONS + "\n\n" + BATCH_INSTRUCTIONS

    async def bootstrap_capabilities(self) -> CapabilityCatalog:
        base = await self._environment.bootstrap_capabilities()
        return self._catalog_with_program(base, include_registered=False)

    async def start(self, spec: RunSpec) -> EnvironmentState:
        if self._active_run_id is not None:
            raise RuntimeError("ProgrammableEnvironment already owns an active run")
        state = await self._environment.start(spec)
        self._active_run_id = spec.run_id
        self._completed_run_id = None
        self._programs.clear()
        self._versions.clear()
        self._constraints = spec.constraints.model_copy(deep=True)
        self._program_executions = 0
        self._program_subcalls = 0
        self._completed_counts = (0, 0)
        self._pending = None
        self._batches = BatchLedger()
        self._broken = False
        return self._batches.project(state)

    async def capabilities(self, state: EnvironmentState) -> CapabilityCatalog:
        base = await self._environment.capabilities(_base_state(state))
        return self._catalog_with_program(base, include_registered=True)

    async def execute(self, call: CapabilityCall, state: EnvironmentState) -> Observation:
        if self._broken:
            raise RuntimeError("composite execution failed; the session cannot continue")
        if self._pending is not None:
            raise RuntimeError("the previous composite execution has not been reduced")
        if call.name == BATCH_CAPABILITY_NAME:
            return await self._execute_batch(call, _base_state(state))
        if call.name != PROGRAM_CAPABILITY_NAME:
            return await self._environment.execute(call, _base_state(state))
        try:
            invocation = ProgramInvocation.model_validate(call.arguments)
        except Exception as error:
            return _program_error("INVALID_PROGRAM", str(error))

        if invocation.mode == "define_and_execute":
            assert invocation.definition is not None
            violations = await self._validate_definition(invocation.definition, state)
            if violations:
                return _program_error("INVALID_PROGRAM", "; ".join(violations))
            try:
                registered, created = self._register(invocation.definition)
            except ValueError as error:
                return _program_error("PROGRAM_REGISTRY_FULL", str(error))
        else:
            assert invocation.program_id is not None
            registered = self._programs.get(invocation.program_id)
            if registered is None:
                return _program_error(
                    "UNKNOWN_PROGRAM",
                    f"unknown run-scoped program {invocation.program_id!r}",
                )
            created = False

        if registered.definition.followup_capability is None and invocation.followup_arguments:
            return _program_error(
                "INVALID_PROGRAM_INPUT",
                "follow-up arguments were supplied to a program without a follow-up",
            )
        return await self._execute_registered(
            registered,
            created=created,
            invocation=invocation,
            outer_call=call,
            state=_base_state(state),
        )

    async def _execute_batch(self, call: CapabilityCall, state: EnvironmentState) -> Observation:
        try:
            invocation = BatchInvocation.model_validate(call.arguments)
        except ValidationError:
            return batch_observation(None, [], code="INVALID_BATCH")
        items = [item_record(i, child) for i, child in enumerate(invocation.items)]
        recent_names = [
            h.removeprefix("recent:") for h in invocation.release if h.startswith("recent:")
        ]
        ledger_invocation = invocation.model_copy(
            update={"release": [h for h in invocation.release if not h.startswith("recent:")]}
        )
        violations = self._batches.preflight(ledger_invocation)
        release_observations = None
        if recent_names:
            if isinstance(self._environment, ObservationReleaseOwner):
                try:
                    release_observations = self._environment.prepare_observation_release(
                        recent_names
                    )
                except ValueError as error:
                    violations.append(str(error))
            else:
                violations.append("the environment does not support recent snapshot release")
        if self._active_run_id is None or state.status != "active":
            violations.append("batch requires an active session")
        if call.name in self._constraints.forbidden_capabilities:
            violations.append("execute_batch is forbidden by run constraints")
        catalog = await self._environment.capabilities(state)
        for i, child in enumerate(invocation.items):
            violations.extend(
                f"items[{i}]: {v}"
                for v in call_violations(
                    child, catalog, self._constraints, retain_results=invocation.retain_results
                )
            )
        if violations:
            code = (
                "BATCH_LEDGER_FULL"
                if any(v.startswith("BATCH_LEDGER_FULL") for v in violations)
                else "INVALID_BATCH"
            )
            return batch_observation(None, items, code=code, violations=violations)

        batch_id = self._batches.next_id()
        failure = None
        code = None
        for i, child in enumerate(invocation.items):
            response = None
            stage = "capabilities"
            try:
                catalog = await self._environment.capabilities(state)
                violations = call_violations(
                    child, catalog, self._constraints, retain_results=invocation.retain_results
                )
                if violations:
                    items[i] = item_record(i, child, reason="; ".join(violations))
                    code = "BATCH_ITEM_UNAVAILABLE"
                    break
                stage = "execute"
                response = await self._environment.execute(child, state)
                items[i] = item_record(i, child, status="response_received", observation=response)
                stage = "reduce"
                state = self._environment.reduce(state, child, response)
                if BATCH_STATE_KEY in state.decision_view:
                    raise ValueError("base Environment returned reserved batch state")
                if not response.ok or response.terminal:
                    break
            except Exception as error:
                # Defer the infrastructure failure to outer reduce so the runner has
                # the full received prefix available for its normal failure trace.
                failure = error
                code = "BATCH_INFRASTRUCTURE_FAILURE"
                if response is None:
                    items[i] = item_record(
                        i,
                        child,
                        status="execution_unknown" if stage == "execute" else "not_executed",
                        reason=f"{stage}: {type(error).__name__}",
                    )
                break
        for item in items:
            if item["status"] == "not_executed" and item["reason"] is None:
                item["reason"] = "stopped_before_dispatch"
        observation = batch_observation(
            batch_id,
            items,
            code=code,
            replaced_transient=self._batches.replaced_transient(invocation),
        )
        self._pending = _PendingReduction(
            call=call.model_copy(deep=True),
            final_state=state,
            compact_observation=observation.model_copy(deep=True),
            batch=ledger_invocation,
            failure=failure,
            release_observations=release_observations,
        )
        return observation

    def reduce(
        self,
        state: EnvironmentState,
        call: CapabilityCall,
        observation: Observation,
    ) -> EnvironmentState:
        if call.name not in COMPOSITE_CAPABILITIES:
            return self._batches.project(
                self._environment.reduce(_base_state(state), call, observation)
            )
        pending = self._pending
        if pending is None:
            return self._batches.project(
                _base_state(state).model_copy(
                    update={
                        "status": "terminal" if observation.terminal else state.status,
                        "latest_observation": observation,
                    },
                    deep=True,
                )
            )
        if pending.call != call:
            raise RuntimeError("composite reduction does not match the executed call")
        self._pending = None
        if pending.failure is not None:
            self._broken = True
            raise BatchExecutionError(
                f"Batch stopped after {type(pending.failure).__name__}; state is unreliable"
            ) from pending.failure
        if pending.release_observations is not None:
            pending.final_state = pending.release_observations(pending.final_state)
        if pending.batch is not None:
            self._batches.commit(pending.batch, pending.compact_observation)
        return self._batches.project(
            pending.final_state.model_copy(
                update={
                    "status": (
                        "terminal"
                        if pending.compact_observation.terminal
                        else pending.final_state.status
                    ),
                    "latest_observation": pending.compact_observation,
                },
                deep=True,
            )
        )

    async def result(
        self,
        state: EnvironmentState,
        completion: RunCompletion,
    ) -> RunResult | None:
        result = await self._environment.result(_base_state(state), completion)
        self._completed_run_id = self._active_run_id
        self._completed_counts = (self._program_executions, self._program_subcalls)
        self._active_run_id = None
        self._programs.clear()
        self._versions.clear()
        self._pending = None
        self._batches = BatchLedger()
        return result

    def telemetry(self, run_id: str) -> EnvironmentTelemetry:
        base = self._environment.telemetry(run_id)
        if run_id == self._active_run_id:
            executions, subcalls = self._program_executions, self._program_subcalls
        elif run_id == self._completed_run_id:
            executions, subcalls = self._completed_counts
        else:
            executions, subcalls = 0, 0
        return base.model_copy(
            update={
                "program_executions": executions,
                "program_subcalls": subcalls,
            }
        )

    async def aclose(self) -> None:
        close = getattr(self._environment, "aclose", None)
        try:
            if close is not None:
                await close()
        finally:
            self._active_run_id = None
            self._programs.clear()
            self._versions.clear()
            self._pending = None
            self._batches = BatchLedger()

    async def _validate_definition(
        self,
        definition: ProgramDefinition,
        state: EnvironmentState,
    ) -> list[str]:
        catalog = await self._environment.capabilities(_base_state(state))
        violations = _definition_capability_violations(
            definition.primary_capability,
            catalog,
            constraints=self._constraints,
            role="primary",
            allow_mutating=True,
        )
        if definition.followup_capability is not None:
            violations.extend(
                _definition_capability_violations(
                    definition.followup_capability,
                    catalog,
                    constraints=self._constraints,
                    role="follow-up",
                    allow_mutating=False,
                )
            )
        return violations

    def _register(self, definition: ProgramDefinition) -> tuple[_RegisteredProgram, bool]:
        for registered in self._programs.values():
            if registered.definition == definition:
                return registered, False
        if len(self._programs) >= _MAX_PROGRAMS:
            raise ValueError(f"a run may register at most {_MAX_PROGRAMS} programs")
        version = self._versions.get(definition.name, 0) + 1
        self._versions[definition.name] = version
        program_id = f"{definition.name}@{version}"
        registered = _RegisteredProgram(program_id=program_id, definition=definition)
        self._programs[program_id] = registered
        return registered, True

    async def _execute_registered(
        self,
        registered: _RegisteredProgram,
        *,
        created: bool,
        invocation: ProgramInvocation,
        outer_call: CapabilityCall,
        state: EnvironmentState,
    ) -> Observation:
        definition = registered.definition
        primary_call = _capability_call(
            definition.primary_capability,
            invocation.primary_arguments,
        )
        current_state, primary_observation, primary_called = await self._execute_nested(
            primary_call,
            state,
            allow_mutating=True,
        )
        subcalls = [_subcall("primary", primary_call, primary_observation)]
        results = [_compact_result("primary", definition.primary_output_paths, primary_observation)]
        executed_calls = int(primary_called)
        skipped_followup = False
        followup_observation: Observation | None = None

        should_follow_up = (
            definition.followup_capability is not None
            and primary_observation.ok
            and not primary_observation.terminal
            and (
                definition.condition is None
                or _condition_matches(definition.condition, primary_observation)
            )
        )
        if should_follow_up:
            assert definition.followup_capability is not None
            followup_call = _capability_call(
                definition.followup_capability,
                invocation.followup_arguments,
            )
            current_state, followup_observation, followup_called = await self._execute_nested(
                followup_call,
                current_state,
                allow_mutating=False,
            )
            subcalls.append(_subcall("followup", followup_call, followup_observation))
            results.append(
                _compact_result(
                    "followup",
                    definition.followup_output_paths,
                    followup_observation,
                )
            )
            executed_calls += int(followup_called)
        elif definition.followup_capability is not None:
            skipped_followup = True

        self._program_executions += 1
        self._program_subcalls += executed_calls
        all_ok = primary_observation.ok and (
            followup_observation is None or followup_observation.ok
        )
        terminal = primary_observation.terminal or bool(
            followup_observation and followup_observation.terminal
        )
        compact_data: JsonObject = {
            "program_id": registered.program_id,
            "created": created,
            "executed_calls": executed_calls,
            "skipped_followup": skipped_followup,
            "results": cast(JsonValue, results),
        }
        compact_data, output_ok = _bound_compact_data(compact_data)
        all_ok = all_ok and output_ok
        summary = (
            f"Program {registered.program_id} executed {executed_calls} capability call(s); "
            f"status={'ok' if all_ok else 'error'}; followup_skipped={skipped_followup}."
        )
        if not output_ok:
            summary += " Selected output exceeded the context limit; request fewer paths."
        compact = Observation(
            action_kind=PROGRAM_CAPABILITY_NAME,
            ok=all_ok,
            summary=summary,
            data=compact_data,
            terminal=terminal,
        )
        trace_data = compact_data | {"subcalls": cast(JsonValue, subcalls)}
        trace = compact.model_copy(update={"data": trace_data}, deep=True)
        self._pending = _PendingReduction(
            call=outer_call.model_copy(deep=True),
            final_state=current_state,
            compact_observation=compact,
        )
        return trace

    async def _execute_nested(
        self,
        call: CapabilityCall,
        state: EnvironmentState,
        *,
        allow_mutating: bool,
    ) -> tuple[EnvironmentState, Observation, bool]:
        violation = await self._nested_call_violation(
            call,
            state,
            allow_mutating=allow_mutating,
        )
        if violation is not None:
            return state, _nested_error(call.name, violation), False
        observation = await self._environment.execute(call, state)
        return self._environment.reduce(state, call, observation), observation, True

    async def _nested_call_violation(
        self,
        call: CapabilityCall,
        state: EnvironmentState,
        *,
        allow_mutating: bool,
    ) -> str | None:
        catalog = await self._environment.capabilities(state)
        capability = catalog.find(call.name)
        if capability is None:
            return f"capability {call.name!r} is no longer available"
        violations = _capability_violations(
            capability,
            constraints=self._constraints,
            role="nested",
            allow_mutating=allow_mutating,
        )
        violations.extend(
            f"arguments at {list(error.absolute_path)!r}: {error.validator} violated"
            for error in Draft202012Validator(capability.input_schema).iter_errors(call.arguments)
        )
        return "; ".join(violations) if violations else None

    def _catalog_with_program(
        self,
        base: CapabilityCatalog,
        *,
        include_registered: bool,
    ) -> CapabilityCatalog:
        if any(base.find(name) is not None for name in COMPOSITE_CAPABILITIES):
            raise ValueError("base Environment already declares a composite capability")
        description = _PROGRAM_DESCRIPTION
        if include_registered and self._programs:
            descriptors = [
                {
                    "program_id": item.program_id,
                    "description": item.definition.description,
                    "primary": item.definition.primary_capability,
                    "followup": item.definition.followup_capability,
                }
                for item in self._programs.values()
            ]
            description += " Registered run-scoped programs: " + json.dumps(
                descriptors,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        items = [*base.items, _program_capability(description, base)]
        batch = batch_capability(
            base, release_observations=isinstance(self._environment, ObservationReleaseOwner)
        )
        if batch is not None:
            items.append(batch)
        return CapabilityCatalog(items=items)


def _base_state(state: EnvironmentState) -> EnvironmentState:
    return state.model_copy(
        update={
            "decision_view": {
                key: value for key, value in state.decision_view.items() if key != BATCH_STATE_KEY
            }
        },
        deep=True,
    )


def _program_capability(description: str, catalog: CapabilityCatalog) -> Capability:
    return Capability(
        name=PROGRAM_CAPABILITY_NAME,
        description=description,
        input_schema=_inline_program_schema(catalog),
        mutates_state=True,
    )


def _inline_program_schema(catalog: CapabilityCatalog) -> JsonObject:
    schema = ProgramInvocation.model_json_schema()
    definitions = schema.get("$defs", {})
    # JsonValue is recursive at runtime. For generation, reuse the finite shapes
    # the Environment already publishes instead of an unrestricted JSON-object schema.
    scalar = definitions["JsonScalar"]
    values = [scalar, {"type": "array", "items": scalar}]
    seen: set[str] = set()
    for capability in catalog.items:
        for value in _argument_value_schemas(capability.input_schema):
            if not _may_accept_container(value):
                continue
            identity = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if identity not in seen:
                seen.add(identity)
                values.append(deepcopy(value))
    value_schema = {"anyOf": values}

    def inline(value: object) -> object:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if reference == "#/$defs/JsonValue":
            # Native schemas are already compiled. Do not reinterpret their literal
            # enum/const data as references into the program model's definitions.
            return deepcopy(value_schema)
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = definitions.get(reference.removeprefix("#/$defs/"))
            if not isinstance(target, dict):
                raise ValueError(f"unknown program schema reference {reference!r}")
            return inline(target)
        result = {
            key: inline(child)
            for key, child in value.items()
            if key not in {"$defs", "default", "title"}
        }
        # DecisionPolicy and structured-output providers both understand enum.
        if "const" in result:
            result["enum"] = [result.pop("const")]
        return result

    result = inline(schema)
    if not isinstance(result, dict):
        raise TypeError("program capability schema must be an object")
    return cast(JsonObject, result)


def _argument_value_schemas(schema: JsonObject) -> Iterator[JsonObject]:
    properties = schema.get("properties", {})
    if isinstance(properties, dict):
        yield from (value for value in properties.values() if isinstance(value, dict))
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    yield from _argument_value_schemas(branch)


def _may_accept_container(schema: JsonObject) -> bool:
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind in {"object", "array"}
    if isinstance(kind, list):
        return "object" in kind or "array" in kind
    branches = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(branches, list):
        return any(
            isinstance(branch, dict) and _may_accept_container(branch) for branch in branches
        )
    return True


def _definition_capability_violations(
    name: str,
    catalog: CapabilityCatalog,
    *,
    constraints: AgentConstraints,
    role: str,
    allow_mutating: bool,
) -> list[str]:
    capability = catalog.find(name)
    if capability is None:
        return [f"{role} uses unknown capability {name!r}"]
    return _capability_violations(
        capability,
        constraints=constraints,
        role=role,
        allow_mutating=allow_mutating,
    )


def _capability_violations(
    capability: Capability,
    *,
    constraints: AgentConstraints,
    role: str,
    allow_mutating: bool,
) -> list[str]:
    name = capability.name
    violations: list[str] = []
    if name in COMPOSITE_CAPABILITIES:
        violations.append(f"{role} cannot recursively execute a program or batch")
    if name in FORBIDDEN_GENERIC_CAPABILITIES:
        violations.append(f"{role} uses forbidden capability {name!r}")
    if name in constraints.forbidden_capabilities:
        violations.append(f"{role} uses run-forbidden capability {name!r}")
    if capability.terminal:
        violations.append(f"{role} cannot use terminal capability {name!r}")
    if capability.mutates_state and not allow_mutating:
        violations.append(f"{role} must be read-only; {name!r} mutates state")
    return violations


def _validate_output_paths(paths: list[list[str]], label: str) -> None:
    normalized = [tuple(path) for path in paths]
    if any(not path or len(path) > 8 for path in normalized):
        raise ValueError(f"{label} output paths must contain between one and eight fields")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} output paths must be unique")


def _capability_call(name: str, arguments: list[ProgramArgument]) -> CapabilityCall:
    return CapabilityCall(
        name=name,
        arguments={item.name: cast(JsonValue, deepcopy(item.value)) for item in arguments},
    )


def _condition_matches(condition: ProgramCondition, observation: Observation) -> bool:
    found, actual = _read_path(
        cast(JsonValue, observation.model_dump(mode="json")),
        condition.path,
    )
    if condition.operator == "exists":
        return found == condition.value
    if not found:
        return False
    if condition.operator == "eq":
        return actual == condition.value
    if condition.operator == "ne":
        return actual != condition.value
    if isinstance(actual, bool) or not isinstance(actual, int | float):
        return False
    if isinstance(condition.value, bool) or not isinstance(condition.value, int | float):
        return False
    if condition.operator == "gt":
        return actual > condition.value
    if condition.operator == "gte":
        return actual >= condition.value
    if condition.operator == "lt":
        return actual < condition.value
    return actual <= condition.value


def _subcall(step: str, call: CapabilityCall, observation: Observation) -> JsonObject:
    return cast(
        JsonObject,
        {
            "step": step,
            "call": call.model_dump(mode="json"),
            "observation": observation.model_dump(mode="json"),
        },
    )


def _compact_result(step: str, paths: list[list[str]], observation: Observation) -> JsonObject:
    source = cast(JsonValue, observation.model_dump(mode="json"))
    selected: JsonObject = {}
    missing: list[JsonValue] = []
    for path in paths:
        found, value = _read_path(source, path)
        key = ".".join(path)
        if found:
            selected[key] = deepcopy(value)
        else:
            missing.append(key)
    return {
        "step": step,
        "action_kind": observation.action_kind,
        "ok": observation.ok,
        "summary": observation.summary,
        "terminal": observation.terminal,
        "selected": selected,
        "missing_paths": missing,
    }


def _read_path(value: JsonValue, path: list[str]) -> tuple[bool, JsonValue]:
    current = value
    for field in path:
        if not isinstance(current, dict) or field not in current:
            return False, None
        current = current[field]
    return True, current


def _program_error(code: str, message: str) -> Observation:
    message = message[:1_500]
    return Observation(
        action_kind=PROGRAM_CAPABILITY_NAME,
        ok=False,
        summary=f"Program rejected: {message}",
        data={"code": code, "message": message},
    )


def _nested_error(action_kind: str, message: str) -> Observation:
    message = message[:1_500]
    return Observation(
        action_kind=action_kind,
        ok=False,
        summary=f"Nested program call rejected: {message}",
        data={"code": "INVALID_NESTED_CALL", "message": message},
    )


def _bound_compact_data(data: JsonObject) -> tuple[JsonObject, bool]:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= _MAX_COMPACT_OUTPUT_BYTES:
        return data, True
    results = data.get("results")
    summaries: list[JsonValue] = []
    if isinstance(results, list):
        for value in results:
            if isinstance(value, dict):
                summaries.append(
                    {
                        key: deepcopy(value[key])
                        for key in ("step", "action_kind", "ok", "summary", "terminal")
                        if key in value
                    }
                )
    bounded = {key: deepcopy(value) for key, value in data.items() if key != "results"}
    bounded.update(
        {
            "code": "PROGRAM_OUTPUT_TOO_LARGE",
            "output_bytes": len(encoded),
            "output_limit_bytes": _MAX_COMPACT_OUTPUT_BYTES,
            "results": summaries,
        }
    )
    return cast(JsonObject, bounded), False


_PROGRAM_DESCRIPTION = (
    "Define and execute, or reuse, a bounded run-scoped program over current Environment "
    "capabilities. Use it only when one chosen primary call may be followed by one mechanical, "
    "read-only call whose condition depends on the primary structured observation; fresh "
    "reasoning must not be needed between calls. Supply primary/follow-up arguments on every "
    "execution. condition=null follows a successful non-terminal primary call without an "
    "extra predicate. Conditions and output paths start at the Observation root; tool response "
    'fields require the data prefix, e.g. ["data", "status"]. exists requires a boolean value. '
    "Programs cannot access shell, filesystem, network, credentials, terminal "
    "capabilities, another program, or a batch."
)

_PROGRAM_INSTRUCTIONS = """# Programmatic execution

`execute_program` can register and reuse a bounded program during the current run. A program
contains one primary capability and at most one conditional read-only follow-up. Supply each
underlying call's arguments on every execution. The condition reads the primary Observation,
and output paths select only evidence needed by the next decision. Every nested call is checked
against the latest Environment catalog and normal Environment validation. Programs disappear
when the run ends and never grant new authority.

The Observation envelope has action_kind, ok, summary, data, and terminal fields. The tool's
JSON response is inside data. For example, a response containing clock.simulation_time is read
with ["data", "clock", "simulation_time"]. Output selection uses the same root: select an
operation's status with ["data", "status"]. Use the actual field names documented by the world.

For exists, value must be true (field present) or false (field absent), never null. Equality
conditions eq/ne accept scalar values including null; gt/gte/lt/lte require numeric values.
With condition=null, the follow-up runs after a successful, non-terminal primary call. It is
still skipped if the primary fails or completes the world.

When the world provides time advancement and operation inspection, a mechanical wait can use
the documented time-advance tool as primary, the operation reader as follow-up, condition=null,
and ["data", "status"] as a follow-up output path if that is the documented response field.
Supply the wait duration and operation ID through the respective tools' arguments. A definition
rejected before execution has not run its primary call; correct it or run those calls separately.
""".strip()
