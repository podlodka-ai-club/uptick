from __future__ import annotations

from uptick_agent.core.models import AgentContext, JsonObject, JsonValue, PolicyResult, SGRDecision

FORBIDDEN_GENERIC_CAPABILITIES = frozenset(
    {
        "shell",
        "run_shell",
        "execute_command",
        "http_request",
        "fetch_url",
        "open_url",
    }
)


class DecisionPolicy:
    """Small, provider-independent validation policy for one capability call."""

    def validate(self, context: AgentContext, decision: SGRDecision) -> PolicyResult:
        call = decision.envelope.selected_action
        violations: list[str] = []
        capability = context.capabilities.find(call.name)

        if call.name in FORBIDDEN_GENERIC_CAPABILITIES:
            violations.append(f"capability {call.name!r} is explicitly forbidden")
        if call.name in context.constraints.forbidden_capabilities:
            violations.append(f"capability {call.name!r} is forbidden by run constraints")
        if capability is None:
            violations.append(f"unknown capability {call.name!r}")
        else:
            violations.extend(
                validate_json_schema(call.arguments, capability.input_schema, "arguments")
            )
            if decision.envelope.task_completed != capability.terminal:
                violations.append(
                    "task_completed must be true exactly when the selected capability is terminal"
                )

        if context.environment_state.status != "active":
            violations.append("terminal environment state does not accept another action")
        progress = context.progress
        if (
            progress is not None
            and progress.step_limit is not None
            and progress.step > progress.step_limit
        ):
            violations.append("step limit is exhausted")

        return PolicyResult(accepted=not violations, violations=violations)


def validate_json_schema(value: JsonValue, schema: JsonObject, path: str) -> list[str]:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branches = [branch for branch in any_of if isinstance(branch, dict)]
        if branches and not any(
            not validate_json_schema(value, branch, path) for branch in branches
        ):
            return [f"{path} does not match any allowed schema"]
        return []

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return [f"{path} must be one of {enum!r}"]

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path} must be an object"]
        errors: list[str] = []
        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required_names = required if isinstance(required, list) else []
        for name in required_names:
            if isinstance(name, str) and name not in value:
                errors.append(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in property_schemas:
                    errors.append(f"{path}.{name} is not allowed")
        for name, child in value.items():
            child_schema = property_schemas.get(name)
            if isinstance(child_schema, dict):
                errors.extend(validate_json_schema(child, child_schema, f"{path}.{name}"))
        return errors
    if expected == "array":
        if not isinstance(value, list):
            return [f"{path} must be an array"]
        items = schema.get("items")
        if isinstance(items, dict):
            return [
                error
                for index, item in enumerate(value)
                for error in validate_json_schema(item, items, f"{path}[{index}]")
            ]
        return []
    if expected == "string":
        if not isinstance(value, str):
            return [f"{path} must be a string"]
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        errors = []
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path} is shorter than {minimum}")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path} is longer than {maximum}")
        return errors
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path} must be an integer"]
        return _validate_number_bounds(value, schema, path)
    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{path} must be a number"]
        return _validate_number_bounds(value, schema, path)
    if expected == "boolean" and not isinstance(value, bool):
        return [f"{path} must be a boolean"]
    if expected == "null" and value is not None:
        return [f"{path} must be null"]
    return []


def _validate_number_bounds(value: int | float, schema: JsonObject, path: str) -> list[str]:
    errors: list[str] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, int | float) and value < minimum:
        errors.append(f"{path} must be at least {minimum}")
    if isinstance(maximum, int | float) and value > maximum:
        errors.append(f"{path} must be at most {maximum}")
    return errors
