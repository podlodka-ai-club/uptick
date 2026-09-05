"""Small, secret-aware HTTP adapter for the simulator API v2.

The adapter deliberately returns dictionaries rather than exposing simulator
or credential models to the decision layer.  Panel and server credentials
remain in this object and are injected only while constructing an HTTP request.
"""

from __future__ import annotations

import base64
import contextlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from uptick_agent.redaction import sanitize_json
from uptick_agent.simulator.timestamps import TimestampOrder, parse_rfc3339, query_timestamp

_DEFAULT_BASE_URL = "http://81.176.229.58:8080"
_COMMANDS = frozenset(
    {
        "firewall.rules.list",
        "firewall.rules.upsert",
        "firewall.rules.delete",
        "server.types.list",
        "server.create",
        "server.inspect",
        "server.delete",
        "database.create",
        "database.inspect",
        "database.backup",
        "database.backups.list",
        "database.restore",
        "site.config.get",
        "site.stop",
        "site.start",
        "site.database.set",
        "disk.usage",
        "disk.cleanup",
    }
)
_ASYNC_COMMANDS = frozenset(
    {"server.create", "server.delete", "database.backup", "database.restore", "site.stop"}
)
_TARGET_AUTH_COMMANDS = frozenset(
    {
        "database.create",
        "database.inspect",
        "database.backup",
        "database.restore",
        "disk.usage",
        "disk.cleanup",
    }
)
_TARGET_AUTH_ERRORS = frozenset({"TARGET_UNAUTHORIZED", "CREDENTIALS_EXPIRED"})
_SENSITIVE_MARKERS = re.compile(
    r"(?i)(?:credential|password|passwd|username|login|target_auth|secret|token|"
    r"access|auth|парол|логин|секрет)"
)
_REF_PATTERN = re.compile(
    r"(?i)\b(credential[_ -]?id|server[_ -]?id|database[_ -]?id)\s*[:=]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127})"
)


def _parse_query_time(value: datetime | str) -> TimestampOrder:
    try:
        return parse_rfc3339(value)
    except ValueError as error:
        raise SimulatorV2ApiError(
            400, "INVALID_REQUEST", "from and to must be valid timezone-aware RFC3339 date-times"
        ) from error


def _query_time(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    try:
        return query_timestamp(value)
    except ValueError as error:
        raise SimulatorV2ApiError(
            400, "INVALID_REQUEST", "from and to must be valid timezone-aware RFC3339 date-times"
        ) from error


def _validate_query_window(
    from_time: datetime | str | None,
    to_time: datetime | str | None,
    *,
    require_pair: bool,
) -> None:
    if require_pair and (from_time is None) != (to_time is None):
        raise SimulatorV2ApiError(400, "INVALID_REQUEST", "from and to must be supplied together")
    if from_time is None or to_time is None:
        return
    if _parse_query_time(from_time) > _parse_query_time(to_time):
        raise SimulatorV2ApiError(400, "INVALID_REQUEST", "from must not be later than to")


def _query_value(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


class SimulatorV2ApiError(RuntimeError):
    """A safe API error with no response body or request payload attached."""

    __slots__ = ("status_code", "code", "message")

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = int(status_code)
        self.code = code if code else "HTTP_ERROR"
        self.message = message if message else "Simulator request failed"
        super().__init__(f"HTTP {self.status_code} {self.code}: {self.message}")


@dataclass(slots=True)
class _RunAuthState:
    panel_username: str = field(repr=False)
    panel_password: str = field(repr=False)
    credential_by_resource: dict[str, str] = field(default_factory=dict)
    server_by_database: dict[str, str] = field(default_factory=dict)
    known_credential_ids: set[str] = field(default_factory=set)


class _SecretRedactor:
    """Exact-value redaction layered on the repository's shared sanitizer."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: object) -> None:
        if isinstance(value, str) and value:
            self._values.add(value)

    def register_basic(self, username: str, password: str) -> None:
        self.register(username)
        self.register(password)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.register(token)
        self.register(f"Basic {token}")

    def sanitize(self, value: object) -> object:
        safe = sanitize_json(value)
        return self._replace(safe)

    def _replace(self, value: object) -> object:
        if isinstance(value, dict):
            return {key: self._replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace(item) for item in value]
        if isinstance(value, str):
            for secret in sorted(self._values, key=len, reverse=True):
                value = value.replace(secret, "<redacted>")
        return value


class SimulatorV2Client:
    """Async v2 simulator client with private per-run credential resolution."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout),
            headers={"Accept": "application/json"},
        )
        self._runs: dict[str, _RunAuthState] = {}
        self._redactor = _SecretRedactor()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        run_id: str | None = None,
        panel_auth: bool = False,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        required: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], int]:
        auth: httpx.BasicAuth | None = None
        if panel_auth:
            if run_id is None or run_id not in self._runs:
                raise SimulatorV2ApiError(
                    401, "CONTROL_UNAUTHORIZED", "Control panel access is unavailable"
                )
            state = self._runs[run_id]
            auth = httpx.BasicAuth(state.panel_username, state.panel_password)
            self._redactor.register_basic(state.panel_username, state.panel_password)
        try:
            response = await self.client.request(method, path, json=json, params=params, auth=auth)
        except httpx.HTTPError:
            raise SimulatorV2ApiError(503, "HTTP_ERROR", "Simulator request failed") from None

        if response.is_error:
            self._raise_http_error(response)
        try:
            body = response.json()
        except (TypeError, ValueError):
            raise SimulatorV2ApiError(
                response.status_code, "INVALID_RESPONSE", "Simulator returned invalid JSON"
            ) from None
        if not isinstance(body, dict):
            raise SimulatorV2ApiError(
                response.status_code, "INVALID_RESPONSE", "Simulator returned an invalid object"
            )
        missing = [name for name in required if name not in body]
        if missing:
            raise SimulatorV2ApiError(
                response.status_code,
                "INVALID_RESPONSE",
                "Simulator response is missing required fields",
            )
        return body, response.status_code

    def _raise_http_error(self, response: httpx.Response) -> None:
        code = "HTTP_ERROR"
        message = "Simulator request failed"
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
        if isinstance(body, dict):
            raw_code = body.get("error")
            raw_message = body.get("message")
            if isinstance(raw_code, str) and raw_code:
                code = self._safe_string(raw_code, fallback=code)
            if isinstance(raw_message, str) and raw_message:
                message = self._safe_string(raw_message, fallback=message)
        raise SimulatorV2ApiError(response.status_code, code, message)

    def _safe_string(self, value: str, *, fallback: str) -> str:
        try:
            safe = self._redactor.sanitize(value)
        except (TypeError, ValueError):
            return fallback
        return safe if isinstance(safe, str) else fallback

    def _public(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            safe = self._redactor.sanitize(body)
        except (TypeError, ValueError):
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Simulator response could not be sanitized"
            ) from None
        if not isinstance(safe, dict):
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Simulator returned an invalid object"
            )
        return safe

    @staticmethod
    def _run_path(run_id: str, suffix: str = "") -> str:
        return f"/v2/runs/{quote(run_id, safe='')}{suffix}"

    @staticmethod
    def _require_dict(
        value: object, *, message: str = "Command params must be an object"
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise SimulatorV2ApiError(400, "INVALID_REQUEST", message)
        return value

    def _record_resource_refs(self, run_id: str, value: object) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        if isinstance(value, dict):
            server_id = value.get("server_id")
            credential_id = value.get("credential_id")
            database_id = value.get("database_id")
            if isinstance(server_id, str) and isinstance(credential_id, str):
                state.credential_by_resource[server_id] = credential_id
                state.known_credential_ids.add(credential_id)
            if isinstance(database_id, str) and isinstance(server_id, str):
                state.server_by_database[database_id] = server_id
            if isinstance(server_id, str) and isinstance(value.get("database_ids"), list):
                for item in value["database_ids"]:
                    if isinstance(item, str):
                        state.server_by_database[item] = server_id
            resource_id = value.get("resource_id")
            if isinstance(resource_id, str) and isinstance(credential_id, str):
                state.credential_by_resource[resource_id] = credential_id
                state.known_credential_ids.add(credential_id)
            for item in value.values():
                self._record_resource_refs(run_id, item)
        elif isinstance(value, list):
            for item in value:
                self._record_resource_refs(run_id, item)

    def _replace_resource_refs(self, run_id: str, body: dict[str, Any]) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        state.credential_by_resource.clear()
        state.server_by_database.clear()
        servers = body.get("servers")
        if isinstance(servers, list):
            self._record_resource_refs(run_id, servers)

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str, request_id: str
    ) -> dict[str, Any]:
        body, status = await self._request_json(
            "POST",
            "/v2/start",
            json={
                "seed": seed,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "request_id": request_id,
            },
            required=("run_id", "status", "commands_markdown", "control_panel_auth"),
        )
        auth = body.get("control_panel_auth")
        if not isinstance(auth, dict):
            raise SimulatorV2ApiError(
                status, "INVALID_RESPONSE", "Start response has invalid panel access"
            )
        username = auth.get("username")
        password = auth.get("password")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password
        ):
            raise SimulatorV2ApiError(
                status, "INVALID_RESPONSE", "Start response has invalid panel access"
            )
        run_id = body["run_id"]
        if not isinstance(run_id, str) or not run_id:
            raise SimulatorV2ApiError(
                status, "INVALID_RESPONSE", "Start response has invalid run ID"
            )
        self._redactor.register_basic(username, password)
        self._runs[run_id] = _RunAuthState(username, password)

        public = dict(body)
        public.pop("control_panel_auth", None)
        catalog = public.get("commands_markdown")
        if isinstance(catalog, str):
            public["commands_markdown"] = self._safe_catalog(catalog)
        return self._public(public)

    def _safe_catalog(self, text: str) -> str:
        safe = self._safe_string(text, fallback="<redacted>")
        return "\n".join(
            "<redacted credential-bearing content>" if _SENSITIVE_MARKERS.search(line) else line
            for line in safe.splitlines()
        )

    async def overview(self, run_id: str) -> dict[str, Any]:
        body, _ = await self._request_json(
            "GET", self._run_path(run_id, "/overview"), required=("clock", "run_id", "status")
        )
        return self._public(body)

    async def metrics(self, run_id: str) -> dict[str, Any]:
        body, _ = await self._request_json(
            "GET", self._run_path(run_id, "/metrics"), required=("clock", "current", "series")
        )
        return self._public(body)

    async def query_metrics(
        self,
        run_id: str,
        *,
        from_time: datetime | str | None = None,
        to_time: datetime | str | None = None,
        step_seconds: int = 60,
        names: list[str] | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        _validate_query_window(from_time, to_time, require_pair=True)
        if not isinstance(step_seconds, int) or isinstance(step_seconds, bool) or step_seconds < 1:
            raise SimulatorV2ApiError(400, "INVALID_REQUEST", "step_seconds must be at least 1")
        if names is not None and (
            not names or any(not isinstance(name, str) or not name for name in names)
        ):
            raise SimulatorV2ApiError(400, "INVALID_REQUEST", "names must be non-empty strings")
        query: dict[str, Any] = {
            "step_seconds": step_seconds,
        }
        for key, value in (
            ("from", _query_time(from_time)),
            ("to", _query_time(to_time)),
            ("page", page),
        ):
            if value is not None:
                query[key] = value
        if names is not None:
            query["names"] = ",".join(names)
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, "/metrics"),
            params=query,
            required=("clock", "current", "series"),
        )
        return self._public(body)

    async def logs(
        self,
        run_id: str,
        *,
        from_time: str | None = None,
        to_time: str | None = None,
        status: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("from", from_time),
            ("to", to_time),
            ("status", status),
            ("cursor", cursor),
        ):
            if value is not None:
                query[key] = value
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, "/logs"),
            params=query,
            required=("clock", "logs", "next_cursor"),
        )
        return self._public(body)

    async def query_logs(
        self,
        run_id: str,
        *,
        from_time: datetime | str | None = None,
        to_time: datetime | str | None = None,
        page: str | None = None,
        status: int | None = None,
        has_error: bool | None = None,
        error: str | None = None,
        source_ip: object | None = None,
        source_cidr: object | None = None,
        user_agent: str | None = None,
        region_code: str | None = None,
        firewall_rule_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_query_window(from_time, to_time, require_pair=False)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise SimulatorV2ApiError(400, "INVALID_REQUEST", "limit must be between 1 and 1000")
        if has_error is False and error is not None:
            raise SimulatorV2ApiError(
                400, "INVALID_REQUEST", "error is incompatible with has_error=false"
            )
        query: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("from", _query_time(from_time)),
            ("to", _query_time(to_time)),
            ("page", page),
            ("status", status),
            ("has_error", has_error),
            ("error", error),
            ("source_ip", _query_value(source_ip)),
            ("source_cidr", _query_value(source_cidr)),
            ("user_agent", user_agent),
            ("region_code", region_code),
            ("firewall_rule_id", firewall_rule_id),
            ("cursor", cursor),
        ):
            if value is not None:
                query[key] = value
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, "/logs"),
            params=query,
            required=("clock", "logs", "next_cursor"),
        )
        return self._public(body)

    async def resources(self, run_id: str) -> dict[str, Any]:
        body, _ = await self._request_json(
            "GET", self._run_path(run_id, "/resources"), required=("clock", "servers")
        )
        self._replace_resource_refs(run_id, body)
        return self._public(body)

    async def probe(
        self, run_id: str, *, request_id: str, page: str, product_id: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"request_id": request_id, "page": page}
        if product_id is not None:
            payload["product_id"] = product_id
        body, _ = await self._request_json(
            "POST",
            self._run_path(run_id, "/probes"),
            json=payload,
            required=("clock", "request_id", "page", "status"),
        )
        return self._public(body)

    async def inbox(
        self, run_id: str, *, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        # Resolve current references before exposing free-form message text. If
        # the read-only refresh cannot run, sensitive messages are projected away.
        with contextlib.suppress(SimulatorV2ApiError):
            await self._refresh_resources(run_id)
        query: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, "/inbox"),
            params=query,
            required=("clock", "messages", "next_cursor"),
        )
        self._learn_inbox_refs(run_id, body)
        await self._prefetch_known_credentials(run_id)
        return self._public_inbox(body)

    def _learn_inbox_refs(self, run_id: str, body: dict[str, Any]) -> None:
        state = self._runs.get(run_id)
        if state is None or not isinstance(body.get("messages"), list):
            return
        for message in body["messages"]:
            if not isinstance(message, dict):
                continue
            text = " ".join(str(message.get(key, "")) for key in ("subject", "description"))
            refs = dict(
                (key.lower().replace("-", "_").replace(" ", "_"), value)
                for key, value in _REF_PATTERN.findall(text)
            )
            credential_id = refs.get("credential_id")
            if credential_id:
                state.known_credential_ids.add(credential_id)

    async def _prefetch_known_credentials(self, run_id: str) -> None:
        state = self._runs.get(run_id)
        if state is None:
            return
        credential_ids = set(state.known_credential_ids) | set(
            state.credential_by_resource.values()
        )
        for credential_id in credential_ids:
            try:
                await self._fetch_credential(run_id, credential_id)
            except SimulatorV2ApiError:
                # Historic/expired refs and unavailable credentials are kept
                # private; the message itself is projected below.
                continue

    def _public_inbox(self, body: dict[str, Any]) -> dict[str, Any]:
        public = dict(body)
        messages = body.get("messages")
        projected: list[dict[str, Any]] = []
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                subject = str(message.get("subject", ""))
                description = str(message.get("description", ""))
                if _SENSITIVE_MARKERS.search(f"{subject} {description}"):
                    refs = _REF_PATTERN.findall(f"{subject} {description}")
                    reference_text = "; ".join(
                        f"{kind.replace('_', ' ')}: {value}" for kind, value in refs
                    )
                    item = {
                        key: message[key]
                        for key in ("message_id", "sender_email", "sent_at")
                        if key in message
                    }
                    item["subject"] = "<redacted delivery notice>"
                    item["description"] = (
                        f"Delivery references ({reference_text})"
                        if reference_text
                        else "<redacted credential delivery message>"
                    )
                    projected.append(item)
                else:
                    projected.append(dict(message))
        public["messages"] = projected
        return self._public(public)

    async def list_commands(self, run_id: str) -> dict[str, Any]:
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, "/control/commands"),
            run_id=run_id,
            panel_auth=True,
            required=("clock", "commands"),
        )
        return self._public(body)

    async def execute_command(
        self, run_id: str, *, request_id: str, command: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if command not in _COMMANDS:
            raise SimulatorV2ApiError(400, "INVALID_REQUEST", "Unknown control command")
        params = self._require_dict(params)
        retried = False
        while True:
            try:
                target_auth: dict[str, str] | None = None
                if command in _TARGET_AUTH_COMMANDS:
                    resource_id = await self._resolve_target_resource(run_id, command, params)
                    target_auth = await self._fetch_credential(
                        run_id,
                        self._credential_id(run_id, resource_id),
                        resource_id=resource_id,
                    )
                payload: dict[str, Any] = {
                    "request_id": request_id,
                    "command": command,
                    "params": params,
                }
                if target_auth is not None:
                    payload["target_auth"] = target_auth
                body, status = await self._request_json(
                    "POST",
                    self._run_path(run_id, "/control/commands"),
                    run_id=run_id,
                    panel_auth=True,
                    json=payload,
                )
            except SimulatorV2ApiError as error:
                if not retried and error.code in _TARGET_AUTH_ERRORS:
                    retried = True
                    await self._refresh_resources(run_id)
                    continue
                raise
            if (command in _ASYNC_COMMANDS) != (status == 202):
                raise SimulatorV2ApiError(
                    status,
                    "INVALID_RESPONSE",
                    "Control command returned an invalid execution status",
                )
            if status == 202:
                self._validate_fields(
                    body, ("clock", "request_id", "command", "operation_id", "status")
                )
            else:
                self._validate_fields(body, ("clock", "request_id", "command", "result"))
            self._record_resource_refs(run_id, body)
            return self._public(body)

    def _validate_fields(self, body: dict[str, Any], fields: tuple[str, ...]) -> None:
        if any(field not in body for field in fields):
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Simulator response is missing required fields"
            )

    async def _resolve_target_resource(
        self, run_id: str, command: str, params: dict[str, Any]
    ) -> str:
        state = self._runs.get(run_id)
        if state is None:
            raise SimulatorV2ApiError(
                401, "CONTROL_UNAUTHORIZED", "Control panel access is unavailable"
            )
        if command in {"database.create", "disk.usage", "disk.cleanup"}:
            resource_id = params.get("server_id")
        else:
            database_id = params.get("database_id")
            resource_id = (
                state.server_by_database.get(database_id) if isinstance(database_id, str) else None
            )
        if not isinstance(resource_id, str) or not resource_id:
            await self._refresh_resources(run_id)
            state = self._runs[run_id]
            if command in {"database.create", "disk.usage", "disk.cleanup"}:
                resource_id = params.get("server_id")
            else:
                database_id = params.get("database_id")
                resource_id = (
                    state.server_by_database.get(database_id)
                    if isinstance(database_id, str)
                    else None
                )
        if isinstance(resource_id, str) and resource_id not in state.credential_by_resource:
            await self._refresh_resources(run_id)
            state = self._runs[run_id]
        if not isinstance(resource_id, str) or not resource_id:
            raise SimulatorV2ApiError(404, "RESOURCE_NOT_FOUND", "Target resource is unavailable")
        return resource_id

    def _credential_id(self, run_id: str, resource_id: str) -> str:
        state = self._runs.get(run_id)
        credential_id = state.credential_by_resource.get(resource_id) if state else None
        if not credential_id:
            raise SimulatorV2ApiError(
                404, "RESOURCE_NOT_FOUND", "Target credentials are unavailable"
            )
        return credential_id

    async def _refresh_resources(self, run_id: str) -> None:
        body, _ = await self._request_json(
            "GET", self._run_path(run_id, "/resources"), required=("clock", "servers")
        )
        self._replace_resource_refs(run_id, body)

    async def _fetch_credential(
        self, run_id: str, credential_id: str, *, resource_id: str | None = None
    ) -> dict[str, str]:
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, f"/credentials/{quote(credential_id, safe='')}"),
            run_id=run_id,
            panel_auth=True,
            required=("clock", "credential"),
        )
        credential = body.get("credential")
        if not isinstance(credential, dict):
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Credential response is invalid")
        if credential.get("credential_id") != credential_id:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Credential response is invalid")
        if resource_id is not None and credential.get("resource_id") != resource_id:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Credential response is invalid")
        username = credential.get("username")
        password = credential.get("password")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or not username
            or not password
        ):
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Credential response is invalid")
        self._redactor.register_basic(username, password)
        return {"username": username, "password": password}

    async def operation(self, run_id: str, operation_id: str) -> dict[str, Any]:
        body, _ = await self._request_json(
            "GET",
            self._run_path(run_id, f"/operations/{quote(operation_id, safe='')}"),
            required=(
                "clock",
                "operation_id",
                "type",
                "command",
                "request_id",
                "status",
                "progress",
                "result",
            ),
        )
        return self._public(body)

    async def advance_time(
        self,
        run_id: str,
        *,
        request_id: str,
        duration_seconds: int,
        stop_when: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"request_id": request_id, "duration_seconds": duration_seconds}
        if stop_when is not None:
            payload["stop_when"] = stop_when
        body, _ = await self._request_json(
            "POST",
            self._run_path(run_id, "/time/advance"),
            json=payload,
            required=(
                "clock",
                "previous_simulation_time",
                "requested_duration_seconds",
                "processed_events",
                "new_logs",
                "stop_reason",
            ),
        )
        return self._public(body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


__all__ = ["SimulatorV2ApiError", "SimulatorV2Client"]
