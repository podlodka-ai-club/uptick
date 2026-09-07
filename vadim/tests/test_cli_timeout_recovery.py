from types import SimpleNamespace

import pytest

from uptick_agent import cli


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_passes_explicit_timeout_policy_to_provider_registry(monkeypatch, enabled):
    configs = []

    def create(_registry, config):
        configs.append(config)
        return SimpleNamespace(model=config.model)

    monkeypatch.setattr(cli.LlmProviderRegistry, "create", create)
    arguments = ["run", "--seed", "1", "--decision-provider", "openai", "--model", "test"]
    if enabled:
        arguments.append("--recover-decision-timeouts")
    model = cli._decision_model(cli._parser().parse_args(arguments))
    assert model.model == "test"
    policy = configs[0].timeout_recovery
    if enabled:
        assert policy.attempt_timeout_seconds == 45
        assert policy.total_timeout_seconds == 120
        assert policy.max_attempts == 2
    else:
        assert policy is None


def test_pinned_evaluation_does_not_accept_unregistered_timeout_override():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "evaluate-v2",
                "--profile",
                "unused.json",
                "--recover-decision-timeouts",
            ]
        )
