#!/usr/bin/env python3
"""Adapter for the Pantry training world."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

ROOT = Path.cwd()
BOOTSTRAP_PATH = ROOT / "bootstrap.json"
STATE_PATH = ROOT / ".adapter-state.json"
DEFAULT_ORIGIN = "http://127.0.0.1:8097"
MAX_RESPONSE_BYTES = 1_000_000
READ_CHUNK_BYTES = 16_384
TIMEOUT_SECONDS = 10


def output(
    *,
    data: Any = None,
    evidence: list[dict[str, Any]] | None = None,
    done: bool = False,
    success: bool | None = None,
    metrics: dict[str, Any] | None = None,
    external_id: str | None = None,
    pending: bool = False,
    error: Any = None,
) -> None:
    document = {
        "data": {} if data is None else data,
        "evidence": [] if evidence is None else evidence,
        "done": done,
        "success": success,
        "metrics": {} if metrics is None else metrics,
        "external_id": external_id,
        "pending": pending,
        "error": error,
    }
    sys.stdout.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.flush()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def load_local_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"mutations": {}}
    state = read_json(STATE_PATH)
    if not isinstance(state.get("mutations"), dict):
        state["mutations"] = {}
    return state


def save_local_state(state: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=".adapter-state-", suffix=".json", dir=ROOT
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, STATE_PATH)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def bootstrap_parts(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = document.get("bootstrap")
    if not isinstance(payload, dict):
        payload = document
    world = document.get("world")
    if not isinstance(world, dict):
        world = {}
    return payload, world


def resolve_origin(request: dict[str, Any], world: dict[str, Any]) -> str:
    options = request.get("options")
    if isinstance(options, dict) and isinstance(options.get("origin"), str):
        origin = options["origin"]
    elif os.environ.get("AK_ORIGIN"):
        origin = os.environ["AK_ORIGIN"]
    else:
        defaults = world.get("defaults")
        origin = defaults.get("origin") if isinstance(defaults, dict) else None
        if not isinstance(origin, str):
            origin = DEFAULT_ORIGIN
    return origin.rstrip("/")


def read_json_response(stream: BinaryIO) -> dict[str, Any]:
    """Read one bounded JSON object without waiting for connection EOF."""
    read1 = getattr(stream, "read1", None)
    if not callable(read1):
        raise RuntimeError("HTTP response stream does not support read1()")

    raw = bytearray()
    while True:
        remaining = MAX_RESPONSE_BYTES - len(raw)
        chunk = read1(min(READ_CHUNK_BYTES, remaining + 1))
        if chunk:
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("HTTP response exceeded size limit")

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason == "unexpected end of data":
                    continue
                raise RuntimeError("server returned invalid UTF-8") from exc

            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                raise RuntimeError("server JSON response is not an object")
            return value

        break

    if not raw:
        raise RuntimeError("server returned an empty response")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("server returned incomplete or invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("server JSON response is not an object")
    return value


def http_json(
    method: str, url: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    encoded = None
    headers = {"Accept": "application/json"}
    if body is not None:
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url, data=encoded, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            if "json" not in content_type.lower():
                raise RuntimeError(
                    f"expected JSON response, got Content-Type {content_type!r}"
                )
            return read_json_response(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.dumps(
                read_json_response(exc),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except RuntimeError:
            detail = "remote request failed"
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"transport error: {exc}") from exc


def extract_state(response: dict[str, Any]) -> dict[str, Any]:
    state = response.get("state")
    if isinstance(state, dict):
        return state
    return response


def state_metrics(state: dict[str, Any]) -> dict[str, Any]:
    names = ("health", "cycles_completed", "actions")
    return {name: state[name] for name in names if name in state}


def completion_evidence(
    state: dict[str, Any], run_id: str
) -> list[dict[str, Any]]:
    if state.get("done") is not True:
        return []
    success = state.get("success")
    return [
        {
            "identity": f"pantry-run:{run_id}:completion",
            "outcome": "success" if success is True else "failure",
            "kind": "completion",
            "detail": "World reported the run complete with success=true."
            if success is True
            else "World reported the run complete with success=false.",
        }
    ]


def event_evidence(response: dict[str, Any]) -> list[dict[str, Any]]:
    event = response.get("event")
    if not isinstance(event, dict):
        return []
    identity = event.get("identity")
    outcome = event.get("outcome")
    if not isinstance(identity, str) or not identity:
        return []
    if outcome not in ("success", "failure"):
        return []
    detail = event.get("description")
    if not isinstance(detail, str):
        detail = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return [
        {
            "identity": identity,
            "outcome": outcome,
            "kind": "action",
            "detail": detail,
        }
    ]


def result_from_response(
    response: dict[str, Any], run_id: str, include_event: bool
) -> dict[str, Any]:
    state = extract_state(response)
    done = state.get("done") is True
    success = (
        state.get("success")
        if done and isinstance(state.get("success"), bool)
        else None
    )
    evidence = event_evidence(response) if include_event else []
    if done:
        known = {item["identity"] for item in evidence}
        evidence.extend(
            item
            for item in completion_evidence(state, run_id)
            if item["identity"] not in known
        )
    return {
        "data": {
            "state": state,
            "actions": {
                "cook": "Prepare a meal.",
                "eat": "Attempt to consume a prepared meal.",
                "wait": "Advance with no cooking or eating action.",
            },
            "action_schema": {"type": "cook|eat|wait"},
        },
        "evidence": evidence,
        "done": done,
        "success": success,
        "metrics": state_metrics(state),
        "external_id": run_id,
        "pending": False,
        "error": None,
    }


def main() -> None:
    try:
        incoming = json.load(sys.stdin)
        if not isinstance(incoming, dict):
            raise ValueError("stdin must contain a JSON object")

        bootstrap_document = read_json(BOOTSTRAP_PATH)
        bootstrap, world = bootstrap_parts(bootstrap_document)
        initial_state = bootstrap.get("state")
        run_id = bootstrap.get("run_id")
        if not isinstance(run_id, str) and isinstance(initial_state, dict):
            run_id = initial_state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("bootstrap.json does not contain run_id")

        origin = resolve_origin(incoming, world)
        operation = incoming.get("operation")

        if operation == "observe":
            response = http_json("GET", f"{origin}/runs/{run_id}")
            output(**result_from_response(response, run_id, include_event=False))
            return

        if operation != "act":
            output(
                error={
                    "type": "invalid_operation",
                    "message": "operation must be observe or act",
                }
            )
            return

        action_object = incoming.get("action")
        if not isinstance(action_object, dict):
            output(
                error={
                    "type": "invalid_action",
                    "message": "action must be an object",
                }
            )
            return
        action_type = action_object.get("type")
        if action_type not in ("cook", "eat", "wait"):
            output(
                error={
                    "type": "invalid_action",
                    "message": "action.type must be cook, eat, or wait",
                }
            )
            return

        request_id = incoming.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            output(
                error={
                    "type": "invalid_request_id",
                    "message": "request_id must be a non-empty string",
                }
            )
            return

        mutation_body = {"request_id": request_id, "action": action_type}
        local_state = load_local_state()
        mutations = local_state["mutations"]
        recorded = mutations.get(request_id)
        if isinstance(recorded, dict):
            if recorded.get("body") != mutation_body:
                output(
                    error={
                        "type": "idempotency_conflict",
                        "message": "this request_id was already used with a different action body",
                    }
                )
                return
            cached_response = recorded.get("response")
            if isinstance(cached_response, dict):
                output(
                    **result_from_response(
                        cached_response, run_id, include_event=True
                    )
                )
                return
        else:
            mutations[request_id] = {"body": mutation_body}
            save_local_state(local_state)

        response = http_json(
            "POST", f"{origin}/runs/{run_id}/act", mutation_body
        )
        mutations[request_id]["response"] = response
        save_local_state(local_state)
        output(**result_from_response(response, run_id, include_event=True))
    except Exception as exc:
        output(error={"type": "adapter_error", "message": str(exc)})


if __name__ == "__main__":
    main()
