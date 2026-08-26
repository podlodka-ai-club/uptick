import asyncio

import pytest
from pydantic import ValidationError

from uptick_agent import cli


def test_cli_uses_codex_model_default_and_cli_override_without_api_keys(monkeypatch) -> None:
    created: list[object] = []

    class FakeCodexModel:
        def __init__(self, *, model: str | None) -> None:
            self.model = model
            created.append(self)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_MODEL", "codex-from-env")
    monkeypatch.setattr(cli, "_load_codex_model", lambda: FakeCodexModel)

    args = cli._parser().parse_args(["run", "--seed", "1", "--decision-provider", "codex"])
    model = cli._decision_model(args)
    assert model.model == "codex-from-env"

    overridden_args = cli._parser().parse_args(
        ["run", "--seed", "1", "--decision-provider", "codex", "--model", "chosen-codex"]
    )
    overridden_model = cli._decision_model(overridden_args)
    assert overridden_model.model == "chosen-codex"
    assert len(created) == 2


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_cli_refuses_codex_provider_when_an_api_key_is_set(monkeypatch, variable: str) -> None:
    monkeypatch.setenv(variable, "not-a-real-key")
    args = cli._parser().parse_args(["run", "--seed", "1", "--decision-provider", "codex"])

    with pytest.raises(ValueError, match="Unset OPENAI_API_KEY and CODEX_API_KEY"):
        cli._decision_model(args)


@pytest.mark.parametrize("provider", ["not-a-provider", " codex "])
def test_cli_rejects_invalid_or_whitespace_padded_provider_environment(
    monkeypatch, provider: str
) -> None:
    monkeypatch.setenv("DECISION_PROVIDER", provider)

    with pytest.raises(ValueError, match="DECISION_PROVIDER must be exactly"):
        cli._parser()


def test_cli_rejects_unknown_provider_internally(monkeypatch) -> None:
    monkeypatch.delenv("DECISION_PROVIDER", raising=False)
    args = cli._parser().parse_args(["run", "--seed", "1"])
    args.decision_provider = "unexpected"

    with pytest.raises(ValueError, match="Unsupported decision provider"):
        cli._decision_model(args)


def test_cli_validates_config_before_constructing_a_codex_model(monkeypatch) -> None:
    created: list[object] = []

    class FakeCodexModel:
        def __init__(self, *, model: str | None) -> None:
            created.append(self)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_load_codex_model", lambda: FakeCodexModel)
    args = cli._parser().parse_args(
        ["run", "--seed", "1", "--max-steps", "0", "--decision-provider", "codex"]
    )

    with pytest.raises(ValidationError):
        asyncio.run(cli._main(args))

    assert created == []
