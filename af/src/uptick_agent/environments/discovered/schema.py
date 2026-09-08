from __future__ import annotations

import copy
import re
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

from uptick_agent.core.models import JsonObject
from uptick_agent.environments.discovered.models import HttpTool


def safe_path(path: str) -> str:
    parsed = urlsplit(path)
    decoded = unquote(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(ord(c) < 32 for c in decoded)
    ):
        raise ValueError("discovery requires an absolute path on the configured origin")
    return path


def resolve_refs(value: Any, root: dict, seen: tuple[str, ...] = ()) -> Any:
    if len(seen) > 32:
        raise ValueError("API schema reference depth exceeded")
    if isinstance(value, list):
        return [resolve_refs(item, root, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            raise ValueError("only acyclic local schema references are supported")
        target = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        merged = resolve_refs(target, root, (*seen, ref))
        return merged | {k: resolve_refs(v, root, seen) for k, v in value.items() if k != "$ref"}
    return {k: resolve_refs(v, root, seen) for k, v in value.items()}


def validate(value: Any, schema: JsonObject) -> None:
    Draft202012Validator.check_schema(schema)
    error = next(iter(Draft202012Validator(schema).iter_errors(value)), None)
    if error is not None:
        # Do not include the rejected value, which might contain an attempted secret.
        raise ValueError(
            f"invalid arguments at {'/'.join(map(str, error.path))}: {error.validator}"
        )


def model_schema(schema: JsonObject) -> JsonObject:
    """Represent optional fields as null in strict structured-output providers."""
    result = copy.deepcopy(schema)
    # Structured Outputs is a subset, not the transport validator. Keep every original
    # constraint in HttpTool.schema/body_schema for validation immediately before I/O.
    for keyword in (
        "$schema",
        "$id",
        "$defs",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "dependentRequired",
        "dependentSchemas",
        "uniqueItems",
    ):
        result.pop(keyword, None)
    properties = result.get("properties")
    if isinstance(properties, dict):
        required = cast(list, result.get("required", []))
        for name, child in properties.items():
            if isinstance(child, dict):
                normalized = model_schema(child)
                properties[name] = (
                    normalized if name in required else {"anyOf": [normalized, {"type": "null"}]}
                )
    items = result.get("items")
    if isinstance(items, dict):
        result["items"] = model_schema(items)
    for key in ("anyOf", "oneOf", "allOf"):
        branches = result.get(key)
        if isinstance(branches, list):
            result[key] = [model_schema(b) if isinstance(b, dict) else b for b in branches]
    return result


def wire_arguments(value: Any, schema: JsonObject) -> Any:
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        assert isinstance(properties, dict)
        required = schema.get("required", [])
        return {
            k: wire_arguments(v, cast(JsonObject, properties.get(k, {})))
            for k, v in value.items()
            if v is not None or k in required
        }
    if isinstance(value, list):
        return [wire_arguments(v, cast(JsonObject, schema.get("items", {}))) for v in value]
    return value


def compile_tools(document: dict, catalog: dict | None, catalog_path: str | None) -> list[HttpTool]:
    """Compile only published operations; no endpoint names or action union are embedded."""
    result: list[HttpTool] = []
    for path, path_item in document.get("paths", {}).items():
        safe_path(path)
        if "{run_id}" not in path:
            continue  # lifecycle and APIs outside the current session are not agent tools
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path == catalog_path and method == "get":
                continue
            security = operation.get("security", document.get("security", []))
            panel_auth = bool(security)
            for requirement in security:
                for name in requirement:
                    scheme = document["components"]["securitySchemes"][name]
                    if scheme.get("type") != "http" or scheme.get("scheme") != "basic":
                        raise ValueError("unsupported API authentication scheme")
            if path == catalog_path and method == "post" and catalog is not None:
                for item in catalog["commands"]:
                    schema = resolve_refs(item["params_schema"], item["params_schema"])
                    result.append(
                        HttpTool(
                            name=item["command"],
                            description=item["description"],
                            method="POST",
                            path=path,
                            schema=schema,
                            panel_auth=panel_auth,
                            command=item["command"],
                            target_auth=item["target_auth_required"],
                            result_schema=resolve_refs(
                                item["result_schema"], item["result_schema"]
                            ),
                            asynchronous=item["execution"] == "asynchronous",
                        )
                    )
                continue
            name = operation.get("operationId")
            if not isinstance(name, str):
                raise ValueError("operationId is required for discoverable tools")
            properties: dict = {}
            required: list[str] = []
            parameters: list[JsonObject] = []
            for raw in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
                parameter = resolve_refs(raw, document)
                if parameter["name"] == "run_id" and parameter["in"] == "path":
                    continue
                if parameter["in"] not in {"path", "query"}:
                    raise ValueError("unsupported parameter location")
                parameters.append(parameter)
                properties[parameter["name"]] = parameter["schema"]
                if parameter.get("required"):
                    required.append(parameter["name"])
            body_schema = None
            if "requestBody" in operation:
                body = resolve_refs(operation["requestBody"], document)
                body_schema = body["content"]["application/json"]["schema"]
                if body_schema.get("type") != "object":
                    raise ValueError("only JSON object request bodies are supported")
                for key, value in body_schema.get("properties", {}).items():
                    if key == "request_id":
                        continue
                    if key in properties:
                        raise ValueError("body and URL parameter names collide")
                    properties[key] = value
                required.extend(k for k in body_schema.get("required", []) if k != "request_id")
            result_schema = {}
            responses = operation.get("responses", {})
            for status in ("200", "201", "202"):
                if status in responses:
                    response = resolve_refs(responses[status], document)
                    result_schema = (
                        response.get("content", {}).get("application/json", {}).get("schema", {})
                    )
                    break
            result.append(
                HttpTool(
                    name=name,
                    description=operation.get("summary", name)
                    + "\n"
                    + operation.get("description", ""),
                    method=method.upper(),
                    path=path,
                    schema=cast(
                        JsonObject,
                        {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                    ),
                    parameters=parameters,
                    body_schema=body_schema,
                    panel_auth=panel_auth,
                    result_schema=result_schema,
                )
            )
    if not result or len(result) > 128 or len({t.name for t in result}) != len(result):
        raise ValueError("invalid or duplicate discovered tools")
    for tool in result:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,100}", tool.name):
            raise ValueError("invalid discovered tool name")
        Draft202012Validator.check_schema(tool.schema)
    return result
