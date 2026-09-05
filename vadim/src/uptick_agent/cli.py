from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from uptick_agent.composition.evaluation_memory import DefaultEvaluationMemoryFactory
from uptick_agent.decisions.contracts import NextStep, V1NextStep, V2NextStep
from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT, compose_system_prompt
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.environment.prestarted import PrestartedEnvironment
from uptick_agent.evaluation.artifacts import FilesystemEvaluationArtifactStore
from uptick_agent.evaluation.contracts import V2EvaluationProfile, V2Manifest, resolved_manifest
from uptick_agent.evaluation.execution import EvaluationRuntime
from uptick_agent.evaluation.lifecycle import EvaluationJournal
from uptick_agent.experiments import ExperimentRunner
from uptick_agent.llm import (
    GenerationSettings,
    LlmProviderConfig,
    LlmProviderFactory,
    LlmProviderRegistry,
    OpenAIProviderFactory,
)
from uptick_agent.llm.decision_model import (
    StructuredDecisionModel as _GenericStructuredDecisionModel,
)
from uptick_agent.memory import InMemoryMemory, JsonlMemory, legacy_memory_runtime
from uptick_agent.memory.stores import SqliteStructuredStore
from uptick_agent.observers import CompositeObserver, ConsoleObserver, JsonlObserver
from uptick_agent.ports import AgentMemory, DecisionModel
from uptick_agent.runs.config import AgentConfig
from uptick_agent.runs.execute import AgentRunner
from uptick_agent.simulator import SimulatorClient, SimulatorEnvironment
from uptick_agent.simulator.briefings import (
    V1_ENVIRONMENT_BRIEFING,
    V2_ENVIRONMENT_BRIEFING,
)
from uptick_agent.simulator.v2_client import SimulatorV2Client
from uptick_agent.simulator.v2_environment import SimulatorV2Environment
from uptick_agent.simulator.v2_policy import SimulatorV2TimeBudgetPolicy
from uptick_agent.stage0 import sha256_file, sha256_json, sha256_tree


class CloseableDecisionModel(DecisionModel, Protocol):
    async def aclose(self) -> None: ...


class CodexFactoryConstructor(Protocol):
    def __call__(self) -> LlmProviderFactory: ...


class StructuredDecisionModel(_GenericStructuredDecisionModel):
    """Historical CLI facade; canonical bridge callers must pass a schema."""

    def __init__(self, client, *, response_model=NextStep, **kwargs):
        super().__init__(client, response_model=response_model, **kwargs)


def _decision_provider_default() -> str:
    provider = os.getenv("DECISION_PROVIDER", "openai")
    if provider not in {"openai", "codex"}:
        raise ValueError(
            f"DECISION_PROVIDER must be exactly 'openai' or 'codex'; got {provider!r}."
        )
    return provider


def _load_codex_factory() -> CodexFactoryConstructor:
    try:
        from uptick_agent.llm.codex import CodexProviderFactory
    except ModuleNotFoundError as error:
        if error.name == "openai_codex":
            raise RuntimeError(
                "Codex provider requires the optional dependency. "
                "Run `uv sync --extra codex` before using --decision-provider codex."
            ) from error
        raise
    return CodexProviderFactory


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--simulator-url", default=os.getenv("SIMULATOR_URL", "http://81.176.229.58:8080")
    )
    parser.add_argument(
        "--simulator-api-version",
        choices=["v1", "v2"],
        default="v2",
        help="Simulator API contract to use (v2 by default; v1 is the legacy adapter).",
    )
    parser.add_argument(
        "--decision-provider",
        choices=["openai", "codex"],
        default=_decision_provider_default(),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OpenAI uses OPENAI_MODEL (or gpt-4.1-mini); Codex uses optional CODEX_MODEL.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        default=None,
        help="Optional provider reasoning effort; omitted keeps provider defaults.",
    )
    parser.add_argument("--openai-base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--agent-id", default="uptick-sgr")
    parser.add_argument("--agent-version", default="baseline-0.1")
    parser.add_argument("--max-steps", type=int, default=160)
    parser.add_argument("--memory", choices=["none", "in-memory", "jsonl"], default="jsonl")
    parser.add_argument("--memory-file", type=Path, default=Path("memory.jsonl"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uptick-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run one agent against one simulator seed")
    _common(run)
    run.add_argument("--seed", type=int, required=True)

    benchmark = subparsers.add_parser("benchmark", help="run the same agent against several seeds")
    _common(benchmark)
    benchmark.add_argument("--name", required=True)
    benchmark.add_argument("--seeds", required=True, help="comma-separated non-zero integers")
    benchmark.add_argument(
        "--carry-memory",
        action="store_true",
        help="allow earlier seeds to affect later seeds; disabled by default for fair comparisons",
    )

    evaluate = subparsers.add_parser(
        "evaluate-v2", help="run a preregistered v2 evaluation profile"
    )
    evaluate.add_argument("--profile", type=Path, required=True)
    evaluate.add_argument(
        "--environment-briefing",
        type=Path,
        default=None,
        help="Previously observed sanitized startup text used to preregister the prompt.",
    )
    evaluate.add_argument(
        "--simulator-url", default=os.getenv("SIMULATOR_URL", "http://81.176.229.58:8080")
    )
    evaluate.add_argument("--openai-base-url", default=os.getenv("OPENAI_BASE_URL"))
    evaluate.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    evaluate.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help=(
            "project checkout or frozen source capsule\n"
            "containing src/, pyproject.toml, and uv.lock"
        ),
    )
    return parser


def _memory_factory(args) -> Callable[[], AgentMemory]:
    if args.memory == "none":
        return lambda: legacy_memory_runtime(None)
    if args.memory == "in-memory":
        return lambda: legacy_memory_runtime(InMemoryMemory())
    return lambda: legacy_memory_runtime(JsonlMemory(args.memory_file))


def _trace_name(args: argparse.Namespace) -> str:
    if args.command == "benchmark":
        return args.name
    return f"seed-{args.seed}"


def _decision_model(
    args: argparse.Namespace,
    decision_spec: EnvironmentDecisionSpec | None = None,
) -> CloseableDecisionModel:
    if args.decision_provider not in {"openai", "codex"}:
        raise ValueError(f"Unsupported decision provider {args.decision_provider!r}.")
    if decision_spec is None:
        # Compatibility for callers of this private helper.  The real CLI
        # path constructs the environment first and passes its immutable spec.
        if getattr(args, "simulator_api_version", "v2") == "v1":
            decision_spec = EnvironmentDecisionSpec(
                response_model=V1NextStep,
                environment_briefing=V1_ENVIRONMENT_BRIEFING,
            )
        else:
            decision_spec = EnvironmentDecisionSpec(
                response_model=V2NextStep,
                environment_briefing=V2_ENVIRONMENT_BRIEFING,
            )

    registry = LlmProviderRegistry()
    registry.register(
        "openai",
        OpenAIProviderFactory(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=args.openai_base_url,
        ),
    )

    if args.decision_provider == "codex":
        if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
            raise ValueError(
                "Codex subscription provider refuses API-key configuration. "
                "Unset OPENAI_API_KEY and CODEX_API_KEY to prevent API billing, then run "
                "`codex login` on your trusted local machine."
            )
        codex_factory = _load_codex_factory()
        registry.register("codex", codex_factory())

    if args.decision_provider == "openai":
        model = args.model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    else:
        model = args.model or os.getenv("CODEX_MODEL") or None
    settings = GenerationSettings(reasoning_effort=args.reasoning_effort)
    client = registry.create(LlmProviderConfig(provider=args.decision_provider, model=model))
    assert decision_spec is not None
    return _build_decision_model(client, args, decision_spec, settings=settings)


def _build_decision_model(
    client: Any,
    args: argparse.Namespace,
    decision_spec: EnvironmentDecisionSpec,
    *,
    settings: GenerationSettings,
) -> CloseableDecisionModel:
    if getattr(args, "simulator_api_version", "v2") == "v1":
        return StructuredDecisionModel(
            client,
            response_model=decision_spec.response_model,
            environment_briefing=decision_spec.environment_briefing,
            settings=settings,
        )
    return SimulatorV2TimeBudgetPolicy(
        StructuredDecisionModel(
            client,
            response_model=decision_spec.response_model,
            environment_briefing=decision_spec.environment_briefing,
            settings=settings,
        )
    )


def _load_v2_manifest(path: Path) -> V2Manifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read v2 evaluation profile {path}") from error
    if not isinstance(payload, dict):
        raise ValueError("v2 evaluation profile must be a JSON object")
    if "manifest_hash" in payload:
        return V2Manifest.model_validate(payload)
    return resolved_manifest(V2EvaluationProfile.model_validate(payload))


def _reject_unsupported_xmemory(profile: V2EvaluationProfile) -> None:
    """Reject mutable external memory before an evaluation can start.

    ``xmemory`` is intentionally not part of the frozen-memory contract yet.
    Use ``getattr`` so profiles created before that field existed retain their
    current behavior.
    """

    enabled_conditions = tuple(
        condition.condition_id
        for condition in profile.conditions
        if (
            (module := getattr(condition.memory_configuration, "xmemory", None)) is not None
            and bool(getattr(module, "enabled", False))
        )
    )
    if enabled_conditions:
        condition_ids = ", ".join(enabled_conditions)
        raise ValueError(
            "evaluate-v2 cannot run with enabled xmemory: immutable snapshot export is "
            f"unsupported for condition(s) {condition_ids}; disable xmemory before evaluation"
        )


def _profile_generation_settings(profile: V2EvaluationProfile) -> GenerationSettings:
    allowed = {"temperature", "max_output_tokens", "reasoning_effort"}
    unknown = set(profile.provider.settings) - allowed
    if unknown:
        raise ValueError(
            "v2 provider settings contain unsupported generation controls: "
            + ", ".join(sorted(unknown))
        )
    return GenerationSettings(
        temperature=profile.provider.settings.get("temperature"),
        max_output_tokens=profile.provider.settings.get("max_output_tokens"),
        reasoning_effort=profile.provider.settings.get("reasoning_effort"),
    )


def _git_output(source_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git command failed"
        raise ValueError(f"cannot verify source provenance: {detail}")
    return result.stdout.strip()


def _v2_source_root(args: argparse.Namespace) -> Path:
    # An explicit capsule is the reproducibility boundary.  The __file__
    # fallback is only for a local checkout and is never used to invent a pin.
    return (
        args.source_root
        if args.source_root is not None
        else Path(__file__).resolve().parents[1].parent
    ).resolve()


def _verify_v2_pins(
    profile: V2EvaluationProfile,
    args: argparse.Namespace,
) -> None:
    """Verify every local/provider pin before constructing external clients."""

    source_root = _v2_source_root(args)
    source_dir = source_root / "src"
    pyproject = source_root / "pyproject.toml"
    lockfile = source_root / "uv.lock"
    if not source_dir.is_dir() or not pyproject.is_file() or not lockfile.is_file():
        raise ValueError("--source-root must contain src/, pyproject.toml, and uv.lock")
    running_source_dir = Path(__file__).resolve().parents[1]
    if source_dir.resolve() != running_source_dir:
        raise ValueError("--source-root/src must be the executing uptick_agent package root")

    declared_revision = profile.source.source_revision
    actual_revision = _git_output(source_root, "rev-parse", "HEAD")
    if actual_revision != declared_revision:
        raise ValueError(
            f"source revision mismatch: profile={declared_revision!r}, checkout={actual_revision!r}"
        )
    status = _git_output(
        source_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "src",
        "pyproject.toml",
        "uv.lock",
    )
    actual_dirty = bool(status)
    if profile.source.source_dirty is None:
        raise ValueError("source_dirty must be declared for v2 execution")
    if actual_dirty != profile.source.source_dirty:
        raise ValueError(
            "scoped source dirty-state mismatch: "
            f"profile={profile.source.source_dirty!r}, checkout={actual_dirty!r}"
        )

    actual_tree_hash = sha256_tree(source_dir)
    if actual_tree_hash != profile.source.source_tree_hash:
        raise ValueError("source_tree_hash does not match the selected source capsule")
    actual_lock_hash = sha256_file(lockfile)
    if actual_lock_hash != profile.source.dependency_lock_hash:
        raise ValueError("dependency_lock_hash does not match uv.lock")
    actual_runtime_fingerprint = sha256_json(
        {
            "source_tree_hash": actual_tree_hash,
            "pyproject_hash": sha256_file(pyproject),
        }
    )
    if profile.source.runtime_fingerprint != actual_runtime_fingerprint:
        raise ValueError(
            "runtime_fingerprint must equal sha256_json(source_tree_hash, pyproject_hash)"
        )

    expected_briefing = _expected_environment_briefing(args)
    expected_prompt = _prompt_fingerprint(expected_briefing)
    if profile.provider.prompt_fingerprint != expected_prompt:
        raise ValueError(
            "v2 provider prompt_fingerprint does not match the external environment briefing"
        )
    settings = _profile_generation_settings(profile)
    resolved_settings = {
        key: getattr(settings, key)
        for key in ("temperature", "max_output_tokens", "reasoning_effort")
        if key in profile.provider.settings
    }
    if sha256_json(resolved_settings) != profile.provider.settings_fingerprint:
        raise ValueError("v2 provider settings_fingerprint does not match resolved settings")
    if profile.provider.policy_id != SimulatorV2TimeBudgetPolicy.policy_id:
        raise ValueError("v2 provider policy_id does not match the installed policy")
    if profile.provider.policy_version != SimulatorV2TimeBudgetPolicy.policy_version:
        raise ValueError("v2 provider policy_version does not match the installed policy")
    estimators = {
        (
            condition.memory_configuration.context_budget.estimator_id,
            condition.memory_configuration.context_budget.estimator_version,
        )
        for condition in profile.conditions
    }
    if len(estimators) != 1 or next(iter(estimators)) != (
        profile.provider.token_estimator_id,
        profile.provider.token_estimator_version,
    ):
        raise ValueError(
            "token estimator pin must match the single resolved context estimator "
            "used by every v2 condition"
        )

    if profile.provider.provider not in {"openai", "codex"}:
        raise ValueError(f"v2 evaluation does not support provider {profile.provider.provider!r}")
    if profile.provider.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise ValueError("v2 OpenAI evaluation requires OPENAI_API_KEY")
    if profile.provider.provider == "codex" and (
        os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY")
    ):
        raise ValueError("v2 Codex evaluation refuses API-key configuration; unset API keys")

    endpoint_hash = hashlib.sha256(args.simulator_url.rstrip("/").encode("utf-8")).hexdigest()
    for pin in (profile.environment, *profile.world_contexts.values()):
        if pin.endpoint_fingerprint is not None and pin.endpoint_fingerprint != endpoint_hash:
            raise ValueError(
                f"endpoint_fingerprint does not match simulator URL for {pin.scenario_id!r}"
            )


def _v2_model_factory(
    profile: V2EvaluationProfile,
    args: argparse.Namespace,
    decision_spec: EnvironmentDecisionSpec,
):
    decision_spec.assert_unchanged()
    if (
        _prompt_fingerprint(decision_spec.environment_briefing)
        != profile.provider.prompt_fingerprint
    ):
        raise ValueError("actual environment startup prompt differs from the preregistered prompt")
    if decision_spec.response_model.model_json_schema() != V2NextStep.model_json_schema():
        raise ValueError("actual environment tool schema differs from the pinned v2 adapter")
    provider = profile.provider.provider
    if provider not in {"openai", "codex"}:
        raise ValueError(f"v2 evaluation does not support provider {provider!r}")
    registry = LlmProviderRegistry()
    registry.register(
        "openai",
        OpenAIProviderFactory(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=args.openai_base_url,
        ),
    )
    if provider == "codex":
        if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
            raise ValueError(
                "Codex subscription provider refuses API-key configuration; unset API keys"
            )
        registry.register("codex", _load_codex_factory()())
    client = registry.create(LlmProviderConfig(provider=provider, model=profile.provider.model))
    return SimulatorV2TimeBudgetPolicy(
        StructuredDecisionModel(
            client,
            response_model=decision_spec.response_model,
            environment_briefing=decision_spec.environment_briefing,
            settings=_profile_generation_settings(profile),
        )
    )


async def _evaluate_v2(args: argparse.Namespace) -> int:
    manifest = _load_v2_manifest(args.profile)
    if manifest.profile.simulator_api_version != "v2":
        raise ValueError("evaluate-v2 accepts only a v2 profile")
    _reject_unsupported_xmemory(manifest.profile)
    _verify_v2_pins(manifest.profile, args)
    artifact_store = FilesystemEvaluationArtifactStore(args.artifacts)
    journal = EvaluationJournal(manifest, artifacts=artifact_store)
    memory_store = SqliteStructuredStore(args.artifacts / "memory.sqlite3")
    memory_factory = DefaultEvaluationMemoryFactory(manifest, store=memory_store)

    class OwnedV2Environment(SimulatorV2Environment):
        async def aclose(self) -> None:
            await self.client.aclose()

    def environment_factory(block, condition, attempt):
        return OwnedV2Environment(SimulatorV2Client(args.simulator_url))

    def model_factory(block, condition, attempt, run_id, decision_spec):
        return _v2_model_factory(manifest.profile, args, decision_spec)

    runtime = EvaluationRuntime(
        manifest,
        environment_factory=environment_factory,
        model_factory=model_factory,
        memory_factory=memory_factory,
        binding_factory=memory_factory.freeze_binding,
        journal=journal,
    )
    report = await runtime.run()
    artifact_store.put("report", manifest.manifest_id, report.model_dump(mode="json"))
    report_path = args.artifacts / "report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.model_dump_json(indent=2))
    return 0


async def _main(args) -> int:
    if args.command == "evaluate-v2":
        return await _evaluate_v2(args)
    if getattr(args, "seed", 1) == 0:
        raise ValueError("simulator seed 0 is invalid")

    api_version = getattr(args, "simulator_api_version", "v2")
    if api_version not in {"v1", "v2"}:
        raise ValueError(f"Unsupported simulator API version {api_version!r}.")
    config_values = {
        "agent_id": args.agent_id,
        "agent_version": args.agent_version,
        "max_steps": args.max_steps,
    }
    config = AgentConfig(**config_values)
    seeds: list[int] | None = None
    if args.command == "benchmark":
        seeds = [int(value.strip()) for value in args.seeds.split(",") if value.strip()]
        if not seeds:
            raise ValueError("at least one seed is required")
        if 0 in seeds:
            raise ValueError("simulator seed 0 is invalid")
    memory_factory = _memory_factory(args)

    class PreparedRun:
        def __init__(self):
            self.memory = memory_factory()

        async def run(self, seed):
            return await _run_seed(args, config, self.memory, seed)

    if args.command == "run":
        result = await PreparedRun().run(args.seed)
        print(result.model_dump_json(indent=2))
    else:
        assert seeds is not None
        result = await ExperimentRunner(PreparedRun).run(
            name=args.name,
            seeds=seeds,
            carry_memory=args.carry_memory,
        )
        destination = args.artifacts / args.name / "summary.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(result.model_dump_json(indent=2))
    return 0


def _prompt_fingerprint(briefing: str | None) -> str:
    return hashlib.sha256(compose_system_prompt(CORE_SYSTEM_PROMPT, briefing).encode()).hexdigest()


def _expected_environment_briefing(args: argparse.Namespace) -> str:
    path = getattr(args, "environment_briefing", None)
    if path is None:
        raise ValueError(
            "evaluate-v2 requires --environment-briefing "
            "with the preregistered external startup text"
        )
    value = Path(path).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("external environment briefing must not be empty")
    return value


async def _run_seed(args, config: AgentConfig, memory: AgentMemory, seed: int):
    model = None
    api_version = getattr(args, "simulator_api_version", "v2")
    client = (
        SimulatorClient(args.simulator_url)
        if api_version == "v1"
        else SimulatorV2Client(args.simulator_url)
    )
    try:
        environment = (
            SimulatorEnvironment(client, environment_briefing=V1_ENVIRONMENT_BRIEFING)
            if api_version == "v1"
            else SimulatorV2Environment(client)
        )
        session, latest = await environment.start(
            seed=seed,
            agent_id=config.agent_id,
            agent_version=config.agent_version,
        )
        startup_artifacts = FilesystemEvaluationArtifactStore(args.artifacts / _trace_name(args))
        startup_artifacts.put(
            "startup_observation",
            session.run_id,
            {"run_id": session.run_id, "observation": latest.model_dump(mode="json")},
        )
        prepared = PrestartedEnvironment(environment, session, latest)
        spec = prepared.decision_spec
        # Retain exact effective public inputs before any decision-provider call.
        startup_artifacts.put(
            "startup_spec",
            session.run_id,
            {
                "run_id": session.run_id,
                "spec": spec.public_input(),
                "spec_fingerprint": spec.fingerprint,
                "prompt_fingerprint": _prompt_fingerprint(spec.environment_briefing),
            },
        )
        model = _decision_model(args, spec)
        observer = CompositeObserver(
            ConsoleObserver(),
            JsonlObserver(args.artifacts / _trace_name(args) / "trace.jsonl"),
        )
        return await AgentRunner(
            config=config,
            model=model,
            memory=memory,
            environment=prepared,
            observer=observer,
        ).run(seed)
    finally:
        try:
            if model is not None:
                await model.aclose()
        finally:
            await client.aclose()


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
