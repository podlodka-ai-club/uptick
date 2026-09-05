"""Environment adapter for the simulator's v2 HTTP contract.

The v2 client owns authentication and returns JSON-safe, already normalised
responses.  This module is deliberately small: it translates those responses
to the generic runner contracts and keeps only cursor/deduplication state that
is needed by a run.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from uptick_agent.decisions.actions import (
    AdvanceTime,
    AgentAction,
    FinishRun,
    GetLogs,
    GetMetrics,
    GetOperation,
    GetOverview,
    GetResources,
    V2AdvanceTime,
    V2ProbePage,
)
from uptick_agent.decisions.contracts import ToolResult
from uptick_agent.memory.contracts import ObjectiveMetric, OperationLink
from uptick_agent.redaction import sanitize_json
from uptick_agent.runs.results import RunResult
from uptick_agent.simulator.v2_client import SimulatorV2ApiError, SimulatorV2Client
from uptick_agent.v2_actions import ControlCommand, GetControlCommands, GetInbox


@dataclass(slots=True)
class SimulatorV2Session:
    """Runner-facing state for one v2 run.

    Credentials intentionally do not have a field here.  The v2 client keeps
    those private while this session contains only attribution, clocks and
    pagination bookkeeping.
    """

    run_id: str
    seed: int
    agent_id: str
    agent_version: str
    status: str
    simulation_time: datetime | None
    logs_from: datetime | None
    request_prefix: str = field(default_factory=lambda: uuid4().hex[:12])
    request_number: int = 0
    logs_cursor: str | None = None
    logs_cursor_status: int | None = None
    logs_initial_from: datetime | None = None
    logs_from_by_status: dict[str, datetime | None] = field(default_factory=dict)
    logs_cursor_by_status: dict[str, str | None] = field(default_factory=dict)
    seen_log_ids: set[str] = field(default_factory=set)
    inbox_cursor: str | None = None
    seen_inbox_ids: set[str] = field(default_factory=set)

    def next_request_id(self, kind: str) -> str:
        self.request_number += 1
        return f"uptick-{self.request_prefix}-{kind}-{self.request_number:05d}"


# One API page per runner step keeps the result bounded at the v2 default limit
# while retaining the cursor for the next observation.
_MAX_PAGES_PER_READ = 1


def _safe(value: object) -> dict[str, Any]:
    """Sanitize a v2 client's JSON object at the generic boundary."""

    if not isinstance(value, dict):
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Simulator returned an invalid object")
    try:
        safe = sanitize_json(value)
    except (TypeError, ValueError) as error:
        raise SimulatorV2ApiError(
            200, "INVALID_RESPONSE", "Simulator response could not be sanitized"
        ) from error
    if not isinstance(safe, dict):
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Simulator returned an invalid object")
    return _without_credentials(safe)


def _without_credentials(value: object) -> object:
    if isinstance(value, dict):
        sensitive = {
            "control_panel_auth",
            "target_auth",
            "credentials",
            "credential",
            "password",
            "username",
        }
        return {
            key: _without_credentials(item)
            for key, item in value.items()
            if key.lower() not in sensitive
        }
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


def _nested(value: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", f"Simulator response has invalid {key}")
    return value


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _clock_time(data: Mapping[str, Any]) -> datetime | None:
    return _datetime(_nested(data, "clock", "simulation_time")) or _datetime(
        data.get("simulation_time")
    )


def _required_clock(data: Mapping[str, Any]) -> Mapping[str, Any]:
    clock = data.get("clock")
    if not isinstance(clock, Mapping):
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Simulator response has invalid clock")
    if _datetime(clock.get("simulation_time")) is None:
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Simulator response has invalid clock")
    remaining = clock.get("remaining_seconds")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or remaining < 0:
        raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Simulator response has invalid clock")
    return clock


def _remaining_seconds(data: Mapping[str, Any]) -> object:
    return _nested(data, "clock", "remaining_seconds")


def _clock_terminal(data: Mapping[str, Any]) -> bool:
    remaining = _remaining_seconds(data)
    return isinstance(remaining, (int, float)) and remaining <= 0


def _objective_metrics(data: Mapping[str, Any], *, kind: str) -> list[ObjectiveMetric]:
    if kind == "get_overview":
        source = data
    elif kind == "get_metrics":
        current = data.get("current")
        source = current if isinstance(current, Mapping) else {}
    else:
        return []

    metric_specs = (
        ("uptime_ratio", "ratio"),
        ("downtime_seconds", "seconds"),
        ("observed_seconds", "seconds"),
        ("available_seconds", "seconds"),
        ("total_cost_minor", "minor"),
        ("server_cost_minor", "minor"),
        ("backup_storage_cost_minor", "minor"),
        ("current_cost_per_hour_minor", "minor"),
    )
    # Overview nests availability and costs; metrics.current is already flat.
    availability = source.get("availability")
    costs = source.get("costs")
    if isinstance(availability, Mapping):
        source = {**source, **availability}
    if isinstance(costs, Mapping):
        source = {**source, **costs}

    result: list[ObjectiveMetric] = []
    for name, unit in metric_specs:
        value = source.get(name)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        result.append(ObjectiveMetric(name=name, value=float(value), unit=unit))
    return result


def _active_server_counts(data: Mapping[str, Any]) -> dict[str, int | str]:
    """Count active backend and database rows when the inventory is complete."""

    servers = data.get("servers")
    if not isinstance(servers, list):
        return {"backend": "unknown", "database": "unknown"}

    counts: Counter[str] = Counter()
    for server in servers:
        if not isinstance(server, Mapping):
            return {"backend": "unknown", "database": "unknown"}
        role = server.get("role")
        status = server.get("status")
        if (
            not isinstance(role, str)
            or role not in {"backend", "database"}
            or not isinstance(status, str)
            or not status
        ):
            return {"backend": "unknown", "database": "unknown"}
        if status == "active":
            counts[role] += 1
    return {"backend": counts["backend"], "database": counts["database"]}


def _terminal_for(data: Mapping[str, Any], *, overview: bool = False) -> bool:
    # An operation's status and a command's status describe that operation,
    # never the run.  Only overview.status or the run clock may end a step.
    if overview:
        status = data.get("status")
        if status not in {"running", "completed", "failed"}:
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Simulator response has invalid status"
            )
        if status != "running":
            return True
    _required_clock(data)
    return _clock_terminal(data)


def _result(
    action_kind: str,
    value: object,
    summary: str,
    *,
    ok: bool = True,
    terminal: bool = False,
    operation_relation: str | None = None,
) -> ToolResult:
    data = _safe(value)
    links: list[OperationLink] = []
    operation_id = data.get("operation_id")
    if isinstance(operation_id, str) and operation_relation is not None:
        links.append(OperationLink(operation_id=operation_id, relation=operation_relation))
    return ToolResult(
        action_kind=action_kind,
        ok=ok,
        summary=summary,
        data=data,
        objective_metrics=_objective_metrics(data, kind=action_kind),
        operation_links=links,
        terminal=terminal,
    )


def _error_result(action_kind: str, error: BaseException, *, terminal: bool = False) -> ToolResult:
    status_code = getattr(error, "status_code", 500)
    code = getattr(error, "code", "HTTP_ERROR")
    message = getattr(error, "message", "Simulator request failed")
    if not isinstance(code, str):
        code = "HTTP_ERROR"
    if not isinstance(message, str):
        message = "Simulator request failed"
    payload = {
        "status_code": status_code,
        "code": code,
        "message": message,
    }
    safe_payload = _safe(payload)
    return ToolResult(
        action_kind=action_kind,
        ok=False,
        summary=(f"Simulator v2 error {safe_payload['code']}: {safe_payload['message']}"),
        data=safe_payload,
        terminal=terminal,
    )


class SimulatorV2Environment:
    """Translate simulator v2 actions and responses to generic agent ports."""

    def __init__(self, client: SimulatorV2Client) -> None:
        self.client = client

    async def start(
        self,
        *,
        seed: int,
        agent_id: str,
        agent_version: str,
        request_id: str | None = None,
    ) -> tuple[SimulatorV2Session, ToolResult]:
        prefix = uuid4().hex[:12]
        started = await self.client.start(
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
            request_id=request_id or f"uptick-{prefix}-start",
        )
        data = _safe(started)
        simulation_time = _clock_time(data)
        run_id = data.get("run_id")
        status = data.get("status")
        if not isinstance(run_id, str) or not run_id:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Start response has invalid run ID")
        if status not in {"running", "completed", "failed"}:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Start response has invalid status")
        if simulation_time is None:
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Start response has invalid simulation time"
            )
        session = SimulatorV2Session(
            run_id=run_id,
            seed=seed,
            agent_id=agent_id,
            agent_version=agent_version,
            status=status,
            simulation_time=simulation_time,
            logs_from=simulation_time,
            logs_initial_from=simulation_time,
            request_prefix=prefix,
        )
        return session, _result(
            "start",
            data,
            f"Run {run_id} started at "
            f"{simulation_time.isoformat() if simulation_time else 'an unknown time'}.",
        )

    async def execute(self, session: SimulatorV2Session, action: AgentAction) -> ToolResult:
        try:
            return await self._execute(session, action)
        except SimulatorV2ApiError as error:
            code = getattr(error, "code", "")
            if code in {"RUN_COMPLETED", "RUN_NOT_RUNNING"}:
                # The final status is checked by finish() through overview.
                return ToolResult(
                    action_kind=action.kind,
                    ok=True,
                    summary="The simulator reports that this run is no longer running.",
                    data=_safe(
                        {
                            "status_code": getattr(error, "status_code", None),
                            "code": code,
                            "message": getattr(error, "message", "Simulator request failed"),
                        }
                    ),
                    terminal=True,
                )
            return _error_result(action.kind, error)

    async def _execute(self, session: SimulatorV2Session, action: AgentAction) -> ToolResult:
        if isinstance(action, FinishRun):
            value = _safe(await self.client.overview(session.run_id))
            self._observe(session, value, update_status=True)
            availability = _required_mapping(value, "availability")
            costs = _required_mapping(value, "costs")
            terminal = value["status"] in {"completed", "failed"}
            remaining = _required_clock(value).get("remaining_seconds")
            if not terminal:
                return ToolResult(
                    action_kind=action.kind,
                    ok=False,
                    summary=(
                        "The run is still running; the full simulation horizon is required "
                        f"before finishing (remaining_seconds={remaining}). "
                        "SLO has not been decided yet."
                    ),
                    data=value,
                    objective_metrics=_objective_metrics(value, kind="get_overview"),
                    terminal=False,
                )
            return ToolResult(
                action_kind=action.kind,
                summary=(
                    f"The run is {value['status']}; finish accepted with "
                    f"uptime={availability.get('uptime_ratio')} and "
                    f"total_cost_minor={costs.get('total_cost_minor')}."
                ),
                data=value,
                objective_metrics=_objective_metrics(value, kind="get_overview"),
                terminal=True,
            )

        if isinstance(action, GetOverview):
            value = _safe(await self.client.overview(session.run_id))
            self._observe(session, value, update_status=True)
            availability = _required_mapping(value, "availability")
            costs = _required_mapping(value, "costs")
            return _result(
                action.kind,
                value,
                f"Site is {value.get('site_status', 'unknown')}; "
                f"uptime={availability.get('uptime_ratio', 'unknown')}; "
                f"total_cost_minor={costs.get('total_cost_minor', 'unknown')}.",
                terminal=_terminal_for(value, overview=True),
            )

        if isinstance(action, GetMetrics):
            value = _safe(await self.client.metrics(session.run_id))
            self._observe(session, value)
            current = _required_mapping(value, "current")
            return _result(
                action.kind,
                value,
                f"uptime={current.get('uptime_ratio', 'unknown')}; "
                f"downtime_seconds={current.get('downtime_seconds', 'unknown')}; "
                f"total_cost_minor={current.get('total_cost_minor', 'unknown')}.",
                terminal=_terminal_for(value),
            )

        if isinstance(action, GetLogs):
            return await self._get_logs(session, action)

        if isinstance(action, GetResources):
            value = _safe(await self.client.resources(session.run_id))
            self._observe(session, value)
            active_roles = _active_server_counts(value)
            return _result(
                action.kind,
                value,
                f"active_instances={value.get('active_instances', 'unknown')}; "
                f"active_backend_instances={active_roles['backend']}; "
                f"active_database_instances={active_roles['database']}; "
                f"capacity={value.get('total_capacity_units', 'unknown')}; "
                f"hourly_cost={value.get('total_cost_per_hour_minor', 'unknown')}.",
                terminal=_terminal_for(value),
            )

        if isinstance(action, GetOperation):
            value = _safe(await self.client.operation(session.run_id, action.operation_id))
            self._observe(session, value)
            operation_status = value.get("status", "unknown")
            if operation_status not in {"queued", "running", "succeeded", "failed"}:
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Operation response has invalid status"
                )
            ok = operation_status != "failed"
            return _result(
                action.kind,
                value,
                f"Operation {value.get('operation_id', action.operation_id)} "
                f"is {operation_status}.",
                ok=ok,
                terminal=_terminal_for(value),
                operation_relation="observed",
            )

        if isinstance(action, V2ProbePage):
            value = _safe(
                await self.client.probe(
                    session.run_id,
                    request_id=session.next_request_id("probe"),
                    page=action.page,
                    product_id=action.product_id,
                )
            )
            self._observe(session, value)
            logical_status = value.get("status")
            if logical_status not in {200, 403, 500, 503}:
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Probe response has invalid status"
                )
            # The endpoint returns HTTP 200 even when the simulated page is
            # 403/500/503.  Such a probe is an unsuccessful observation.
            ok = logical_status == 200 if isinstance(logical_status, int) else True
            return _result(
                action.kind,
                value,
                f"Probe {action.page} returned simulated HTTP {logical_status}.",
                ok=ok,
                terminal=_terminal_for(value),
            )

        if isinstance(action, (AdvanceTime, V2AdvanceTime)):
            if isinstance(action, V2AdvanceTime):
                stop_when = (
                    action.stop_when.model_dump(mode="json", exclude_none=True)
                    if action.stop_when is not None
                    else None
                )
            else:
                stop_when = {"new_log_errors": 1}
            value = _safe(
                await self.client.advance_time(
                    session.run_id,
                    request_id=session.next_request_id("advance"),
                    duration_seconds=action.duration_seconds,
                    stop_when=stop_when,
                )
            )
            self._observe(session, value)
            clock = value.get("clock") or {}
            actual = clock.get("applied_advance_seconds", value.get("applied_advance_seconds"))
            requested = value.get("requested_duration_seconds", action.duration_seconds)
            return _result(
                action.kind,
                value,
                f"Advanced {actual if actual is not None else 'unknown'}s "
                f"(requested {requested}s); "
                f"processed_events={value.get('processed_events', 'unknown')}; "
                f"new_logs={value.get('new_logs', 'unknown')}.",
                terminal=_terminal_for(value),
            )

        if isinstance(action, GetInbox):
            return await self._get_inbox(session, action)

        if isinstance(action, GetControlCommands):
            value = _safe(await self.client.list_commands(session.run_id))
            self._observe(session, value)
            commands = value.get("commands")
            count = len(commands) if isinstance(commands, list) else 0
            return _result(
                action.kind,
                value,
                f"Control panel catalog contains {count} commands.",
                terminal=_terminal_for(value),
            )

        if isinstance(action, ControlCommand):
            request = action.request
            command = request.command
            params_model = request.params
            params = params_model.model_dump(mode="json", exclude_none=True)
            value = _safe(
                await self.client.execute_command(
                    session.run_id,
                    request_id=session.next_request_id("command"),
                    command=command,
                    params=params,
                )
            )
            self._observe(session, value)
            accepted = isinstance(value.get("operation_id"), str)
            command_state = (
                f"accepted as operation {value['operation_id']}"
                if accepted
                else "completed synchronously"
            )
            return _result(
                action.kind,
                value,
                f"Command {command} {command_state}.",
                terminal=_terminal_for(value),
                operation_relation="initiated" if accepted else None,
            )

        # Legacy mutations have no v2 wire equivalent.  Returning a local
        # validation error is safer than accidentally sending a v1 request.
        return ToolResult(
            action_kind=action.kind,
            ok=False,
            summary=f"Action {type(action).__name__} is not supported by simulator v2.",
            data={"code": "UNSUPPORTED_V2_ACTION", "action_kind": action.kind},
        )

    async def _get_logs(self, session: SimulatorV2Session, action: GetLogs) -> ToolResult:
        status = action.status
        status_key = str(status)
        if session.logs_initial_from is None:
            session.logs_initial_from = session.logs_from
        from_time = session.logs_from_by_status.setdefault(status_key, session.logs_initial_from)
        cursor = session.logs_cursor_by_status.get(status_key)
        collected: list[dict[str, Any]] = []
        latest_clock: Mapping[str, Any] | None = None

        for _ in range(_MAX_PAGES_PER_READ):
            page = _safe(
                await self.client.logs(
                    session.run_id,
                    from_time=from_time.isoformat() if from_time else None,
                    to_time=None,
                    status=status,
                    cursor=cursor,
                    limit=100,
                )
            )
            clock = page.get("clock")
            if not isinstance(clock, Mapping):
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Logs response has invalid clock"
                )
            _required_clock(page)
            latest_clock = clock
            raw_logs = page.get("logs")
            if not isinstance(raw_logs, list) or any(
                not isinstance(item, dict) for item in raw_logs
            ):
                raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Logs response has invalid logs")
            for log in raw_logs:
                log_id = log.get("request_id")
                if not isinstance(log_id, str) or not log_id:
                    raise SimulatorV2ApiError(
                        200, "INVALID_RESPONSE", "Logs response has invalid request ID"
                    )
                if log_id not in session.seen_log_ids:
                    session.seen_log_ids.add(log_id)
                    collected.append(dict(log))
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Logs response has invalid next cursor"
                )
            cursor = next_cursor
            if cursor is None:
                break

        if latest_clock is not None:
            observed = _datetime(latest_clock.get("simulation_time"))
            if observed is None:
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Logs response has invalid clock"
                )
            session.simulation_time = observed
            if cursor is None:
                session.logs_from_by_status[status_key] = observed
                session.logs_from = observed

        session.logs_cursor = cursor
        session.logs_cursor_status = status if cursor is not None else None
        session.logs_cursor_by_status[status_key] = cursor
        error_counts: dict[str, int] = {}
        for item in collected:
            error = item.get("error")
            if isinstance(error, str) and error:
                error_counts[error] = error_counts.get(error, 0) + 1
        data = {
            "clock": dict(latest_clock) if latest_clock is not None else None,
            "total_logs": len(collected),
            "logs": collected,
            "truncated": cursor is not None,
        }
        if cursor is None:
            summary = (
                f"Read {len(collected)} new logs from a complete page; "
                f"errors={error_counts or 'none'}."
            )
        else:
            summary = (
                f"Read {len(collected)} new logs from the returned page; "
                f"errors={error_counts or 'none'}; page is truncated and unread logs remain."
            )
        return ToolResult(
            action_kind=action.kind,
            summary=summary,
            data=_safe(data),
            terminal=bool(latest_clock and _clock_terminal({"clock": latest_clock})),
        )

    async def _get_inbox(self, session: SimulatorV2Session, action: AgentAction) -> ToolResult:
        cursor = session.inbox_cursor
        collected: list[dict[str, Any]] = []
        latest_clock: Mapping[str, Any] | None = None
        for _ in range(_MAX_PAGES_PER_READ):
            page = _safe(
                await self.client.inbox(
                    session.run_id,
                    cursor=cursor,
                    limit=100,
                )
            )
            clock = page.get("clock")
            if not isinstance(clock, Mapping):
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Inbox response has invalid clock"
                )
            _required_clock(page)
            latest_clock = clock
            messages = page.get("messages")
            if not isinstance(messages, list) or any(
                not isinstance(item, dict) for item in messages
            ):
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Inbox response has invalid messages"
                )
            for message in messages:
                message_id = message.get("message_id")
                if not isinstance(message_id, str) or not message_id:
                    raise SimulatorV2ApiError(
                        200, "INVALID_RESPONSE", "Inbox response has invalid message ID"
                    )
                if message_id not in session.seen_inbox_ids:
                    session.seen_inbox_ids.add(message_id)
                    collected.append(dict(message))
            next_cursor = page.get("next_cursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Inbox response has invalid next cursor"
                )
            cursor = next_cursor
            if cursor is None:
                break

        if latest_clock is not None:
            observed = _datetime(latest_clock.get("simulation_time"))
            if observed is None:
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Inbox response has invalid clock"
                )
            session.simulation_time = observed
        session.inbox_cursor = cursor
        data = {
            "clock": dict(latest_clock) if latest_clock is not None else None,
            "messages": collected,
            "total_messages": len(collected),
            "truncated": cursor is not None,
        }
        subjects = [
            item.get("subject") for item in collected if isinstance(item.get("subject"), str)
        ]
        return ToolResult(
            action_kind=action.kind,
            summary=(
                f"Read {len(collected)} new inbox messages" + (f": {subjects}" if subjects else ".")
            ),
            data=_safe(data),
            terminal=bool(latest_clock and _clock_terminal({"clock": latest_clock})),
        )

    @staticmethod
    def _observe(
        session: SimulatorV2Session,
        data: Mapping[str, Any],
        *,
        update_status: bool = False,
    ) -> None:
        clock = _required_clock(data)
        simulation_time = _datetime(clock.get("simulation_time"))
        if simulation_time is None:
            raise SimulatorV2ApiError(
                200, "INVALID_RESPONSE", "Simulator response has invalid clock"
            )
        session.simulation_time = simulation_time
        if update_status:
            status = data.get("status")
            if status not in {"running", "completed", "failed"}:
                raise SimulatorV2ApiError(
                    200, "INVALID_RESPONSE", "Simulator response has invalid status"
                )
            session.status = status

    async def finish(
        self,
        session: SimulatorV2Session,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RunResult:
        overview = _safe(await self.client.overview(session.run_id))
        _required_clock(overview)
        status = overview.get("status")
        if status not in {"running", "completed", "failed"}:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Overview has invalid status")
        availability = _required_mapping(overview, "availability")
        costs = _required_mapping(overview, "costs")
        uptime_ratio = availability.get("uptime_ratio")
        if uptime_ratio is not None and (
            not isinstance(uptime_ratio, (int, float))
            or isinstance(uptime_ratio, bool)
            or not 0 <= uptime_ratio <= 1
        ):
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Overview has invalid uptime ratio")
        slo_passed = availability.get("slo_passed")
        if slo_passed is not None and not isinstance(slo_passed, bool):
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Overview has invalid SLO status")
        total_cost = costs.get("total_cost_minor")
        server_cost = costs.get("server_cost_minor")
        if not isinstance(total_cost, int) or isinstance(total_cost, bool) or total_cost < 0:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Overview has invalid total cost")
        if not isinstance(server_cost, int) or isinstance(server_cost, bool) or server_cost < 0:
            raise SimulatorV2ApiError(200, "INVALID_RESPONSE", "Overview has invalid server cost")
        metrics = _objective_metrics(overview, kind="get_overview")

        fields: dict[str, Any] = {
            "run_id": session.run_id,
            "seed": session.seed,
            "agent_id": session.agent_id,
            "agent_version": session.agent_version,
            "status": status,
            "steps": steps,
            "duration_seconds": duration_seconds,
            "objective_kind": "uptime_cost",
            "server_cost_minor": server_cost,
            "uptime_ratio": uptime_ratio,
            "slo_passed": slo_passed,
            "total_cost_minor": total_cost,
            "objective_metrics": metrics,
            "stop_reason": stop_reason,
        }
        return RunResult(**fields)
