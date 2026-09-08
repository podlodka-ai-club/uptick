import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from uptick_agent import composition
from uptick_agent.config import AgentConfigError, SQLiteMemoryConfig, load_agent_config
from uptick_agent.config.loader import _PROJECT_DIRECTORY, _portable_source_id
from uptick_agent.core.models import DEFAULT_OBJECTIVE, RunSpec
from uptick_agent.core.sgr import DEFAULT_SYSTEM_PROMPT, CurrentSGR
from uptick_agent.memory import NoMemory, SQLiteMemory
from uptick_agent.store import JsonlRunStore


def _codex_yaml(*, memory: str = "  backend: none", endpoint: str = "http://localhost:8080") -> str:
    return f"""\
schema_version: 2
agent:
  id: af-sgr
  version: af-sgr-v2-0.1
reasoners:
  decision:
    provider: codex
    model: gpt-test
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
memory:
{memory}
run_store:
  backend: jsonl
  path: .agent/runs
environment:
  adapter: uptickv2
  endpoint: {endpoint}
  seed: 1
"""


def _openai_yaml(*, api_key: str = "    api_key:\n      from_env: OPENAI_API_KEY") -> str:
    return f"""\
schema_version: 2
agent:
  id: af-sgr
  version: af-sgr-v2-0.1
reasoners:
  decision:
    provider: openai
    model: gpt-test
    effort: null
    thread_mode: stateless
    timeout_seconds: 600
    retries: 2
{api_key}
    base_url: https://api.openai.com/v1
memory:
  backend: none
run_store:
  backend: jsonl
  path: .agent/runs
environment:
  adapter: uptickv2
  endpoint: http://localhost:8080
  seed: 1
"""


def _write(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


def test_ad_hoc_objective_defers_to_the_published_environment_goal(tmp_path: Path) -> None:
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", _codex_yaml()))
    spec = composition._ad_hoc_run_spec(loaded, seed=1)
    assert spec.objective == DEFAULT_OBJECTIVE
    assert RunSpec(run_id="run-generic", environment="other").objective == DEFAULT_OBJECTIVE


def test_removed_uptick_adapter_is_rejected(tmp_path: Path) -> None:
    source = _codex_yaml().replace("adapter: uptickv2", "adapter: uptick")
    with pytest.raises(AgentConfigError, match="environment.adapter"):
        load_agent_config(_write(tmp_path / "agent.yaml", source))


@pytest.mark.parametrize(
    "setting,expected", [("", None), ("  step_limit: null\n", None), ("  step_limit: 7\n", 7)]
)
def test_uptickv2_runs_without_a_step_limit_unless_explicitly_configured(
    tmp_path: Path, setting: str, expected: int | None
) -> None:
    source = _codex_yaml() + setting
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", source))

    spec = composition._ad_hoc_run_spec(loaded, seed=11)

    assert spec.step_limit == expected


def test_load_valid_yaml_resolves_paths_from_config_location_and_hashes_source(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "nested"
    directory.mkdir()
    source = _codex_yaml(memory="  backend: sqlite\n  path: state/memory.sqlite")
    path = _write(directory / "agent.yaml", source)

    loaded = load_agent_config(path)

    assert loaded.config.schema_version == 2
    assert loaded.config.agent.id == "af-sgr"
    assert loaded.config.run_store.path == directory / ".agent" / "runs"
    assert isinstance(loaded.config.memory, SQLiteMemoryConfig)
    assert loaded.config.memory.path == directory / "state" / "memory.sqlite"
    assert loaded.source_id == "external/agent.yaml"
    assert loaded.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert str(path.resolve()) not in repr(loaded)
    assert str(path.resolve()) not in loaded.canonical_redacted_json()


def test_portable_source_id_is_project_relative_or_external() -> None:
    assert _portable_source_id(_PROJECT_DIRECTORY / "configs" / "agent.yaml") == (
        "configs/agent.yaml"
    )
    assert _portable_source_id(Path("/outside/private/agent.yaml")) == "external/agent.yaml"


@pytest.mark.parametrize(
    "source,field",
    [
        (_codex_yaml().replace("schema_version: 2", "schema_version: 1"), "schema_version"),
        (_codex_yaml().replace("  id: af-sgr\n", ""), "agent.id"),
        (
            _codex_yaml().replace("    provider: codex", "    provider: unknown"),
            "reasoners.decision",
        ),
        (_codex_yaml().replace("    retries: 1", "    retries: 1\n    extra: marker"), "extra"),
        (
            _codex_yaml().replace("    thread_mode: ephemeral", "    thread_mode: stateless"),
            "thread_mode",
        ),
    ],
)
def test_schema_rejects_unsupported_missing_unknown_and_provider_specific_fields(
    tmp_path: Path, source: str, field: str
) -> None:
    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert field in str(error.value)
    assert "marker" not in str(error.value)


@pytest.mark.parametrize("seed", ["0", "true", '"1"'])
def test_uptick_seed_is_strict_nonzero_integer(tmp_path: Path, seed: str) -> None:
    source = _codex_yaml().replace("  seed: 1", f"  seed: {seed}")

    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert "environment.seed" in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com",
        "https://marker-secret@example.com",
        "https://example.com/?token=marker-secret",
        "https://example.com/#marker-secret",
    ],
)
def test_urls_reject_unsafe_shapes_without_echoing_values(tmp_path: Path, url: str) -> None:
    source = _codex_yaml(endpoint=url)

    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert "environment.endpoint" in str(error.value)
    assert "marker-secret" not in str(error.value)


def test_openai_base_url_uses_the_same_safe_url_policy(tmp_path: Path) -> None:
    source = _openai_yaml().replace(
        "https://api.openai.com/v1", "https://marker-secret@example.com/v1"
    )

    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert "base_url" in str(error.value)
    assert "marker-secret" not in str(error.value)


@pytest.mark.parametrize(
    "source",
    [
        "schema_version: !unsafe 1\n",
        _codex_yaml() + "---\n{}\n",
        "- not\n- a\n- mapping\n",
    ],
)
def test_loader_rejects_custom_tags_multiple_documents_and_non_mapping_roots(
    tmp_path: Path, source: str
) -> None:
    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert "invalid YAML" in str(error.value) or "root must be a mapping" in str(error.value)


def test_secret_ref_is_typed_and_secret_value_never_enters_config_state(
    monkeypatch, tmp_path: Path
) -> None:
    marker = "marker-secret-value"
    path = _write(tmp_path / "agent.yaml", _openai_yaml())
    loaded = load_agent_config(path)
    captured: list[str] = []

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs["api_key"])
            self.chat = SimpleNamespace(completions=SimpleNamespace())

        async def close(self) -> None:
            return None

    monkeypatch.setenv("OPENAI_API_KEY", marker)
    monkeypatch.setattr("uptick_agent.reasoners.openai.AsyncOpenAI", FakeOpenAI)

    reasoner = composition._build_reasoner(
        loaded,
        composition.resolve_reasoner_config(loaded),
    )

    assert captured == [marker]
    assert marker not in repr(loaded)
    assert marker not in loaded.config.model_dump_json()
    assert marker not in loaded.canonical_redacted_json()
    assert marker not in loaded.source_sha256
    asyncio.run(reasoner.aclose())


def test_uptick_participant_token_uses_secret_ref(monkeypatch, tmp_path: Path) -> None:
    marker = "test-participant-secret"
    monkeypatch.setenv("UPTICK_PARTICIPANT_TOKEN", marker)
    source = _codex_yaml()
    source += "  participant_token:\n    from_env: UPTICK_PARTICIPANT_TOKEN\n"
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", source))
    assert loaded.config.environment.adapter == "uptickv2"
    assert loaded.config.environment.participant_token is not None
    assert loaded.config.environment.participant_token.from_env == "UPTICK_PARTICIPANT_TOKEN"
    assert marker not in loaded.canonical_redacted_json()
    assert marker not in repr(loaded)
    invalid = source.replace(
        "participant_token:\n    from_env: UPTICK_PARTICIPANT_TOKEN", "participant_token: " + marker
    )
    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "inline.yaml", invalid))
    assert marker not in str(error.value)


def test_composition_passes_participant_secret_reference_to_v2_adapter(
    monkeypatch, tmp_path: Path
) -> None:
    source = _codex_yaml()
    source += "  participant_token:\n    from_env: UPTICK_PARTICIPANT_TOKEN\n"
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", source))
    captured = {}

    class FakeReasoner:
        async def aclose(self):
            return None

    class FakeEnvironment:
        def __init__(self, endpoint, **kwargs):
            captured.update(kwargs)

        async def aclose(self):
            return None

    monkeypatch.setattr(composition, "_build_reasoner", lambda *args: FakeReasoner())
    monkeypatch.setattr(composition, "UptickV2Environment", FakeEnvironment)
    application = composition.build_application(loaded, show_progress=False)
    assert captured["participant_token_env"] == "UPTICK_PARTICIPANT_TOKEN"
    asyncio.run(application.aclose())


def test_missing_openai_secret_fails_before_other_adapter_construction(
    monkeypatch, tmp_path: Path
) -> None:
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", _openai_yaml()))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("another adapter was constructed before secret validation")

    monkeypatch.setattr(composition, "JsonlRunStore", forbidden)
    monkeypatch.setattr(composition, "UptickV2Environment", forbidden)

    with pytest.raises(ValueError, match="OPENAI_API_KEY") as error:
        composition.build_application(loaded)

    assert "marker-secret" not in str(error.value)


def test_openai_sdk_initialization_error_cannot_echo_secret(monkeypatch, tmp_path: Path) -> None:
    marker = "marker-secret-value"
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", _openai_yaml()))

    class FailingOpenAI:
        def __init__(self, **kwargs) -> None:
            raise ValueError(f"SDK rejected {kwargs['api_key']}")

    monkeypatch.setenv("OPENAI_API_KEY", marker)
    monkeypatch.setattr("uptick_agent.reasoners.openai.AsyncOpenAI", FailingOpenAI)

    with pytest.raises(RuntimeError, match="initialization failed") as error:
        composition._build_reasoner(loaded, composition.resolve_reasoner_config(loaded))

    assert marker not in str(error.value)


def test_application_uses_configured_endpoint_and_universal_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    source = _codex_yaml()
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", source))
    endpoints: list[str] = []
    prompts: list[str] = []

    def make_sgr(**kwargs):
        sgr = CurrentSGR(**kwargs)
        prompts.append(sgr.system_prompt)
        return sgr

    class FakeReasoner:
        async def aclose(self) -> None:
            return None

    class FakeEnvironment:
        def __init__(self, endpoint: str, **kwargs) -> None:
            endpoints.append(endpoint)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(composition, "_build_reasoner", lambda *args: FakeReasoner())
    monkeypatch.setattr(composition, "UptickV2Environment", FakeEnvironment)
    monkeypatch.setattr(composition, "CurrentSGR", make_sgr)

    application = composition.build_application(loaded, show_progress=False)

    assert isinstance(application.environment, composition.ProgrammableLauncher)
    assert prompts == [DEFAULT_SYSTEM_PROMPT]
    assert isinstance(application.run_store, JsonlRunStore)
    assert application.run_store.path == tmp_path / ".agent" / "runs" / "trace.jsonl"
    assert application.run_store.manifest_directory == tmp_path / ".agent" / "runs" / "manifests"
    assert endpoints == ["http://localhost:8080"]
    asyncio.run(application.aclose())


def test_mistaken_inline_secret_is_not_echoed_in_validation_error(tmp_path: Path) -> None:
    source = _openai_yaml(api_key="    api_key: marker-secret")

    with pytest.raises(AgentConfigError) as error:
        load_agent_config(_write(tmp_path / "agent.yaml", source))

    assert "api_key" in str(error.value)
    assert "marker-secret" not in str(error.value)


def test_loaded_config_is_immutable_and_builds_no_memory(tmp_path: Path) -> None:
    source = _codex_yaml()
    loaded = load_agent_config(_write(tmp_path / "agent.yaml", source))

    with pytest.raises(ValidationError):
        loaded.config.environment.seed = 2  # type: ignore[misc]
    memory = composition.build_memory(loaded)
    assert isinstance(memory, NoMemory)


def test_config_v2_requires_learner_exactly_when_learning_is_enabled(tmp_path: Path) -> None:
    learner = """\
  learner:
    provider: codex
    model: gpt-learner
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
"""
    learning = """\
learning:
  trigger: after_run
  min_evidence_groups: 2
"""
    base = _codex_yaml(memory="  backend: sqlite\n  path: state/memory.sqlite")

    with pytest.raises(AgentConfigError, match="learner"):
        load_agent_config(_write(tmp_path / "only-learning.yaml", base + learning))
    with pytest.raises(AgentConfigError, match="learner"):
        load_agent_config(
            _write(
                tmp_path / "only-learner.yaml",
                base.replace("memory:\n", learner + "memory:\n"),
            )
        )
    loaded = load_agent_config(
        _write(
            tmp_path / "learning.yaml",
            base.replace("memory:\n", learner + "memory:\n") + learning,
        )
    )
    assert loaded.config.learning is not None
    assert loaded.config.reasoners.learner is not None


def test_sqlite_builds_and_inline_learning_composition(monkeypatch, tmp_path: Path) -> None:
    sqlite = load_agent_config(
        _write(
            tmp_path / "sqlite.yaml",
            _codex_yaml(memory="  backend: sqlite\n  path: state/memory.sqlite"),
        )
    )

    memory = composition.build_memory(sqlite)
    assert isinstance(memory, SQLiteMemory)
    assert memory.path == tmp_path / "state" / "memory.sqlite"

    learner = """\
  learner:
    provider: codex
    model: gpt-learner
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
"""
    learning = """\
learning:
  trigger: after_closed_episode
  min_evidence_groups: 1
"""
    source = _codex_yaml(memory="  backend: sqlite\n  path: state/learning.sqlite")
    configured = load_agent_config(
        _write(
            tmp_path / "learning.yaml",
            source.replace("memory:\n", learner + "memory:\n") + learning,
        )
    )

    roles: list[str] = []

    class FakeReasoner:
        async def aclose(self) -> None:
            return None

    class FakeEnvironment:
        def __init__(self, endpoint: str, **kwargs) -> None:
            del endpoint, kwargs

        async def aclose(self) -> None:
            return None

    def build_reasoner(*args, role="decision", **kwargs):
        del args, kwargs
        roles.append(role)
        return FakeReasoner()

    monkeypatch.setattr(composition, "_build_reasoner", build_reasoner)
    monkeypatch.setattr(composition, "UptickV2Environment", FakeEnvironment)

    application = composition.build_application(configured, show_progress=False)

    assert roles == ["decision", "learner"]
    asyncio.run(application.aclose())


def test_inline_learning_requires_one_evidence_group(tmp_path: Path) -> None:
    learner = """\
  learner:
    provider: codex
    model: gpt-learner
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
"""
    learning = """\
learning:
  trigger: after_closed_episode
  min_evidence_groups: 2
"""
    source = _codex_yaml(memory="  backend: sqlite\n  path: state/learning.sqlite")

    with pytest.raises(AgentConfigError, match="learning"):
        load_agent_config(
            _write(
                tmp_path / "invalid-inline.yaml",
                source.replace("memory:\n", learner + "memory:\n") + learning,
            )
        )


def test_after_run_learning_builds_distinct_decision_and_learner_reasoners(
    monkeypatch, tmp_path: Path
) -> None:
    learner = """\
  learner:
    provider: codex
    model: gpt-learner
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
"""
    learning = """\
learning:
  trigger: after_run
  min_evidence_groups: 2
"""
    source = _codex_yaml(memory="  backend: sqlite\n  path: state/learning.sqlite")
    configured = load_agent_config(
        _write(
            tmp_path / "learning.yaml",
            source.replace("memory:\n", learner + "memory:\n") + learning,
        )
    )
    roles: list[str] = []

    class FakeReasoner:
        async def aclose(self) -> None:
            return None

    class FakeEnvironment:
        def __init__(self, endpoint: str, **kwargs) -> None:
            del endpoint, kwargs

        async def aclose(self) -> None:
            return None

    def build_reasoner(*args, role="decision", **kwargs):
        del args, kwargs
        roles.append(role)
        return FakeReasoner()

    monkeypatch.setattr(composition, "_build_reasoner", build_reasoner)
    monkeypatch.setattr(composition, "UptickV2Environment", FakeEnvironment)

    application = composition.build_application(configured, show_progress=False)

    assert roles == ["decision", "learner"]
    assert application.reasoner is not application.learner_reasoner
    assert application.learner_reasoner is not None
    asyncio.run(application.aclose())
