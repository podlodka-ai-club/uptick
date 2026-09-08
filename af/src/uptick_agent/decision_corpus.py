from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from uptick_agent.core.agent_core import AgentCore
from uptick_agent.core.contracts import Policy, RunStore
from uptick_agent.core.errors import ReasonerFailure
from uptick_agent.core.models import (
    AgentContext,
    FailureRecord,
    JsonObject,
    Observation,
    PolicyResult,
    ReasonerConfig,
    RunManifest,
    SGRDecision,
    StrictModel,
)
from uptick_agent.core.trace_models import (
    DecisionTracePayload,
    RunFailedPayload,
    RunFinishedPayload,
    run_stream_id,
)
from uptick_agent.experiments import ExperimentReport, ExperimentSpec


class CorpusDecision(StrictModel):
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int | None = None
    repeat_index: int | None = Field(default=None, ge=0)
    iteration: int = Field(ge=1)
    context: AgentContext
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: SGRDecision | None = None
    policy_result: PolicyResult | None = None
    following_observation: Observation | None = None
    failure: FailureRecord | None = None
    recalled_record_ids: list[str] = Field(default_factory=list)


class CorpusRun(StrictModel):
    seed: int | None = None
    repeat_index: int | None = Field(default=None, ge=0)
    run_id: str
    manifest: RunManifest
    final_outcome: JsonObject | None = None
    final_failure: FailureRecord | None = None
    decisions: list[CorpusDecision] = Field(default_factory=list)


class DecisionCorpus(StrictModel):
    schema_version: Literal[3] = 3
    spec: ExperimentSpec | None = None
    resolved_spec_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runs: list[CorpusRun]

    @model_validator(mode="after")
    def validate_provenance(self) -> DecisionCorpus:
        if (self.spec is None) != (self.resolved_spec_sha256 is None):
            raise ValueError("corpus spec and resolved_spec_sha256 must be present together")
        return self

    def find_decision(self, decision_id: str) -> CorpusDecision:
        matches = [
            decision
            for run in self.runs
            for decision in run.decisions
            if decision.decision_id == decision_id
        ]
        if len(matches) != 1:
            raise ValueError(f"decision_id must identify exactly one corpus item: {decision_id}")
        return matches[0]


class ReplayAttempt(StrictModel):
    attempt_index: int = Field(ge=0)
    decision: SGRDecision | None = None
    policy_result: PolicyResult | None = None
    failure: FailureRecord | None = None


class StabilityMetric(StrictModel):
    modal_count: int = Field(ge=0)
    modal_signatures: list[str] = Field(default_factory=list)
    unique_count: int = Field(ge=0)
    agreement_among_valid: float | None = Field(default=None, ge=0, le=1)
    agreement_among_requested: float | None = Field(default=None, ge=0, le=1)


class ReplayStability(StrictModel):
    requested: int = Field(ge=1)
    schema_valid: int = Field(ge=0)
    schema_failures: int = Field(ge=0)
    provider_failures: int = Field(ge=0)
    policy_rejections: int = Field(ge=0)
    capability_call: StabilityMetric
    behavioral_signature: StabilityMetric
    full_envelope: StabilityMetric


class ReplayReport(StrictModel):
    schema_version: Literal[2] = 2
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reasoner: ReasonerConfig
    attempts: list[ReplayAttempt]
    stability: ReplayStability


async def export_decision_corpus(
    report: ExperimentReport,
    run_store: RunStore,
) -> DecisionCorpus:
    if report.resolved_spec_sha256 != report.spec.sha256():
        raise ValueError("experiment report spec hash does not match its resolved spec")

    runs: list[CorpusRun] = []
    for attempt in report.attempts:
        if attempt.run_id is None:
            continue
        manifest = await run_store.load_manifest(attempt.run_id)
        if manifest is None:
            raise ValueError(f"manifest not found for run {attempt.run_id!r}")
        _validate_manifest_link(report, attempt.seed, attempt.repeat_index, manifest)
        runs.append(
            await _export_corpus_run(
                run_id=attempt.run_id,
                seed=attempt.seed,
                repeat_index=attempt.repeat_index,
                manifest=manifest,
                run_store=run_store,
                decision_anchor=report.resolved_spec_sha256,
            )
        )

    return DecisionCorpus(
        spec=report.spec,
        resolved_spec_sha256=report.resolved_spec_sha256,
        runs=runs,
    )


async def export_ad_hoc_corpus(run_id: str, run_store: RunStore) -> DecisionCorpus:
    manifest = await run_store.load_manifest(run_id)
    if manifest is None:
        raise ValueError(f"manifest not found for run {run_id!r}")
    if manifest.experiment_id is not None or manifest.resolved_spec_sha256 is not None:
        raise ValueError("ad-hoc corpus export cannot discard experiment provenance")
    run = await _export_corpus_run(
        run_id=run_id,
        seed=manifest.seed,
        repeat_index=manifest.repeat_index,
        manifest=manifest,
        run_store=run_store,
        decision_anchor=f"run:{run_id}",
    )
    return DecisionCorpus(runs=[run])


async def _export_corpus_run(
    *,
    run_id: str,
    seed: int | None,
    repeat_index: int | None,
    manifest: RunManifest,
    run_store: RunStore,
    decision_anchor: str,
) -> CorpusRun:
    if manifest.system_prompt_sha256 is None or manifest.schema_sha256 is None:
        raise ValueError(f"run {run_id!r} has no replayable decision hashes")
    events = await run_store.load_stream(run_stream_id(run_id))
    decisions: list[CorpusDecision] = []
    final_outcome: JsonObject | None = None
    final_failure: FailureRecord | None = None
    for event in events:
        if event.kind == "decision_trace":
            trace = DecisionTracePayload.model_validate(event.payload)
            context = AgentContext.model_validate(trace.context_projection)
            decisions.append(
                CorpusDecision(
                    decision_id=_decision_id(
                        decision_anchor,
                        seed,
                        repeat_index,
                        trace.iteration,
                        trace.context_sha256,
                    ),
                    seed=seed,
                    repeat_index=repeat_index,
                    iteration=trace.iteration,
                    context=context,
                    context_sha256=trace.context_sha256,
                    system_prompt_sha256=manifest.system_prompt_sha256,
                    schema_sha256=manifest.schema_sha256,
                    decision=trace.decision,
                    policy_result=trace.policy_result,
                    following_observation=trace.observation,
                    failure=trace.failure,
                    recalled_record_ids=(
                        trace.retrieval_diagnostics.selected_record_ids
                        if trace.retrieval_diagnostics is not None
                        else []
                    ),
                )
            )
        elif event.kind == "run_finished":
            finished = RunFinishedPayload.model_validate(event.payload)
            final_outcome = finished.result_details or finished.result.model_dump(mode="json")
        elif event.kind == "run_failed":
            failed = RunFailedPayload.model_validate(event.payload)
            final_failure = failed.failure
    if final_outcome is None and final_failure is None:
        raise ValueError(f"run {run_id!r} has no final outcome or failure event")
    return CorpusRun(
        seed=seed,
        repeat_index=repeat_index,
        run_id=run_id,
        manifest=manifest,
        final_outcome=final_outcome,
        final_failure=final_failure,
        decisions=decisions,
    )


async def replay_decision(
    corpus: DecisionCorpus,
    *,
    decision_id: str,
    repeats: int,
    reasoner_config: ReasonerConfig,
    agent_core: AgentCore,
    policy: Policy,
) -> ReplayReport:
    if repeats < 1:
        raise ValueError("replay repeats must be at least one")
    if (
        corpus.spec is not None
        and corpus.resolved_spec_sha256 is not None
        and corpus.resolved_spec_sha256 != corpus.spec.sha256()
    ):
        raise ValueError("decision corpus spec hash does not match its resolved spec")
    source = corpus.find_decision(decision_id)
    fingerprint = agent_core.fingerprint(source.context)
    if fingerprint.context_sha256 != source.context_sha256:
        raise ValueError("saved AgentContext hash does not match the current AgentCore input")
    if fingerprint.system_prompt_sha256 != source.system_prompt_sha256:
        raise ValueError("saved system prompt hash does not match the current AgentCore")
    if fingerprint.schema_sha256 != source.schema_sha256:
        raise ValueError("saved SGR schema hash does not match the current AgentCore")

    attempts: list[ReplayAttempt] = []
    schema_failures = 0
    provider_failures = 0
    for attempt_index in range(repeats):
        try:
            decision = await agent_core.decide(source.context.model_copy(deep=True))
        except Exception as error:
            failure, is_schema_failure = _replay_failure(error)
            if is_schema_failure:
                schema_failures += 1
            else:
                provider_failures += 1
            attempts.append(ReplayAttempt(attempt_index=attempt_index, failure=failure))
            continue
        policy_result = policy.validate(source.context, decision)
        attempts.append(
            ReplayAttempt(
                attempt_index=attempt_index,
                decision=decision,
                policy_result=policy_result,
            )
        )

    valid = [attempt for attempt in attempts if attempt.decision is not None]
    policy_rejections = sum(
        attempt.policy_result is not None and not attempt.policy_result.accepted
        for attempt in valid
    )
    call_signatures = [
        _canonical_json(attempt.decision.envelope.selected_action.model_dump(mode="json"))
        for attempt in valid
        if attempt.decision is not None
    ]
    behavioral_signatures = [
        _canonical_json(
            {
                "task_completed": attempt.decision.envelope.task_completed,
                "action": attempt.decision.envelope.selected_action.model_dump(mode="json"),
            }
        )
        for attempt in valid
        if attempt.decision is not None
    ]
    envelope_signatures = [
        _canonical_json(attempt.decision.envelope.model_dump(mode="json"))
        for attempt in valid
        if attempt.decision is not None
    ]
    schema_valid = len(valid)
    return ReplayReport(
        decision_id=decision_id,
        context_sha256=source.context_sha256,
        reasoner=reasoner_config,
        attempts=attempts,
        stability=ReplayStability(
            requested=repeats,
            schema_valid=schema_valid,
            schema_failures=schema_failures,
            provider_failures=provider_failures,
            policy_rejections=policy_rejections,
            capability_call=_stability_metric(call_signatures, requested=repeats),
            behavioral_signature=_stability_metric(behavioral_signatures, requested=repeats),
            full_envelope=_stability_metric(envelope_signatures, requested=repeats),
        ),
    )


def _validate_manifest_link(
    report: ExperimentReport,
    seed: int,
    repeat_index: int,
    manifest: RunManifest,
) -> None:
    if manifest.experiment_id != report.spec.experiment_id:
        raise ValueError("manifest experiment_id does not match the report")
    if manifest.seed != seed or manifest.repeat_index != repeat_index:
        raise ValueError("manifest run key does not match the report attempt")
    if manifest.resolved_spec_sha256 != report.resolved_spec_sha256:
        raise ValueError("manifest experiment spec hash does not match the report")


def _decision_id(
    anchor: str,
    seed: int | None,
    repeat_index: int | None,
    iteration: int,
    context_sha256: str,
) -> str:
    value = "\0".join([anchor, str(seed), str(repeat_index), str(iteration), context_sha256])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replay_failure(error: Exception) -> tuple[FailureRecord, bool]:
    if isinstance(error, ReasonerFailure):
        schema_failure = error.category == "invalid_output"
        return (
            FailureRecord(
                stage="replay_reasoner",
                error_type=type(error).__name__,
                message=str(error),
                category=error.category,
                telemetry=error.telemetry,
            ),
            schema_failure,
        )
    schema_failure = isinstance(error, (ValidationError, TypeError, ValueError))
    return (
        FailureRecord(
            stage="replay_reasoner",
            error_type=type(error).__name__,
            message=str(error),
            category="invalid_output" if schema_failure else "provider",
        ),
        schema_failure,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stability_metric(signatures: list[str], *, requested: int) -> StabilityMetric:
    if not signatures:
        return StabilityMetric(modal_count=0, unique_count=0)
    counts = Counter(signatures)
    modal_count = max(counts.values())
    modal_signatures = sorted(
        signature for signature, count in counts.items() if count == modal_count
    )
    if len(signatures) < 2:
        among_valid = None
        among_requested = None
    else:
        among_valid = modal_count / len(signatures)
        among_requested = modal_count / requested
    return StabilityMetric(
        modal_count=modal_count,
        modal_signatures=modal_signatures,
        unique_count=len(counts),
        agreement_among_valid=among_valid,
        agreement_among_requested=among_requested,
    )
