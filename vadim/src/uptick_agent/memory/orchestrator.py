"""Stage 3 composition root for optional memory modules.

This module intentionally contains no retrieval policy, learned behaviour,
storage implementation, environment adapter, or LLM provider.  It constructs
only configured modules and applies the stable contracts at their edges.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import Field, JsonValue

from uptick_agent.memory.audit_contracts import (
    AuditTraceEvent,
    AuditTraceSink,
    AuditTraceWrite,
    audit_event_id,
)
from uptick_agent.memory.config import MemoryConfiguration, ModuleConfig
from uptick_agent.memory.contracts import (
    ConsolidationParticipant,
    ConsolidationRequest,
    ConsolidationResult,
    ContextContributor,
    ContextItem,
    ContractModel,
    CreatedMemoryItem,
    DecisionMemoryContext,
    ExperienceSink,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
    RunFinalizer,
    RunOutcome,
)
from uptick_agent.memory.retrieval import RetrievalStrategy
from uptick_agent.memory.stores.contracts import canonical_json

_DEFAULT_ESTIMATOR_ID = "utf8-byte-upper-bound"
_DEFAULT_ESTIMATOR_VERSION = "1.0"


def _estimate_utf8_bytes(item: ContextItem) -> int:
    """Return the dependency-free deterministic estimator named in configuration."""

    payload = item.model_dump(mode="json", exclude={"estimated_tokens"})
    rendered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return max(1, len(rendered.encode("utf-8")))


@dataclass(frozen=True)
class MemoryModuleRegistration:
    """A composition-root registration; factories are never module imports.

    Retrieval is an optional read-side decorator.  Keeping it on the
    registration preserves the module object's write/finalize capabilities;
    wrapping a module object would silently drop those lifecycle methods.
    """

    module_id: str
    factory: Callable[[ModuleConfig], object]
    requires: tuple[str, ...] = ()
    retrieval_strategy: RetrievalStrategy | None = None


class MemoryContextDiagnostics(ContractModel):
    """Versioned selection trace, kept outside the prompt-facing context."""

    configuration_fingerprint: str = Field(min_length=64, max_length=64)
    resolved_configuration: dict[str, JsonValue]
    request_id: str | None = Field(default=None, max_length=256)
    effective_item_limit: int = Field(default=0, ge=0)
    effective_token_limit: int = Field(default=0, ge=0)
    used_items: int = Field(default=0, ge=0)
    used_estimated_tokens: int = Field(default=0, ge=0)
    estimator_id: str = Field(min_length=1, max_length=128)
    estimator_version: str = Field(min_length=1, max_length=64)
    contributors: list[str] = Field(default_factory=list)
    module_versions: dict[str, str] = Field(default_factory=dict)
    selection_evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    selected_item_ids: list[str] = Field(default_factory=list)
    truncations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MemoryModuleTelemetry(ContractModel):
    """Observed lifecycle calls for one constructed memory module."""

    module_id: str = Field(min_length=1, max_length=128)
    module_version: str = Field(min_length=1, max_length=64)
    construction_events: int = Field(default=0, ge=0)
    read_events: int = Field(default=0, ge=0)
    contribution_events: int = Field(default=0, ge=0)
    write_events: int = Field(default=0, ge=0)
    finalization_events: int = Field(default=0, ge=0)
    consolidation_events: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class _Candidate:
    module_id: str
    item: ContextItem

    @property
    def rank(self) -> tuple[float, str, str, str]:
        """The total order used for duplicate resolution and all truncation."""

        return (
            -self.item.score,
            self.module_id,
            self.item.envelope.item_id,
            self.item.selection_reason,
        )


class MemoryOrchestrator:
    """Construct and dispatch enabled modules through their narrow capabilities."""

    def __init__(
        self,
        configuration: MemoryConfiguration,
        registrations: Iterable[MemoryModuleRegistration],
        *,
        approval_verifier: Callable[[str, ModuleConfig, str], bool] | None = None,
        audit_sink: AuditTraceSink | None = None,
    ) -> None:
        # Keep the composition root on the same snapshot used to validate
        # modules and correlate audit events.  A caller may otherwise mutate
        # the configuration after construction and desynchronise the sink.
        self._configuration = configuration.model_copy(deep=True)
        budget = self._configuration.context_budget
        if (
            budget.estimator_id != _DEFAULT_ESTIMATOR_ID
            or budget.estimator_version != _DEFAULT_ESTIMATOR_VERSION
        ):
            raise MemoryValidationError(
                "configured memory token estimator is unavailable at composition time"
            )
        self._registrations = self._registration_map(registrations)
        self._validate_registration_dependencies()
        self._validate_approvals(approval_verifier)
        self._audit_sink = self._validate_audit_sink(audit_sink)
        self._modules = self._construct_enabled_modules()
        self._module_telemetry = {
            module_id: MemoryModuleTelemetry(
                module_id=module_id,
                module_version=self._configuration.modules[module_id].version,
                construction_events=1,
            )
            for module_id in self._modules
        }
        self._last_context_diagnostics = MemoryContextDiagnostics(
            configuration_fingerprint=self._configuration.fingerprint,
            resolved_configuration=self._configuration.model_dump(mode="json"),
            estimator_id=budget.estimator_id,
            estimator_version=budget.estimator_version,
        )

    def _validate_audit_sink(self, audit_sink: AuditTraceSink | None) -> AuditTraceSink | None:
        if not self._configuration.audit.enabled:
            if audit_sink is not None:
                raise MemoryValidationError(
                    "audit sink cannot be supplied while runtime audit is disabled"
                )
            return None
        if audit_sink is None:
            raise MemoryValidationError("enabled runtime audit requires an audit sink")
        if audit_sink.runtime_configuration_fingerprint != self.configuration_fingerprint:
            raise MemoryValidationError("audit sink uses another runtime configuration")
        if audit_sink.audit_configuration_fingerprint != self._configuration.audit.fingerprint:
            raise MemoryValidationError("audit sink uses another audit configuration")
        return audit_sink

    @property
    def configuration_fingerprint(self) -> str:
        return self._configuration.fingerprint

    @property
    def enabled_module_ids(self) -> tuple[str, ...]:
        return tuple(self._modules)

    @property
    def audit_sink(self) -> AuditTraceSink | None:
        return self._audit_sink

    @property
    def last_context_diagnostics(self) -> MemoryContextDiagnostics:
        return self._last_context_diagnostics.model_copy(deep=True)

    @property
    def module_telemetry(self) -> dict[str, MemoryModuleTelemetry]:
        return {
            module_id: telemetry.model_copy(deep=True)
            for module_id, telemetry in self._module_telemetry.items()
        }

    def _record_module_event(self, module_id: str, field_name: str) -> None:
        telemetry = self._module_telemetry[module_id]
        value = getattr(telemetry, field_name) + 1
        self._module_telemetry[module_id] = telemetry.model_copy(update={field_name: value})

    @staticmethod
    def _registration_map(
        registrations: Iterable[MemoryModuleRegistration],
    ) -> dict[str, MemoryModuleRegistration]:
        result: dict[str, MemoryModuleRegistration] = {}
        for registration in registrations:
            if (
                not registration.module_id
                or registration.module_id != registration.module_id.strip()
            ):
                raise MemoryValidationError("memory module registration requires module_id")
            if registration.module_id in result:
                raise MemoryValidationError(
                    f"duplicate memory module registration {registration.module_id}"
                )
            result[registration.module_id] = registration
        return result

    def _validate_registration_dependencies(self) -> None:
        configured = self._configuration.modules
        unknown_registrations = sorted(self._registrations.keys() - configured.keys())
        if unknown_registrations:
            raise MemoryValidationError(
                f"unknown memory module registrations: {', '.join(unknown_registrations)}"
            )
        enabled = {module_id for module_id, module in configured.items() if module.enabled}
        missing = sorted(enabled - self._registrations.keys())
        if missing:
            raise MemoryValidationError(
                f"enabled memory modules have no registration: {', '.join(missing)}"
            )
        for module_id in sorted(enabled):
            registration = self._registrations[module_id]
            unknown = sorted(set(registration.requires) - configured.keys())
            if unknown:
                raise MemoryValidationError(
                    f"module {module_id} has unknown dependencies: {', '.join(unknown)}"
                )
            disabled = sorted(set(registration.requires) - enabled)
            if disabled:
                raise MemoryValidationError(
                    f"module {module_id} requires disabled modules: {', '.join(disabled)}"
                )

    def _validate_approvals(
        self, approval_verifier: Callable[[str, ModuleConfig, str], bool] | None
    ) -> None:
        for module_id, module_config in sorted(self._configuration.modules.items()):
            if not module_config.enabled or module_config.status != "default":
                continue
            if approval_verifier is None:
                raise MemoryValidationError(
                    f"default memory module {module_id} requires an approval verifier"
                )
            try:
                approved = approval_verifier(
                    module_id,
                    module_config,
                    self.configuration_fingerprint,
                )
            except Exception as error:
                raise MemoryValidationError(
                    f"approval verification failed for memory module {module_id}"
                ) from error
            if not approved:
                raise MemoryValidationError(
                    f"approval record is invalid for memory module {module_id}"
                )

    def _construct_enabled_modules(self) -> dict[str, object]:
        modules: dict[str, object] = {}
        for module_id, module_config in sorted(self._configuration.modules.items()):
            if not module_config.enabled:
                continue
            try:
                modules[module_id] = self._registrations[module_id].factory(module_config)
            except Exception as error:  # Construction failures are configuration failures.
                raise MemoryValidationError(
                    f"could not construct enabled memory module {module_id}"
                ) from error
        return modules

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        """Retrieve, normalize and hard-bound decision memory deterministically."""

        global_items = self._effective_limit(
            self._configuration.context_budget.total_items, request.max_items
        )
        global_tokens = self._effective_limit(
            self._configuration.context_budget.total_tokens, request.max_estimated_tokens
        )
        if global_items == 0 or global_tokens == 0:
            self._last_context_diagnostics = MemoryContextDiagnostics(
                configuration_fingerprint=self.configuration_fingerprint,
                resolved_configuration=self._configuration.model_dump(mode="json"),
                request_id=request.request_id,
                effective_item_limit=global_items,
                effective_token_limit=global_tokens,
                estimator_id=self._configuration.context_budget.estimator_id,
                estimator_version=self._configuration.context_budget.estimator_version,
                warnings=["memory.context_budget_exhausted"],
            )
            await self._record_context_trace(request, self._last_context_diagnostics)
            return DecisionMemoryContext(warnings=["memory.context_budget_exhausted"])

        contributions: list[MemoryContribution] = []
        warnings: list[str] = []
        for module_id, module in self._modules.items():
            if not isinstance(module, ContextContributor):
                continue
            self._record_module_event(module_id, "read_events")
            try:
                contribution = await module.retrieve(request)
                strategy = self._registrations[module_id].retrieval_strategy
                if strategy is not None:
                    # Validate before handing candidates to a strategy.  The
                    # strategy may rerank or filter admitted items, but it
                    # must not become a second admission boundary.
                    self._validate_contribution(module_id, contribution)
                    contribution = contribution.model_copy(
                        update={
                            "items": [
                                self._with_verified_token_estimate(item)
                                for item in contribution.items
                            ]
                        }
                    )
                    original_envelopes = Counter(
                        self._envelope_key(item) for item in contribution.items
                    )
                    ranked = strategy.rank(contribution.items, request)
                    if inspect.isawaitable(ranked):
                        ranked = await ranked
                    if not isinstance(ranked, list) or not all(
                        isinstance(item, ContextItem) for item in ranked
                    ):
                        raise MemoryPermanentError(
                            f"retrieval strategy {module_id} returned invalid candidates"
                        )
                    owned_ranked: list[ContextItem] = []
                    for item in ranked:
                        try:
                            owned_ranked.append(
                                ContextItem.model_validate(
                                    item.model_dump(
                                        mode="python",
                                        round_trip=True,
                                        warnings="error",
                                    )
                                )
                            )
                        except (AttributeError, TypeError, ValueError) as error:
                            raise MemoryPermanentError(
                                f"retrieval strategy {module_id} returned an invalid item"
                            ) from error
                    ranked = owned_ranked
                    ranked_envelopes = Counter(self._envelope_key(item) for item in ranked)
                    if any(
                        ranked_count > original_envelopes[envelope]
                        for envelope, ranked_count in ranked_envelopes.items()
                    ):
                        raise MemoryPermanentError(
                            f"retrieval strategy {module_id} changed or invented an envelope"
                        )
                    contribution = contribution.model_copy(update={"items": ranked})
                self._validate_contribution(module_id, contribution)
            except MemoryTransientError as error:
                warnings.append(f"memory.module_failed.{module_id}.{type(error).__name__}")
                continue
            except (MemoryValidationError, MemoryConflictError, MemoryPermanentError):
                raise
            except Exception as error:
                raise MemoryPermanentError(
                    f"context contributor {module_id} failed unexpectedly"
                ) from error
            contributions.append(contribution)
            # Count only validated nonempty contributions entering the global merge.
            if contribution.items:
                self._record_module_event(module_id, "contribution_events")
            warnings.extend(f"{module_id}:{warning}" for warning in contribution.warnings)

        context, diagnostics = self._merge_contributions(
            contributions,
            global_items=global_items,
            global_tokens=global_tokens,
            request_id=request.request_id,
            warnings=warnings,
        )
        self._last_context_diagnostics = diagnostics
        await self._record_context_trace(request, diagnostics)
        return context

    async def _record_context_trace(
        self,
        request: MemoryContextRequest,
        diagnostics: MemoryContextDiagnostics,
    ) -> None:
        if self._audit_sink is None:
            return
        iteration_value = request.context.get("iteration")
        iteration = (
            iteration_value
            if isinstance(iteration_value, int)
            and not isinstance(iteration_value, bool)
            and iteration_value >= 1
            else None
        )
        await self._audit_sink.record(
            AuditTraceWrite(
                event_id=audit_event_id(
                    "memory.context_selected",
                    self.configuration_fingerprint,
                    request.run_id,
                    request.request_id,
                ),
                event_type="memory.context_selected",
                run_id=request.run_id,
                sequence=(iteration - 1) * 100 + 10 if iteration is not None else 10,
                iteration=iteration,
                request_id=request.request_id,
                producer_id="memory-orchestrator",
                producer_version="1.0",
                metadata=diagnostics.model_dump(
                    mode="json",
                    exclude={
                        "configuration_fingerprint",
                        "request_id",
                        "resolved_configuration",
                    },
                ),
                raw_bodies={"decision_traces": diagnostics.model_dump(mode="json")},
            )
        )

    @staticmethod
    def _effective_limit(configured: int, requested: int | None) -> int:
        return configured if requested is None else min(configured, requested)

    def _validate_contribution(self, expected_module_id: str, contribution: object) -> None:
        if not isinstance(contribution, MemoryContribution):
            raise MemoryPermanentError("context contributor returned a non-contract contribution")
        configured = self._configuration.modules[expected_module_id]
        if contribution.module_id != expected_module_id:
            raise MemoryPermanentError("context contributor returned another module's contribution")
        if contribution.module_version != configured.version:
            raise MemoryPermanentError(
                "context contributor version does not match resolved configuration"
            )
        for item in contribution.items:
            if item.envelope.origin_module != expected_module_id:
                raise MemoryPermanentError(
                    "context item origin does not match its contributing module"
                )
            if item.envelope.origin_version != configured.version:
                raise MemoryPermanentError(
                    "context item version does not match resolved configuration"
                )

    @staticmethod
    def _envelope_key(item: ContextItem) -> str:
        """Canonical envelope identity used to police read-side strategies."""

        return canonical_json(item.envelope.model_dump(mode="json"))

    def _merge_contributions(
        self,
        contributions: list[MemoryContribution],
        *,
        global_items: int,
        global_tokens: int,
        request_id: str,
        warnings: list[str],
    ) -> tuple[DecisionMemoryContext, MemoryContextDiagnostics]:
        normalized_contributions = [
            contribution.model_copy(
                update={
                    "items": [
                        self._with_verified_token_estimate(item) for item in contribution.items
                    ]
                }
            )
            for contribution in contributions
        ]
        candidates = sorted(
            (
                _Candidate(contribution.module_id, item)
                for contribution in normalized_contributions
                for item in contribution.items
            ),
            key=lambda candidate: candidate.rank,
        )
        accepted_ids: set[str] = set()
        selected: list[ContextItem] = []
        truncations: list[str] = []
        module_item_count: dict[str, int] = {}
        module_tokens: dict[str, int] = {}
        type_tokens: dict[str, int] = {}
        selection_evidence: list[dict[str, JsonValue]] = []
        used_tokens = 0

        for candidate in candidates:
            module_id = candidate.module_id
            item = candidate.item
            item_id = item.envelope.item_id
            if item_id in accepted_ids:
                reason = "duplicate"
                truncations.append(f"{module_id}:{item_id}:{reason}")
                selection_evidence.append(self._selection_evidence(candidate, outcome=reason))
                continue

            module_config = self._configuration.modules[module_id]
            item_count = module_item_count.get(module_id, 0)
            module_used_tokens = module_tokens.get(module_id, 0)
            artefact_type = item.envelope.artefact_type
            type_used_tokens = type_tokens.get(artefact_type, 0)
            type_limit = self._configuration.context_budget.per_type_tokens.get(artefact_type)
            reason: str | None = None
            if item_count >= module_config.max_context_items:
                reason = "module_item_limit"
            elif module_used_tokens + item.estimated_tokens > module_config.max_context_tokens:
                reason = "module_token_limit"
            elif type_limit is not None and type_used_tokens + item.estimated_tokens > type_limit:
                reason = "type_token_limit"
            elif len(selected) >= global_items:
                reason = "global_item_limit"
            elif used_tokens + item.estimated_tokens > global_tokens:
                reason = "global_token_limit"
            if reason is not None:
                truncations.append(f"{module_id}:{item_id}:{reason}")
                selection_evidence.append(self._selection_evidence(candidate, outcome=reason))
                continue

            accepted_ids.add(item_id)
            selected.append(item)
            module_item_count[module_id] = item_count + 1
            module_tokens[module_id] = module_used_tokens + item.estimated_tokens
            type_tokens[artefact_type] = type_used_tokens + item.estimated_tokens
            used_tokens += item.estimated_tokens
            selection_evidence.append(self._selection_evidence(candidate, outcome="selected"))

        deterministic_warnings = sorted(set(warnings))
        diagnostics = MemoryContextDiagnostics(
            configuration_fingerprint=self.configuration_fingerprint,
            resolved_configuration=self._configuration.model_dump(mode="json"),
            request_id=request_id,
            effective_item_limit=global_items,
            effective_token_limit=global_tokens,
            used_items=len(selected),
            used_estimated_tokens=used_tokens,
            estimator_id=self._configuration.context_budget.estimator_id,
            estimator_version=self._configuration.context_budget.estimator_version,
            contributors=sorted(
                contribution.module_id for contribution in normalized_contributions
            ),
            module_versions={
                contribution.module_id: contribution.module_version
                for contribution in sorted(
                    normalized_contributions, key=lambda contribution: contribution.module_id
                )
            },
            selection_evidence=selection_evidence,
            selected_item_ids=[item.envelope.item_id for item in selected],
            truncations=truncations,
            warnings=deterministic_warnings,
        )
        return DecisionMemoryContext(items=selected, warnings=deterministic_warnings), diagnostics

    def _with_verified_token_estimate(self, item: ContextItem) -> ContextItem:
        try:
            estimated_tokens = _estimate_utf8_bytes(item)
        except Exception as error:
            raise MemoryPermanentError("memory token estimator failed") from error
        return item.model_copy(update={"estimated_tokens": estimated_tokens})

    @staticmethod
    def _selection_evidence(candidate: _Candidate, *, outcome: str) -> dict[str, JsonValue]:
        item = candidate.item
        return {
            "module_id": candidate.module_id,
            "module_version": item.envelope.origin_version,
            "item_id": item.envelope.item_id,
            "score": item.score,
            "selection_reason": item.selection_reason,
            "estimated_tokens": item.estimated_tokens,
            "outcome": outcome,
        }

    async def record_transition(self, transition: ExperienceTransition) -> None:
        for module_index, (module_id, module) in enumerate(self._modules.items()):
            if not isinstance(module, ExperienceSink):
                continue
            idempotency_key = self._operation_key(
                "record", module_id, transition.model_dump(mode="json")
            )
            for attempt in range(2):
                try:
                    self._record_module_event(module_id, "write_events")
                    receipts = await module.record(transition, idempotency_key=idempotency_key)
                    break
                except MemoryTransientError:
                    if attempt == 1:
                        raise
            if receipts is None:
                continue
            if not isinstance(receipts, list):
                raise MemoryPermanentError(
                    f"experience sink {module_id} returned invalid created-item receipts"
                )
            module_config = self._configuration.modules[module_id]
            owned_receipts: list[CreatedMemoryItem] = []
            for receipt in receipts:
                try:
                    owned_receipt = CreatedMemoryItem.model_validate(
                        receipt.model_dump(mode="python", round_trip=True, warnings="error")
                    )
                except (AttributeError, TypeError, ValueError) as error:
                    raise MemoryPermanentError(
                        f"experience sink {module_id} returned an invalid created-item receipt"
                    ) from error
                owned_receipts.append(owned_receipt)
            if self._audit_sink is None:
                continue
            for owned_receipt in owned_receipts:
                await self._audit_sink.record(
                    AuditTraceWrite(
                        event_id=audit_event_id(
                            "memory.item_created",
                            self.configuration_fingerprint,
                            module_id,
                            transition.transition_id,
                            owned_receipt.item_id,
                        ),
                        event_type="memory.item_created",
                        run_id=transition.run_id,
                        sequence=(transition.iteration - 1) * 100 + 40 + module_index,
                        iteration=transition.iteration,
                        transition_id=transition.transition_id,
                        producer_id=module_id,
                        producer_version=module_config.version,
                        metadata={
                            "artefact_type": owned_receipt.artefact_type,
                            "item_id": owned_receipt.item_id,
                            "module_id": module_id,
                            "module_version": module_config.version,
                            "provenance": [
                                item.model_dump(mode="json") for item in owned_receipt.provenance
                            ],
                        },
                        raw_bodies={"decision_traces": owned_receipt.model_dump(mode="json")},
                    )
                )

    async def finalize_run(self, outcome: RunOutcome) -> None:
        """Record the runner-observed outcome before module finalizers.

        The audit event describes what the runner observed. It does not make
        module finalization across independent stores an atomic operation.
        """

        if self._audit_sink is not None:
            outcome_correlation_id = audit_event_id("run.outcome", outcome.run_id)
            await self._audit_sink.record(
                AuditTraceWrite(
                    event_id=audit_event_id(
                        "run.outcome",
                        self.configuration_fingerprint,
                        outcome_correlation_id,
                    ),
                    event_type="run.outcome",
                    run_id=outcome.run_id,
                    sequence=2_000_000_000,
                    outcome_correlation_id=outcome_correlation_id,
                    producer_id="memory-orchestrator",
                    producer_version="1.0",
                    metadata={
                        "status": outcome.status,
                        "objective_metrics": [
                            item.model_dump(mode="json") for item in outcome.objective_metrics
                        ],
                        "outcome_semantics": "runner-observed-before-module-finalizers",
                    },
                    raw_bodies={"decision_traces": outcome.model_dump(mode="json")},
                )
            )
        for module_id, module in self._modules.items():
            if not isinstance(module, RunFinalizer):
                continue
            idempotency_key = self._operation_key(
                "finalize", module_id, outcome.model_dump(mode="json")
            )
            for attempt in range(2):
                try:
                    self._record_module_event(module_id, "finalization_events")
                    await module.finalize(outcome, idempotency_key=idempotency_key)
                    break
                except MemoryTransientError:
                    if attempt == 1:
                        raise

    async def record_trace(self, write: AuditTraceWrite) -> AuditTraceEvent | None:
        if self._audit_sink is None:
            return None
        return await self._audit_sink.record(write)

    async def consolidate(self, request: ConsolidationRequest) -> ConsolidationResult:
        """Run no automatic consolidation; explicit requests use only its capability."""

        if not self._configuration.consolidation.enabled:
            return ConsolidationResult(
                request_id=request.request_id,
                snapshot_id=request.snapshot_id,
                applied=False,
            )

        deltas = []
        applied = False
        for module_id, module in self._modules.items():
            if not isinstance(module, ConsolidationParticipant):
                continue
            self._record_module_event(module_id, "consolidation_events")
            result = await module.consolidate(request)
            if result.request_id != request.request_id or result.snapshot_id != request.snapshot_id:
                raise MemoryPermanentError(
                    f"consolidation participant {module_id} returned a result for another request"
                )
            deltas.extend(result.deltas)
            applied = applied or result.applied
        return ConsolidationResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot_id,
            applied=applied,
            deltas=deltas,
        )

    @staticmethod
    def _operation_key(operation: str, module_id: str, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{operation}:{module_id}:{digest}"
