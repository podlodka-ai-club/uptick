import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from uptick_agent import cli, composition
from uptick_agent.config import AgentConfigError, load_agent_config
from uptick_agent.core.models import RunSpec


def _write_agent_config(tmp_path: Path, *, seed: int = 7) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(
        f"""\
schema_version: 2
agent:
  id: af-sgr
  version: af-sgr-v2-0.1
reasoners:
  decision:
    provider: codex
    model: test-codex
    effort: high
    thread_mode: ephemeral
    timeout_seconds: 600
    retries: 1
memory:
  backend: none
run_store:
  backend: jsonl
  path: .agent/runs
environment:
  adapter: uptickv2
  endpoint: http://unused.invalid
  seed: {seed}
""",
        encoding="utf-8",
    )
    return path


def test_run_cli_uses_only_config_and_live_operational_inputs(tmp_path: Path) -> None:
    config_path = _write_agent_config(tmp_path)
    args = cli._parser().parse_args(["run", "--config", str(config_path), "--live"])

    assert args.config == config_path
    assert args.live
    assert not hasattr(args, "seed")
    assert RunSpec(run_id="run-default").agent_id == "af-sgr"


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--seed", "1"),
        ("--simulator-url", "http://example.invalid"),
        ("--openai-base-url", "http://example.invalid"),
        ("--agent-id", "other"),
        ("--agent-version", "other"),
        ("--objective", "other"),
        ("--max-steps", "1"),
        ("--memory-recall-limit", "1"),
        ("--decision-provider", "codex"),
        ("--model", "other"),
        ("--effort", "high"),
        ("--timeout-seconds", "1"),
        ("--retries", "1"),
        ("--memory", "none"),
        ("--memory-file", "memory.jsonl"),
    ],
)
def test_run_cli_rejects_removed_semantic_flags(tmp_path: Path, flag: str, value: str) -> None:
    config_path = _write_agent_config(tmp_path)
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["run", "--config", str(config_path), flag, value])
    assert error.value.code == 2


def test_reasoner_config_is_fully_resolved_from_yaml_not_environment(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_MODEL", "ignored-from-env")
    loaded = load_agent_config(_write_agent_config(tmp_path))

    resolved = composition.resolve_reasoner_config(loaded)

    assert resolved.model == "test-codex"
    assert resolved.effort == "high"
    assert resolved.thread_mode == "ephemeral"
    assert resolved.timeout_seconds == 600
    assert resolved.retries == 1


def test_cli_validates_yaml_before_running_composition(monkeypatch, tmp_path: Path) -> None:
    path = _write_agent_config(tmp_path, seed=0)
    called = False

    async def fake_run(*args, **kwargs):
        nonlocal called
        del args, kwargs
        called = True

    monkeypatch.setattr(cli, "run_uptick", fake_run)
    args = cli._parser().parse_args(["run", "--config", str(path), "--live"])

    with pytest.raises(AgentConfigError):
        asyncio.run(cli._main(args))
    assert not called


def test_run_passes_loaded_config_with_uptick_seed(monkeypatch, tmp_path: Path) -> None:
    seen: list[int] = []

    class FakeResult:
        def model_dump_json(self, *, indent: int) -> str:
            del indent
            return "{}"

    async def fake_run(config, *, live):
        assert live
        seen.append(config.config.environment.seed)
        return FakeResult()

    monkeypatch.setattr(cli, "run_uptick", fake_run)
    args = cli._parser().parse_args(
        ["run", "--config", str(_write_agent_config(tmp_path, seed=19)), "--live"]
    )

    assert asyncio.run(cli._main(args)) == 0
    assert seen == [19]


def test_comparison_artifact_id_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="safe path component"):
        cli._safe_artifact_id("../outside")


def test_git_provenance_records_revision_and_scopes_dirty_check_to_af(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        output = "c" * 40 + "\n" if len(calls) == 1 else " M README.md\n"
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(composition.subprocess, "run", fake_run)

    revision, dirty, reasons = composition._git_provenance()

    assert revision == "c" * 40
    assert dirty is True
    assert reasons == []
    assert calls[0][-2:] == ["rev-parse", "HEAD"]
    assert calls[1][-3:] == ["--porcelain", "--", "."]


def test_run_requires_explicit_live_before_adapter_construction(
    monkeypatch, tmp_path: Path
) -> None:
    loaded = load_agent_config(_write_agent_config(tmp_path))
    called = False

    def fake_build(*args, **kwargs):
        nonlocal called
        del args, kwargs
        called = True

    monkeypatch.setattr(composition, "build_application", fake_build)

    with pytest.raises(ValueError, match="explicit live"):
        asyncio.run(composition.run_uptick(loaded, live=False))
    assert not called


class _FakeReport:
    has_failures = True

    def model_dump_json(self, *, indent: int) -> str:
        del indent
        return '{"failed_attempts":1}'


def test_offline_compare_reports_does_not_load_agent_config(monkeypatch, tmp_path: Path) -> None:
    class FakeInputReport:
        @classmethod
        def model_validate_json(cls, source: str):
            del source
            return object()

    def forbidden_loader(*args, **kwargs):
        del args, kwargs
        raise AssertionError("offline command must not load agent config")

    monkeypatch.setattr(cli, "ExperimentReport", FakeInputReport)
    monkeypatch.setattr(cli, "load_agent_config", forbidden_loader)
    monkeypatch.setattr(cli, "summarize_comparison", lambda **kwargs: _FakeReport())
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text("{}", encoding="utf-8")
    right.write_text("{}", encoding="utf-8")
    args = cli._parser().parse_args(
        [
            "compare-reports",
            "--name",
            "offline",
            "--baseline-report",
            str(left),
            "--candidate-report",
            str(right),
            "--output",
            str(tmp_path / "comparison.json"),
        ]
    )

    assert asyncio.run(cli._main(args)) == 2


def test_export_corpus_accepts_ad_hoc_run_without_experiment_report(
    monkeypatch, tmp_path: Path
) -> None:
    seen: list[tuple[str, Path]] = []

    async def fake_export(run_id: str, *, trace_path: Path):
        seen.append((run_id, trace_path))
        return _FakeReport()

    monkeypatch.setattr(cli, "export_ad_hoc_corpus", fake_export)
    trace = tmp_path / "runs" / "trace.jsonl"
    output = tmp_path / "corpus.json"
    args = cli._parser().parse_args(
        [
            "export-corpus",
            "--run-id",
            "uptickv2-7-20260908T120000000000Z",
            "--trace",
            str(trace),
            "--output",
            str(output),
        ]
    )

    assert asyncio.run(cli._main(args)) == 0
    assert seen == [("uptickv2-7-20260908T120000000000Z", trace)]
    assert output.read_text(encoding="utf-8") == '{"failed_attempts":1}'


def test_export_corpus_requires_exactly_one_provenance_source(tmp_path: Path) -> None:
    parser = cli._parser()
    common = ["--trace", str(tmp_path / "trace.jsonl"), "--output", str(tmp_path / "out.json")]
    with pytest.raises(SystemExit):
        parser.parse_args(["export-corpus", *common])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "export-corpus",
                "--report",
                str(tmp_path / "report.json"),
                "--run-id",
                "run-1",
                *common,
            ]
        )


def test_corpus_export_does_not_silently_read_a_different_trace(tmp_path: Path) -> None:
    (tmp_path / "trace.jsonl").write_text("this is not the requested file")
    requested = tmp_path / "custom.jsonl"
    requested.write_text("requested trace")
    with pytest.raises(ValueError, match="trace path must name"):
        asyncio.run(composition.export_ad_hoc_corpus("run-1", trace_path=requested))


def test_corpus_export_requires_an_existing_trace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="trace file does not exist"):
        asyncio.run(composition.export_ad_hoc_corpus("run-1", trace_path=tmp_path / "trace.jsonl"))
