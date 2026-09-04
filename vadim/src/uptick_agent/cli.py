from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from uptick_agent.experiments import ExperimentRunner
from uptick_agent.llm import (
    LlmClient,
    LlmMessage,
    LlmProviderConfig,
    LlmProviderFactory,
    LlmProviderRegistry,
    OpenAIProviderFactory,
    StructuredGenerationRequest,
    serialize_structured_generation_request,
)
from uptick_agent.llm.prompts import DEFAULT_SYSTEM_PROMPT, V2_OBJECTIVE, V2_SYSTEM_PROMPT
from uptick_agent.memory import InMemoryMemory, JsonlMemory, legacy_memory_runtime
from uptick_agent.models import AgentConfig, DecisionContext, NextStep, V1NextStep, V2NextStep
from uptick_agent.observers import CompositeObserver, ConsoleObserver, JsonlObserver
from uptick_agent.ports import AgentMemory, DecisionModel, Environment
from uptick_agent.runner import AgentRunner
from uptick_agent.simulator import SimulatorClient, SimulatorEnvironment


class CloseableDecisionModel(DecisionModel, Protocol):
    async def aclose(self) -> None: ...


class CodexFactoryConstructor(Protocol):
    def __call__(self) -> LlmProviderFactory: ...


class StructuredDecisionModel:
    """Compatibility bridge from the neutral LLM boundary to the current runner."""

    def __init__(
        self,
        client: LlmClient,
        *,
        response_model: type[NextStep] = NextStep,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self._client = client
        self.model = getattr(client, "model", None)
        self.response_model = response_model
        self.system_prompt = system_prompt

    async def decide(self, context: DecisionContext) -> NextStep:
        result = await self._client.generate_structured(self._build_request(context))
        return result.value

    def prompt_trace(self, context: DecisionContext) -> dict[str, Any]:
        """Serialize the exact neutral request submitted by ``decide``."""
        return serialize_structured_generation_request(self._build_request(context))

    def _build_request(self, context: DecisionContext) -> StructuredGenerationRequest[NextStep]:
        return StructuredGenerationRequest(
            model=self.model,
            response_model=self.response_model,
            messages=(
                LlmMessage(role="system", content=self.system_prompt),
                LlmMessage(
                    role="user",
                    content=(
                        "Choose the next action from this runtime context. JSON follows:\n"
                        + context.model_dump_json(indent=2)
                    ),
                ),
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


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


def _decision_model(args: argparse.Namespace) -> CloseableDecisionModel:
    if args.decision_provider not in {"openai", "codex"}:
        raise ValueError(f"Unsupported decision provider {args.decision_provider!r}.")

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
    client = registry.create(LlmProviderConfig(provider=args.decision_provider, model=model))
    if getattr(args, "simulator_api_version", "v2") == "v1":
        return StructuredDecisionModel(
            client,
            response_model=V1NextStep,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
        )
    return StructuredDecisionModel(
        client,
        response_model=V2NextStep,
        system_prompt=V2_SYSTEM_PROMPT,
    )


async def _main(args) -> int:
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
    if api_version == "v2":
        config_values["objective"] = V2_OBJECTIVE
    config = AgentConfig(**config_values)
    seeds: list[int] | None = None
    if args.command == "benchmark":
        seeds = [int(value.strip()) for value in args.seeds.split(",") if value.strip()]
        if not seeds:
            raise ValueError("at least one seed is required")
        if 0 in seeds:
            raise ValueError("simulator seed 0 is invalid")
    model: CloseableDecisionModel | None = None
    client: Any | None = None
    try:
        model = _decision_model(args)
        assert model is not None
        if api_version == "v1":
            client = SimulatorClient(args.simulator_url)
            environment = cast(Environment, SimulatorEnvironment(client))
        else:
            from uptick_agent.simulator.v2_client import SimulatorV2Client
            from uptick_agent.simulator.v2_environment import SimulatorV2Environment

            client = SimulatorV2Client(args.simulator_url)
            environment = cast(Environment, SimulatorV2Environment(client))
        memory_factory = _memory_factory(args)

        def make_runner() -> AgentRunner:
            observer = CompositeObserver(
                ConsoleObserver(),
                JsonlObserver(args.artifacts / _trace_name(args) / "trace.jsonl"),
            )
            return AgentRunner(
                config=config,
                model=model,
                memory=memory_factory(),
                environment=environment,
                observer=observer,
            )

        if args.command == "run":
            result = await make_runner().run(args.seed)
            print(result.model_dump_json(indent=2))
        else:
            assert seeds is not None
            result = await ExperimentRunner(make_runner).run(
                name=args.name,
                seeds=seeds,
                carry_memory=args.carry_memory,
            )
            destination = args.artifacts / args.name / "summary.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            print(result.model_dump_json(indent=2))
    finally:
        if model is not None:
            await model.aclose()
        if client is not None:
            await client.aclose()
    return 0


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
