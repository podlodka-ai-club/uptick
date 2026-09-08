from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from uptick_agent.config import (
    CodexDecisionReasonerConfig,
    LoadedAgentConfig,
    NoMemoryConfig,
    OpenAIDecisionReasonerConfig,
    SQLiteMemoryConfig,
)
from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.contracts import Memory, Policy, Reasoner, RunStore
from uptick_agent.core.models import (
    DEFAULT_OBJECTIVE,
    ReasonerConfig,
    RunMetadata,
    RunResult,
    RunSpec,
)
from uptick_agent.core.policy import DecisionPolicy
from uptick_agent.core.sgr import CurrentSGR
from uptick_agent.decision_corpus import (
    DecisionCorpus,
    ReplayReport,
    export_decision_corpus,
    replay_decision,
)
from uptick_agent.decision_corpus import (
    export_ad_hoc_corpus as export_ad_hoc_decision_corpus,
)
from uptick_agent.environments.programmatic import ProgrammableLauncher
from uptick_agent.environments.uptickv2 import UptickV2Environment
from uptick_agent.experiments import (
    ExperimentReport,
)
from uptick_agent.learning import LearningOrchestrator, MemoryConsolidator
from uptick_agent.memory import NoMemory, SQLiteMemory
from uptick_agent.reasoners.openai import build_openai_reasoner
from uptick_agent.runtime.bootstrap import (
    ReasonerEnvironmentBootstrapper,
)
from uptick_agent.runtime.identity import allocate_ad_hoc_run_id
from uptick_agent.runtime.runner import AgentRunner
from uptick_agent.store import ConsoleReportingRunStore, JsonlRunStore

_PROJECT_DIRECTORY = Path(__file__).resolve().parents[2]


class _CloseableReasoner(Reasoner, Protocol):
    async def aclose(self) -> None: ...


class _CodexReasonerConstructor(Protocol):
    def __call__(self, *, config: ReasonerConfig) -> _CloseableReasoner: ...


@dataclass(slots=True)
class Application:
    runner: AgentRunner
    reasoner: _CloseableReasoner
    learner_reasoner: _CloseableReasoner | None
    environment: ProgrammableLauncher
    run_store: RunStore
    jsonl_store: JsonlRunStore
    learning: LearningOrchestrator | None

    async def aclose(self) -> None:
        try:
            try:
                await self.reasoner.aclose()
            finally:
                if self.learner_reasoner is not None:
                    await self.learner_reasoner.aclose()
        finally:
            await self.environment.aclose()


@dataclass(slots=True)
class DecisionApplication:
    agent_core: AgentCore
    reasoner: _CloseableReasoner
    policy: Policy

    async def aclose(self) -> None:
        await self.reasoner.aclose()


def build_application(
    config: LoadedAgentConfig,
    *,
    show_progress: bool = True,
) -> Application:
    learning_config = config.config.learning
    selected_memory = build_memory(config)
    resolved_reasoner = resolve_reasoner_config(config)
    learner_config = None
    learning_trigger = None
    min_evidence_groups = None
    if learning_config is not None:
        configured_learner = config.config.reasoners.learner
        if configured_learner is None:  # pragma: no cover - config validation owns this invariant
            raise ValueError("learning requires a learner reasoner")
        learner_config = _core_reasoner_config(configured_learner)
        learning_trigger = learning_config.trigger
        min_evidence_groups = learning_config.min_evidence_groups
    reasoner = _build_reasoner(config, resolved_reasoner)
    store_root = config.config.run_store.path
    jsonl_store = JsonlRunStore(store_root)
    jsonl_store.ensure_writable()
    store: RunStore = jsonl_store
    environment_config = config.config.environment
    environment: ProgrammableLauncher
    sgr = CurrentSGR(operator_guidance=config.operator_guidance)
    environment = ProgrammableLauncher(
        UptickV2Environment(
            environment_config.endpoint,
            reasoner=reasoner,
            cache_directory=store_root / "discovery",
            participant_token_env=(
                environment_config.participant_token.from_env
                if environment_config.participant_token is not None
                else None
            ),
        )
    )
    if show_progress:
        store = ConsoleReportingRunStore(store)
    core = AgentCore(reasoner=reasoner, sgr=sgr)
    learner_reasoner: _CloseableReasoner | None = None
    learning = None
    if learner_config is not None:
        assert learning_trigger is not None
        assert min_evidence_groups is not None
        learner_reasoner = _build_reasoner(
            config,
            learner_config,
            role="learner",
        )
        learning = LearningOrchestrator(
            memory=selected_memory,
            run_store=store,
            consolidator=MemoryConsolidator(reasoner=learner_reasoner),
            trigger=learning_trigger,
            min_evidence_groups=min_evidence_groups,
        )
    runner = AgentRunner(
        agent_core=core,
        environment=environment,
        memory=selected_memory,
        run_store=store,
        policy=DecisionPolicy(),
        bootstrapper=ReasonerEnvironmentBootstrapper(reasoner=reasoner),
        learning=learning,
    )
    return Application(
        runner=runner,
        reasoner=reasoner,
        learner_reasoner=learner_reasoner,
        environment=environment,
        run_store=store,
        jsonl_store=jsonl_store,
        learning=learning,
    )


def build_decision_application(
    reasoner_config: ReasonerConfig, *, operator_guidance: str | None = None
) -> DecisionApplication:
    if reasoner_config.provider != "codex" or reasoner_config.thread_mode != "ephemeral":
        raise ValueError("live replay currently supports only Codex/ephemeral")
    constructor = _load_codex_reasoner()
    reasoner = constructor(config=reasoner_config)
    return DecisionApplication(
        agent_core=AgentCore(
            reasoner=reasoner, sgr=CurrentSGR(operator_guidance=operator_guidance)
        ),
        reasoner=reasoner,
        policy=DecisionPolicy(),
    )


def build_memory(config: LoadedAgentConfig) -> Memory:
    selected = config.config.memory
    if isinstance(selected, NoMemoryConfig):
        return NoMemory()
    if isinstance(selected, SQLiteMemoryConfig):
        return SQLiteMemory(selected.path)
    raise AssertionError(f"unsupported validated memory config {type(selected).__name__}")


def resolve_reasoner_config(config: LoadedAgentConfig) -> ReasonerConfig:
    return _core_reasoner_config(config.config.reasoners.decision)


async def run_uptick(
    config: LoadedAgentConfig,
    *,
    live: bool,
) -> RunResult:
    _require_live(live)
    application = build_application(config)
    try:
        result = await application.runner.run(
            _ad_hoc_run_spec(config, config.config.environment.seed)
        )
        return result
    finally:
        await application.aclose()


async def export_corpus(
    report: ExperimentReport,
    *,
    trace_path: Path,
) -> DecisionCorpus:
    return await export_decision_corpus(report, _source_run_store(trace_path))


async def export_ad_hoc_corpus(
    run_id: str,
    *,
    trace_path: Path,
) -> DecisionCorpus:
    return await export_ad_hoc_decision_corpus(run_id, _source_run_store(trace_path))


def _source_run_store(trace_path: Path) -> JsonlRunStore:
    if trace_path.name != "trace.jsonl":
        raise ValueError("trace path must name the run store's trace.jsonl")
    if not trace_path.is_file():
        raise ValueError(f"trace file does not exist: {trace_path}")
    return JsonlRunStore(trace_path.parent)


async def run_replay(
    *,
    corpus: DecisionCorpus,
    decision_id: str,
    reasoner_config: ReasonerConfig,
    repeats: int,
    live: bool,
) -> ReplayReport:
    _require_live(live)
    if reasoner_config.provider != "codex" or reasoner_config.thread_mode != "ephemeral":
        raise ValueError("live replay currently supports only Codex/ephemeral")
    source = corpus.find_decision(decision_id)
    source_run = next(run for run in corpus.runs if any(item is source for item in run.decisions))
    application = build_decision_application(
        reasoner_config, operator_guidance=source_run.manifest.operator_guidance
    )
    try:
        return await replay_decision(
            corpus,
            decision_id=decision_id,
            repeats=repeats,
            reasoner_config=reasoner_config,
            agent_core=application.agent_core,
            policy=application.policy,
        )
    finally:
        await application.aclose()


def _build_reasoner(
    config: LoadedAgentConfig,
    reasoner_config: ReasonerConfig,
    *,
    role: Literal["decision", "learner"] = "decision",
) -> _CloseableReasoner:
    if reasoner_config.provider == "openai":
        selected = (
            config.config.reasoners.decision
            if role == "decision"
            else config.config.reasoners.learner
        )
        if not isinstance(selected, OpenAIDecisionReasonerConfig):
            raise ValueError(
                f"effective OpenAI {role} reasoner requires OpenAI credentials in agent config"
            )
        if _core_reasoner_config(selected) != reasoner_config:
            raise ValueError(
                f"effective OpenAI {role} reasoner does not match the agent configuration"
            )
        return build_openai_reasoner(selected)
    if reasoner_config.provider == "codex":
        constructor = _load_codex_reasoner()
        return constructor(config=reasoner_config)
    raise ValueError(f"unsupported reasoner provider {reasoner_config.provider!r}")


def _core_reasoner_config(
    config: CodexDecisionReasonerConfig | OpenAIDecisionReasonerConfig,
) -> ReasonerConfig:
    return ReasonerConfig(
        provider=config.provider,
        model=config.model,
        effort=config.effort,
        thread_mode=config.thread_mode,
        timeout_seconds=config.timeout_seconds,
        retries=config.retries,
    )


def _load_codex_reasoner() -> _CodexReasonerConstructor:
    try:
        from uptick_agent.reasoners.codex import CodexReasoner
    except ModuleNotFoundError as error:
        if error.name == "openai_codex":
            raise RuntimeError(
                "Codex provider requires the optional dependency. "
                "Run `uv sync --extra codex` before using provider codex."
            ) from error
        raise
    return cast(_CodexReasonerConstructor, CodexReasoner)


def _require_live(live: bool) -> None:
    if not live:
        raise ValueError("live model/simulator access requires explicit live=True / --live")


def _ad_hoc_run_spec(config: LoadedAgentConfig, seed: int) -> RunSpec:
    git_revision, git_dirty, provenance_reasons = _git_provenance()
    selected = config.config
    return RunSpec(
        run_id=allocate_ad_hoc_run_id(),
        environment=selected.environment.adapter,
        objective=DEFAULT_OBJECTIVE,
        step_limit=selected.environment.step_limit,
        agent_id=selected.agent.id,
        agent_version=selected.agent.version,
        world_id=str(seed),
        parameters={"seed": seed},
        metadata=RunMetadata(
            reasoner=resolve_reasoner_config(config),
            memory_mode=selected.memory.backend,
            git_revision=git_revision,
            git_dirty=git_dirty,
            agent_config_source=config.source_id,
            agent_config_sha256=config.source_sha256,
            operator_guidance=config.operator_guidance,
            provenance_reasons=provenance_reasons,
        ),
    )


def _git_provenance() -> tuple[str | None, bool | None, list[str]]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(_PROJECT_DIRECTORY), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None, None, ["git revision unavailable"]
    if not revision:
        return None, None, ["git revision unavailable"]

    try:
        status = subprocess.run(
            ["git", "-C", str(_PROJECT_DIRECTORY), "status", "--porcelain", "--", "."],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return revision, None, ["git dirty status unavailable"]
    return revision, bool(status.strip()), []
