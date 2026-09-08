from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from uptick_agent.composition import (
    export_ad_hoc_corpus,
    export_corpus,
    run_replay,
    run_uptick,
)
from uptick_agent.config import AgentConfigError, load_agent_config
from uptick_agent.core.models import ReasonerConfig
from uptick_agent.decision_corpus import DecisionCorpus
from uptick_agent.experiments import (
    ExperimentReport,
    summarize_comparison,
)


def _add_live(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly allow live model and simulator access",
    )


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uptick-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the configured agent once")
    _add_config(run)
    _add_live(run)

    compare_reports = subparsers.add_parser(
        "compare-reports", help="compare two completed experiment reports offline"
    )
    compare_reports.add_argument("--name", required=True)
    compare_reports.add_argument("--baseline-report", type=Path, required=True)
    compare_reports.add_argument("--candidate-report", type=Path, required=True)
    compare_reports.add_argument("--output", type=Path, required=True)

    corpus = subparsers.add_parser("export-corpus", help="export decision data offline")
    corpus_source = corpus.add_mutually_exclusive_group(required=True)
    corpus_source.add_argument("--report", type=Path)
    corpus_source.add_argument("--run-id")
    corpus.add_argument("--trace", type=Path, required=True)
    corpus.add_argument("--output", type=Path, required=True)

    replay = subparsers.add_parser("replay", help="replay one saved AgentContext")
    replay.add_argument("--corpus", type=Path, required=True)
    replay.add_argument("--decision-id", required=True)
    replay.add_argument("--reasoner-config", type=Path, required=True)
    replay.add_argument("--repeats", type=int, required=True)
    replay.add_argument("--output", type=Path, required=True)
    _add_live(replay)
    return parser


def _safe_artifact_id(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
        raise ValueError("artifact ID must be a safe path component")
    return value


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _require_cli_live(live: bool) -> None:
    if not live:
        raise ValueError("live model/simulator access requires explicit --live")


async def _main(args: argparse.Namespace) -> int:
    if args.command == "run":
        config = load_agent_config(args.config)
        result = await run_uptick(config, live=args.live)
        print(result.model_dump_json(indent=2))
        return 0

    if args.command == "compare-reports":
        _safe_artifact_id(args.name)
        baseline = ExperimentReport.model_validate_json(
            args.baseline_report.read_text(encoding="utf-8")
        )
        candidate = ExperimentReport.model_validate_json(
            args.candidate_report.read_text(encoding="utf-8")
        )
        report = summarize_comparison(
            comparison_id=args.name,
            baseline=baseline,
            candidate=candidate,
            mode="offline_uncontrolled",
        )
        _write_new(args.output, report.model_dump_json(indent=2))
        print(report.model_dump_json(indent=2))
        return 2 if report.has_failures else 0

    if args.command == "export-corpus":
        if args.report is not None:
            report = ExperimentReport.model_validate_json(args.report.read_text(encoding="utf-8"))
            corpus = await export_corpus(report, trace_path=args.trace)
        else:
            corpus = await export_ad_hoc_corpus(args.run_id, trace_path=args.trace)
        _write_new(args.output, corpus.model_dump_json(indent=2))
        print(corpus.model_dump_json(indent=2))
        return 0

    if args.command == "replay":
        corpus = DecisionCorpus.model_validate_json(args.corpus.read_text(encoding="utf-8"))
        reasoner = ReasonerConfig.model_validate_json(
            args.reasoner_config.read_text(encoding="utf-8")
        )
        _require_cli_live(args.live)
        if reasoner.provider != "codex" or reasoner.thread_mode != "ephemeral":
            raise ValueError("live replay currently supports only Codex/ephemeral")
        report = await run_replay(
            corpus=corpus,
            decision_id=args.decision_id,
            reasoner_config=reasoner,
            repeats=args.repeats,
            live=args.live,
        )
        _write_new(args.output, report.model_dump_json(indent=2))
        print(report.model_dump_json(indent=2))
        return 0

    raise AssertionError(f"unsupported command {args.command!r}")


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        status = asyncio.run(_main(args))
    except AgentConfigError as error:
        parser.error(str(error))
    raise SystemExit(status)


if __name__ == "__main__":
    main()
