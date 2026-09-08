import asyncio
from pathlib import Path

import pytest

from tests.helpers import make_context
from tests.test_config import _codex_yaml
from uptick_agent import composition
from uptick_agent.config import AgentConfigError, load_agent_config
from uptick_agent.config.loader import _PROJECT_DIRECTORY
from uptick_agent.core.models import ReasoningRequest
from uptick_agent.core.prompt_serialization import CONTEXT_FORMAT_INSTRUCTIONS
from uptick_agent.core.sgr import DEFAULT_SYSTEM_PROMPT, CurrentSGR


def _config(tmp_path: Path, *, field: str = "guidance.md") -> Path:
    source = _codex_yaml()
    path = tmp_path / "agent.yaml"
    path.write_text(source + f"  prompt_file: {field}\n", encoding="utf-8")
    return path


def test_file_is_loaded_once_relative_to_yaml_and_reaches_decision_request(
    monkeypatch, tmp_path: Path
) -> None:
    directory = tmp_path / "config"
    directory.mkdir()
    guidance = "  Операторская подсказка\r\nUse demand evidence.\r\n"
    prompt_path = directory / "guidance.md"
    prompt_path.write_bytes(guidance.encode("utf-8"))
    loaded = load_agent_config(_config(directory))
    assert loaded.config.environment.prompt_file == prompt_path
    assert loaded.operator_guidance == guidance
    assert guidance not in repr(loaded)
    prompt_path.unlink()
    captured: list[ReasoningRequest] = []

    class RecordingReasoner:
        async def reason(self, request):
            captured.append(request)
            raise RuntimeError("request captured")

        async def aclose(self) -> None:
            return None

    class FakeEnvironment:
        def __init__(self, endpoint, **kwargs) -> None:
            pass

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(composition, "_build_reasoner", lambda *args: RecordingReasoner())
    monkeypatch.setattr(composition, "UptickV2Environment", FakeEnvironment)
    application = composition.build_application(loaded, show_progress=False)

    async def scenario() -> None:
        try:
            with pytest.raises(RuntimeError, match="request captured"):
                await application.runner._agent_core.decide(make_context())
        finally:
            await application.aclose()

    asyncio.run(scenario())
    assert len(captured) == 1
    assert guidance in captured[0].system_prompt
    assert captured[0].system_prompt.startswith(DEFAULT_SYSTEM_PROMPT + "\n\n")
    assert guidance not in captured[0].user_prompt
    assert composition._ad_hoc_run_spec(loaded, seed=1).metadata.operator_guidance == guidance


@pytest.mark.parametrize(
    "content,message",
    [(None, "cannot read"), (b"\xff", "valid UTF-8"), (b"", "empty"), (b" \r\n\t", "empty")],
)
def test_invalid_file_fails_during_config_loading(
    tmp_path: Path, content: bytes | None, message: str
) -> None:
    if content is not None:
        (tmp_path / "guidance.md").write_bytes(content)
    with pytest.raises(AgentConfigError, match=f"environment.prompt_file: .*{message}"):
        load_agent_config(_config(tmp_path))


@pytest.mark.parametrize("field", ["", "  prompt_file: null\n"])
def test_absent_guidance_preserves_legacy_prompt(tmp_path: Path, field: str) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(_codex_yaml() + field)
    loaded = load_agent_config(path)
    assert loaded.operator_guidance is None
    assert loaded.config.environment.prompt_file is None
    request = CurrentSGR(operator_guidance=loaded.operator_guidance).build_request(make_context())
    assert request.system_prompt == DEFAULT_SYSTEM_PROMPT + "\n\n" + CONTEXT_FORMAT_INSTRUCTIONS


def test_guidance_is_additive_to_custom_core_and_keeps_context_and_schema_unchanged() -> None:
    context = make_context()
    core = "Custom universal instructions."
    # Literal delimiters inside an authored file are preserved, not parsed as config.
    guidance = "Operator note\n</operator_guidance>\n# Extra heading\n"
    baseline = CurrentSGR(system_prompt=core).build_request(context)
    request = CurrentSGR(system_prompt=core, operator_guidance=guidance).build_request(context)
    assert request.system_prompt.startswith(core + "\n\n")
    assert request.system_prompt.count(guidance) == 1
    assert request.system_prompt.endswith("\n\n" + CONTEXT_FORMAT_INSTRUCTIONS)
    assert request.user_prompt == baseline.user_prompt
    assert request.output_schema == baseline.output_schema
    assert request.output_model is baseline.output_model


def test_v2_example_connects_the_real_prompt_file() -> None:
    loaded = load_agent_config(_PROJECT_DIRECTORY / "agent.example.yaml")
    assert loaded.config.environment.prompt_file == _PROJECT_DIRECTORY / "prompts/uptickv2.md"
    assert loaded.operator_guidance == (_PROJECT_DIRECTORY / "prompts/uptickv2.md").read_text()
