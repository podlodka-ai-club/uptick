from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from uptick_agent.config.models import AgentConfig, SQLiteMemoryConfig

_PROJECT_DIRECTORY = Path(__file__).resolve().parents[3]


class AgentConfigError(ValueError):
    """Sanitized configuration error safe for CLI display."""


@dataclass(frozen=True, slots=True)
class LoadedAgentConfig:
    config: AgentConfig
    source_id: str
    source_sha256: str
    source_path: Path = field(repr=False)
    operator_guidance: str | None = field(default=None, repr=False)

    def canonical_redacted_json(self) -> str:
        return json.dumps(
            self.config.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def load_agent_config(path: str | Path) -> LoadedAgentConfig:
    source_path = Path(path).resolve()
    source_id = _portable_source_id(source_path)
    try:
        source = source_path.read_bytes()
    except OSError:
        raise AgentConfigError(f"{source_id}: cannot read agent configuration") from None

    try:
        raw = yaml.safe_load(source)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise AgentConfigError(f"{source_id}: invalid YAML{location}") from None
    except yaml.YAMLError:
        raise AgentConfigError(f"{source_id}: invalid YAML") from None

    if not isinstance(raw, dict):
        raise AgentConfigError(f"{source_id}: configuration root must be a mapping")
    try:
        config = AgentConfig.model_validate(raw)
    except ValidationError as error:
        raise AgentConfigError(_format_validation_error(source_id, error)) from None

    config = _resolve_paths(config, source_path.parent)
    return LoadedAgentConfig(
        config=config,
        source_id=source_id,
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_path=source_path,
        operator_guidance=_load_operator_guidance(config.environment.prompt_file, source_id),
    )


def _load_operator_guidance(path: Path | None, source_id: str) -> str | None:
    if path is None:
        return None
    try:
        text = path.read_bytes().decode("utf-8")
    except OSError:
        raise AgentConfigError(f"{source_id}: environment.prompt_file: cannot read file") from None
    except UnicodeDecodeError:
        raise AgentConfigError(
            f"{source_id}: environment.prompt_file: must contain valid UTF-8"
        ) from None
    if not text.strip():
        raise AgentConfigError(f"{source_id}: environment.prompt_file: must not be empty")
    return text


def _resolve_paths(config: AgentConfig, base_directory: Path) -> AgentConfig:
    memory = config.memory
    if isinstance(memory, SQLiteMemoryConfig):
        memory = memory.model_copy(update={"path": _resolve_path(memory.path, base_directory)})
    run_store = config.run_store.model_copy(
        update={"path": _resolve_path(config.run_store.path, base_directory)}
    )
    environment = config.environment
    if environment.prompt_file is not None:
        environment = environment.model_copy(
            update={"prompt_file": _resolve_path(environment.prompt_file, base_directory)}
        )
    return config.model_copy(
        update={"memory": memory, "run_store": run_store, "environment": environment}
    )


def _resolve_path(path: Path, base_directory: Path) -> Path:
    return path.resolve() if path.is_absolute() else (base_directory / path).resolve()


def _portable_source_id(source_path: Path) -> str:
    try:
        return source_path.relative_to(_PROJECT_DIRECTORY).as_posix()
    except ValueError:
        return f"external/{source_path.name}"


def _format_validation_error(source_id: str, error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        parts = cast(tuple[object, ...], item["loc"])
        location = ".".join(str(part) for part in parts)
        details.append(f"{location or '<root>'}: {_safe_error_message(str(item['type']), parts)}")
    return f"{source_id}: invalid agent configuration: " + "; ".join(details)


def _safe_error_message(error_type: str, location: tuple[object, ...]) -> str:
    field = str(location[-1]) if location else ""
    if error_type == "value_error" and not location:
        return "learner and learning configuration are inconsistent"
    if error_type == "value_error" and field in {"endpoint", "base_url"}:
        return "must be an absolute HTTP(S) URL without userinfo, query, or fragment"
    if error_type == "value_error" and field == "seed":
        return "must be a non-zero integer"
    messages = {
        "missing": "field is required",
        "extra_forbidden": "unknown field is not permitted",
        "literal_error": "invalid literal value",
        "union_tag_invalid": "invalid discriminator value",
        "union_tag_not_found": "discriminator field is required",
        "string_pattern_mismatch": "value does not match the required pattern",
        "string_too_short": "value is too short",
        "greater_than": "value must be greater than zero",
        "greater_than_equal": "value must be nonnegative",
        "int_type": "value must be an integer",
        "string_type": "value must be a string",
        "value_error": "value failed validation",
    }
    return messages.get(error_type, error_type.replace("_", " "))
