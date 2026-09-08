from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote

from uptick_agent.core.models import (
    Capability,
    CapabilityCall,
    CapabilityCatalog,
    EnvironmentState,
    EnvironmentTelemetry,
    JsonObject,
    Observation,
    RunCompletion,
    RunResult,
    RunSpec,
)
from uptick_agent.environments.discovered.models import HttpTool, WorldMeaning
from uptick_agent.environments.discovered.schema import model_schema, validate, wire_arguments
from uptick_agent.environments.discovered.transport import SessionTransport, TransportError


class DiscoveredRunResult(RunResult):
    simulator_run_id: str
    objective: str
    final_state: JsonObject


class DiscoveredSession:
    def __init__(
        self,
        *,
        transport: SessionTransport,
        tools: list[HttpTool],
        meaning: WorldMeaning,
        source: str,
        spec: RunSpec,
        contract_sha256: str,
    ) -> None:
        self.transport = transport
        self.tools = {tool.name: tool for tool in tools}
        self.meaning = meaning
        self.source = source
        self.spec = spec
        self._contract_sha256 = contract_sha256
        self._request_number = 0
        self._request_prefix = hashlib.sha256(spec.run_id.encode()).hexdigest()[:20]
        self._uncertain_requests: dict[str, str] = {}
        self._bound = False
        self._credentials: dict[str, dict[str, str]] = {}
        self._recent: OrderedDict[str, dict] = OrderedDict()
        self._final: JsonObject = {}

    def _catalog(self) -> CapabilityCatalog:
        capabilities = []
        for tool in self.tools.values():
            assert tool.meaning is not None
            schema = copy.deepcopy(tool.schema)
            if tool.target_auth:
                properties = cast(dict, schema["properties"])
                properties["credential_ref"] = {
                    "type": "string",
                    "minLength": 1,
                    "description": "Issued credential_id registered through the access resolver.",
                }
                schema["required"] = [*cast(list, schema.get("required", [])), "credential_ref"]
            description = tool.description + "\n" + tool.meaning.notes
            if tool.meaning.role == "credentials":
                description = (
                    "Register issued server access by credential_id. Returns a credential_ref "
                    "for subsequent tools; secret values remain private to the executor."
                )
            if tool.asynchronous:
                description += " Accepted operations must be verified through the operation reader."
            capabilities.append(
                Capability(
                    name=tool.name,
                    description=description,
                    input_schema=model_schema(schema),
                    mutates_state=tool.meaning.mutates_state,
                )
            )
        return CapabilityCatalog(items=capabilities)

    def memory_profile_version(self) -> str:
        return "contract-v1-" + self._contract_sha256

    async def initialize(self) -> str:
        return self.source + (
            "\n\nRuntime: optional arguments use null to mean omitted. Issued server access is "
            "registered by the credentials resolver, which returns credential_ref; use that handle "
            "instead of username/password. The executor binds run IDs, request IDs and HTTP auth. "
            "The world completes when responses report its documented completed status. "
            "Continue advancing and observing until then; no synthetic finish operation exists. "
            "recent_observations are historical snapshots with their own clocks."
        )

    async def bootstrap_capabilities(self) -> CapabilityCatalog:
        return self._catalog()

    async def start(self, spec: RunSpec) -> EnvironmentState:
        if self._bound or spec.run_id != self.spec.run_id or spec.environment_profile is None:
            raise ValueError("invalid discovered session binding")
        self._bound = True
        overview = next(
            t for t in self.tools.values() if t.meaning and t.meaning.role == "overview"
        )
        observation = await self._execute(overview, {})
        return EnvironmentState(
            profile=spec.environment_profile,
            status="terminal" if observation.terminal else "active",
            decision_view={"objective": self.meaning.objective},
            latest_observation=observation,
        )

    async def capabilities(self, state: EnvironmentState) -> CapabilityCatalog:
        del state
        return self._catalog()

    async def execute(self, call: CapabilityCall, state: EnvironmentState) -> Observation:
        if not self._bound or state.status != "active":
            raise ValueError("session is not active")
        tool = self.tools.get(call.name)
        if tool is None:
            raise ValueError("unknown discovered capability")
        try:
            return await self._execute(tool, call.arguments)
        except (ValueError, TransportError) as error:
            code = error.code if isinstance(error, TransportError) else "INVALID_ARGUMENTS"
            return Observation(
                action_kind=call.name,
                ok=False,
                summary=f"{call.name}: {code}",
                data={"error": code, "message": self._sanitize(str(error))},
            )

    async def _execute(self, tool: HttpTool, arguments: JsonObject) -> Observation:
        supplied = copy.deepcopy(arguments)
        reference = supplied.pop("credential_ref", None) if tool.target_auth else None
        supplied = wire_arguments(supplied, tool.schema)
        validate(supplied, tool.schema)
        target_auth = None
        if tool.target_auth:
            if not isinstance(reference, str) or reference not in self._credentials:
                raise ValueError(
                    "register issued server access first, then pass its credential_ref"
                )
            target_auth = self._credentials[reference]
        self._request_number += 1
        request_key = json.dumps([tool.name, supplied], sort_keys=True)
        request_id = self._uncertain_requests.get(
            request_key, f"req-{self._request_prefix}-{self._request_number}"
        )
        path = tool.path
        query: list[tuple[str, str]] = []
        body = None
        if tool.command is not None:
            body = {"request_id": request_id, "command": tool.command, "params": supplied}
            if target_auth is not None:
                body["target_auth"] = target_auth
        else:
            for parameter in tool.parameters:
                name = cast(str, parameter["name"])
                if name not in supplied:
                    continue
                value = supplied[name]
                if parameter["in"] == "path":
                    path = path.replace("{" + name + "}", quote(str(value), safe=""))
                elif isinstance(value, list):
                    if parameter.get("explode", True):
                        query.extend((name, str(v)) for v in value)
                    else:
                        query.append((name, ",".join(map(str, value))))
                else:
                    query.append(
                        (name, str(value).lower() if isinstance(value, bool) else str(value))
                    )
            if tool.body_schema is not None:
                properties = cast(dict, tool.body_schema.get("properties", {}))
                body = {k: v for k, v in supplied.items() if k in properties}
                if "request_id" in properties:
                    body["request_id"] = request_id
                validate(body, tool.body_schema)
        try:
            response = await self.transport.request(
                tool.method, path, panel=tool.panel_auth, body=body, params=query or None
            )
        except TransportError as error:
            if body is not None and (error.status == 0 or error.status >= 500):
                self._uncertain_requests[request_key] = request_id
            raise
        self._uncertain_requests.pop(request_key, None)
        if not isinstance(response, dict):
            raise ValueError("world operation returned a non-object response")
        self._capture_credentials(response)
        sanitized = self._sanitize(response)
        assert isinstance(sanitized, dict)
        clock = response.get(self.meaning.clock_field, {})
        completed = response.get(self.meaning.status_field) == self.meaning.completed_status
        if isinstance(clock, dict):
            now, end = clock.get(self.meaning.time_field), clock.get(self.meaning.end_time_field)
            # Both fields are RFC3339 strings in discovered contracts; parse before comparing.
            if isinstance(now, str) and isinstance(end, str):
                from datetime import datetime

                completed |= datetime.fromisoformat(now) >= datetime.fromisoformat(end)
        if tool.meaning and tool.meaning.role == "overview":
            self._final = sanitized
        summary_data = {k: v for k, v in sanitized.items() if k != self.meaning.clock_field}
        summary = json.dumps(summary_data, ensure_ascii=False, separators=(",", ":"))
        return Observation(
            action_kind=tool.name,
            summary=f"{tool.name}: {summary[:1100]}",
            data=sanitized,
            terminal=completed,
        )

    def _capture_credentials(self, value: Any) -> None:
        if isinstance(value, dict):
            identifier = value.get("credential_id")
            if isinstance(value.get("username"), str) and isinstance(value.get("password"), str):
                self.transport.secret_values.update((value["username"], value["password"]))
                if isinstance(identifier, str):
                    self._credentials[identifier] = {
                        "username": value["username"],
                        "password": value["password"],
                    }
            for child in value.values():
                self._capture_credentials(child)
        elif isinstance(value, list):
            for child in value:
                self._capture_credentials(child)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned = {
                k: self._sanitize(v)
                for k, v in value.items()
                if k.lower()
                not in {"username", "password", "authorization", "target_auth", "run_id"}
            }
            if value.get("credential_id") in self._credentials:
                cleaned["credential_ref"] = value["credential_id"]
            return cleaned
        if isinstance(value, list):
            return [self._sanitize(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.transport.secret_values, key=len, reverse=True):
                if secret:
                    value = value.replace(secret, "[private]")
            # Inbox can embed credentials in text, while credential_id stays usable.
            return re.sub(
                r"(?i)((?:password|username|пароль|логин)(?:\s*[:=]\s*|\s+))[^\s,;]+",
                r"\1[private]",
                value,
            )
        return value

    def reduce(
        self, state: EnvironmentState, call: CapabilityCall, observation: Observation
    ) -> EnvironmentState:
        # Preserve the complete sanitized tool response in the model-facing state.
        # Only the number of historical snapshots is bounded, not their contents.
        self._recent.pop(call.name, None)
        self._recent[call.name] = {
            "arguments": copy.deepcopy(call.arguments),
            "observation": copy.deepcopy(observation.data),
        }
        while len(self._recent) > 6:
            self._recent.popitem(last=False)
        return state.model_copy(
            update={
                "status": "terminal" if observation.terminal else "active",
                "latest_observation": observation,
                "decision_view": {
                    "objective": self.meaning.objective,
                    "recent_observations": dict(self._recent),
                },
            },
            deep=True,
        )

    def prepare_observation_release(
        self, names: list[str]
    ) -> Callable[[EnvironmentState], EnvironmentState]:
        if len(names) != len(set(names)):
            raise ValueError("release snapshot names must be unique")
        if set(names) - self._recent.keys():
            raise ValueError("release contains an unknown recent snapshot")
        shown = {name: self._recent[name] for name in names}

        def commit(state: EnvironmentState) -> EnvironmentState:
            for name, snapshot in shown.items():
                # Identity distinguishes a fresh response even if its bytes are equal.
                if self._recent.get(name) is snapshot:
                    del self._recent[name]
            return state.model_copy(
                update={
                    "decision_view": {
                        **state.decision_view,
                        "recent_observations": dict(self._recent),
                    }
                },
                deep=True,
            )

        return commit

    async def result(self, state: EnvironmentState, completion: RunCompletion) -> RunResult:
        overview = next(
            t for t in self.tools.values() if t.meaning and t.meaning.role == "overview"
        )
        await self._execute(overview, {})
        return DiscoveredRunResult(
            run_id=self.spec.run_id,
            simulator_run_id=self.transport.remote_run_id or "",
            status=str(self._final.get(self.meaning.status_field, state.status)),
            steps=completion.steps,
            duration_seconds=completion.duration_seconds,
            stop_reason=completion.stop_reason,
            forced=completion.forced,
            objective=self.meaning.objective,
            final_state=self._final,
        )

    def telemetry(self, run_id: str) -> EnvironmentTelemetry:
        if run_id != self.spec.run_id:
            raise ValueError("unknown run")
        return self.transport.telemetry()

    async def aclose(self) -> None:
        self._credentials.clear()
        await self.transport.aclose()
