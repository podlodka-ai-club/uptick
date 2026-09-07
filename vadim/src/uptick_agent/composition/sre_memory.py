"""Public SRE operation outcomes projected into descriptive memory.

Adapter semantics live here; the generic memory modules do not know server
commands, tariffs, or simulator identities. No hidden simulator state is read.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from uptick_agent.memory.contracts import (
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryContextRequest,
    TransitionAssemblyRequest,
)
from uptick_agent.memory.lesson_contracts import LessonEvidence
from uptick_agent.memory.operation_chains import extract_operation_chains
from uptick_agent.memory.orchestrator import _estimate_utf8_bytes
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.stores.contracts import sha256_json
from uptick_agent.transition_assembly import DefaultExperienceTransitionAssembler

SRE_OUTCOME_SETTINGS = PatternQuerySettings(
    scope_paths=("observation.role", "observation.instance_type", "observation.other_mutations"),
    action_path="action.kind",
    result_path="result.hourly_cost_delta_minor",
)


def public_operation_status(transition: ExperienceTransition, operation_id: str) -> str | None:
    data = transition.result.get("data")
    if transition.result.get("ok") is not True or not isinstance(data, dict):
        return None
    if data.get("operation_id") != operation_id:
        return None
    status = data.get("status")
    if status in ("succeeded", "failed"):
        return status
    return "pending" if status in ("queued", "running", "pending") else None


def project_sre_outcomes(
    evidence: LessonEvidence, cutoffs: dict[str, datetime]
) -> list[ExperienceTransition]:
    """Join completed public operations to bounded before/after cost samples.

    Incomplete operations and missing samples produce no projection. IDs depend
    on the operation and metric, not the growing snapshot, so replay cannot
    manufacture additional support for the same observed outcome.
    """
    records = {r.record_id: r for r in evidence.records}
    transitions = {
        r.record_id: ExperienceTransition.model_validate(r.payload)
        for r in evidence.records
        if r.record_type == "experience-transition"
    }
    chains = extract_operation_chains(
        evidence,
        learning_cutoffs=cutoffs,
        max_iteration_gap=32,
        max_operation_iteration_gap=512,
        max_metric_boundary_gap=32,
        resolve_status=public_operation_status,
    )
    ordered = sorted(transitions.values(), key=lambda t: (t.occurred_at, t.transition_id))
    assembler = DefaultExperienceTransitionAssembler()
    projected = []
    for chain in chains:
        if chain.completion_status != "succeeded":
            continue
        initial = transitions[chain.initiation_record.record_id]
        request = initial.action.get("request", {})
        command = request.get("command")
        if command not in {"server.create", "server.delete"}:
            continue
        relation = next(
            (r for r in chain.metric_relations if r.metric_name == "current_cost_per_hour_minor"),
            None,
        )
        if relation is None:
            continue
        params = request.get("params", {})
        role, instance = params.get("role"), params.get("instance_type")
        descriptors = []
        if command == "server.delete":
            for previous in ordered:
                if (
                    previous.run_id != initial.run_id
                    or previous.occurred_at >= initial.occurred_at
                    or not initial.iteration - 32 <= previous.iteration <= initial.iteration
                ):
                    continue
                data = previous.result.get("data", {})
                if previous.result.get("ok") is not True or not isinstance(data, dict):
                    continue
                for server in data.get("servers", []):
                    if server.get("server_id") == params.get("server_id"):
                        role, instance = server.get("role"), server.get("instance_type")
                        record = records[previous.transition_id]
                        descriptors = [
                            {"record_id": record.record_id, "content_hash": record.content_hash}
                        ]
        if not isinstance(role, str) or not isinstance(instance, str):
            continue
        refs = [r.model_dump(mode="json") for r in relation.interval_records]
        mutations = []
        for ref in relation.interval_records[1:]:
            transition = transitions[ref.record_id]
            other = transition.action.get("request", {}).get("command", "")
            if (
                transition.transition_id != initial.transition_id
                and transition.action.get("kind") == "control_command"
                and other
                and not other.endswith((".get", ".list"))
            ):
                mutations.append(ref.record_id)
        sources = [transitions[r["record_id"]] for r in refs + descriptors]
        last = max(sources, key=lambda t: (t.occurred_at, t.iteration))
        identity = sha256_json(
            {
                "run_id": initial.run_id,
                "operation": chain.operation_id,
                "metric": relation.metric_name,
                "unit": relation.metric_unit,
            }
        )
        projected.append(
            assembler.assemble(
                TransitionAssemblyRequest(
                    transition_id="sre-outcome:" + identity,
                    run_id=initial.run_id,
                    iteration=max(t.iteration for t in sources),
                    occurred_at=last.occurred_at,
                    environment_id=initial.environment_id,
                    scenario_id=initial.scenario_id,
                    trust_classification="derived_untrusted",
                    observation={
                        "role": role,
                        "instance_type": instance,
                        "other_mutations": bool(mutations),
                    },
                    action={"kind": command},
                    result={
                        "hourly_cost_delta_minor": relation.after - relation.before,
                        "operation_status": "succeeded",
                        "source_operation_id": chain.operation_id,
                        "source_interval": refs,
                        "descriptor_sources": descriptors,
                        "other_mutation_ids": mutations,
                        "causal_credit": False,
                        "projection_not_environment_execution": True,
                    },
                    terminal=False,
                )
            )
        )
    return projected


class SreObservedMemory:
    """Runner adapter: online writes plus bounded, temporally scoped recall."""

    def __init__(
        self,
        learning: Any,
        reader: Any,
        *,
        budget_bytes: int = 8000,
        mode: str = "world",
        base_runtime: Any | None = None,
        capacity_reader: Any | None = None,
        learning_layers: dict[str, Any] | None = None,
    ) -> None:
        self.learning = learning
        self.capacity_reader = capacity_reader
        self.learning_layers = learning_layers or {"cost": learning}
        if isinstance(reader, (str, bytes)):
            raise TypeError("SRE observed reader must be an object or sequence of objects")
        readers = tuple(reader) if isinstance(reader, Sequence) else (reader,)
        if not readers or any(reader is None for reader in readers):
            raise ValueError("SRE online memory requires at least one observed reader")
        self.readers = readers
        # Keep the original singular attribute for callers of world/lessons mode.
        self.reader = readers[0]
        self.budget_bytes = budget_bytes
        self.mode = mode
        self._base_runtime = base_runtime or getattr(learning, "_base", None)
        self._diagnostics: dict = {}
        self._consolidation_diagnostics: dict | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.learning, name)

    @property
    def configuration(self) -> Any:
        """Return the immutable effective configuration used by the base runtime."""

        configuration = getattr(self._base_runtime, "configuration", None)
        if configuration is None:
            raise AttributeError("SRE online memory has no effective configuration")
        return configuration

    @property
    def effective_configuration(self) -> Any:
        return self.configuration

    @property
    def enabled_module_ids(self) -> tuple[str, ...]:
        modules = getattr(self._base_runtime, "enabled_module_ids", ())
        return tuple(modules)

    @property
    def module_telemetry(self) -> dict[str, Any]:
        telemetry = getattr(self._base_runtime, "module_telemetry", {})
        return dict(telemetry)

    @property
    def context_diagnostics(self) -> dict:
        base = self.learning.context_diagnostics
        if not isinstance(base, dict):
            base = {}
        reader_modules = [self._reader_module_id(reader) for reader in self.readers]
        active = [
            "episodic",
            "online_learning",
            *reader_modules,
        ]
        if self.capacity_reader is not None:
            active.append("capacity_observations")
        gated = [
            "strict_promotion",
            "causal_credit",
            "observed_recommendation",
        ]
        return {
            **base,
            "online_learning_by_layer": {
                name: layer.online_learning_diagnostics
                for name, layer in self.learning_layers.items()
            },
            "online_recall": self._diagnostics,
            "consolidation": self._consolidation_diagnostics,
            "sre_capabilities": {
                "mode": self.mode,
                "active": active,
                "gated": gated,
                "observed_readers": reader_modules,
                "capacity_observations_enabled": self.capacity_reader is not None,
                "strict_modules": list(self.enabled_module_ids),
            },
        }

    async def learn_persisted_run(self, run_id: str) -> None:
        for layer in self.learning_layers.values():
            await layer.learn_persisted_run(run_id)

    async def consolidate_after_finalize(self, outcome: Any) -> Any | None:
        """Run the configured consolidation dry run against the real source snapshot."""

        if self.mode != "full":
            return None
        consolidate = getattr(self._base_runtime, "consolidate_before_freeze", None)
        if not callable(consolidate):
            return None
        persisted = getattr(self.learning, "_persisted_source_evidence", None)
        if not callable(persisted):
            raise RuntimeError("online learning source snapshot is unavailable")
        source = await persisted()
        snapshot_id = source.evidence.snapshot.snapshot_id
        run_id = getattr(outcome, "run_id", "unknown")
        request_id = f"sre-online-consolidate:{run_id}"
        idempotency_key = "sre-online-consolidate:" + sha256_json(
            {"run_id": run_id, "snapshot_id": snapshot_id}
        )
        dry_run = await consolidate(
            snapshot_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            apply=False,
        )
        applied = await consolidate(
            snapshot_id,
            request_id=request_id,
            idempotency_key=idempotency_key + ":apply",
            apply=True,
        )
        self._consolidation_diagnostics = {
            "snapshot_id": snapshot_id,
            "request_id": request_id,
            "dry_run_applied": dry_run.applied,
            "applied": applied.applied,
            "delta_count": len(applied.deltas),
        }
        return applied

    async def finalize_run(self, outcome: Any) -> None:
        """Finalize the online bridge once, then invoke full-profile maintenance."""

        await self.learning.finalize_run(outcome)
        await self.consolidate_after_finalize(outcome)

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        # The host supplies the cutoff. Model text cannot enlarge the time boundary.
        cutoff = datetime.now(UTC).isoformat()
        constrained = request.model_copy(
            update={
                "max_items": min(3, request.max_items) if request.max_items is not None else 3,
                "max_estimated_tokens": min(8000, request.max_estimated_tokens)
                if request.max_estimated_tokens is not None
                else 8000,
            }
        )
        baseline = await self.learning.build_context(constrained)
        types, scope_evidence = await self._recall_scope(request, cutoff)
        candidates = []
        latest = request.context.get("latest_result", {})
        if (
            self.capacity_reader is not None
            and isinstance(latest, dict)
            and latest.get("ok") is True
        ):
            data = latest.get("data", {})
            logs = data.get("logs", []) if isinstance(data, dict) else []
            has_capacity_error = isinstance(logs, list) and any(
                isinstance(log, dict) and log.get("error") == "SERVER_CAPACITY_EXCEEDED"
                for log in logs
            )
            capacity_view = latest.get("action_kind") in {"get_metrics", "get_resources"} and any(
                role == "backend" for role, _instance in types
            )
            if has_capacity_error or capacity_view:
                scoped = constrained.model_copy(
                    update={
                        "query": "request_admission SERVER_CAPACITY_EXCEEDED",
                        "context": {
                            **request.context,
                            "decision_cutoff": cutoff,
                            "observation": {
                                "error_code": "SERVER_CAPACITY_EXCEEDED",
                                "relation": "request_exceeds_remaining_capacity",
                            },
                        },
                        "max_items": 1,
                    }
                )
                contribution = await self.capacity_reader.retrieve(scoped)
                candidates.extend(self._observed_context_item(item) for item in contribution.items)
        for role, instance in sorted(types):
            scoped = constrained.model_copy(
                update={
                    "query": "server.create server.delete hourly cost",
                    "context": {
                        **request.context,
                        "decision_cutoff": cutoff,
                        "observation": {
                            "role": role,
                            "instance_type": instance,
                            "other_mutations": False,
                        },
                    },
                    "max_items": 2,
                }
            )
            for reader in self.readers:
                contribution = await reader.retrieve(scoped)
                for item in contribution.items:
                    candidates.append(self._observed_context_item(item))

        # One item from each descriptive view is admitted first.  Remaining
        # slots are filled round-robin so a prolific view cannot monopolize
        # the three-item online budget.
        by_origin: dict[str, list[Any]] = {}
        for item in candidates:
            by_origin.setdefault(item.envelope.origin_module, []).append(item)
        balanced: list[Any] = []
        origins = sorted(by_origin)
        while origins and len(balanced) < 2:
            next_origins = []
            for origin in origins:
                values = by_origin[origin]
                if values:
                    balanced.append(values.pop(0))
                    if len(balanced) >= 2:
                        break
                if values:
                    next_origins.append(origin)
            origins = next_origins

        limit = min(3, constrained.max_items if constrained.max_items is not None else 3)
        selected = []
        seen = set()
        for item in balanced + baseline.items:
            if item.envelope.item_id in seen or len(selected) >= limit:
                continue
            item = item.model_copy(update={"estimated_tokens": _estimate_utf8_bytes(item)})
            if (
                constrained.max_estimated_tokens is not None
                and sum(i.estimated_tokens for i in selected) + item.estimated_tokens
                > constrained.max_estimated_tokens
            ):
                continue
            proposed = DecisionMemoryContext(items=[*selected, item], warnings=baseline.warnings)
            encoded = json.dumps(
                proposed.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            if len(encoded) <= self.budget_bytes:
                selected.append(item)
                seen.add(item.envelope.item_id)
        self._diagnostics = {
            "decision_cutoff": cutoff,
            "known_types": [list(pair) for pair in sorted(types)],
            "scope_evidence": scope_evidence,
            "reader_modules": [self._reader_module_id(reader) for reader in self.readers],
            "selected_derived_ids": [
                i.envelope.item_id
                for i in selected
                if i.envelope.item_id in {candidate.envelope.item_id for candidate in candidates}
            ],
        }
        return DecisionMemoryContext(items=selected, warnings=baseline.warnings)

    @staticmethod
    def _public_resource_types(result: dict) -> set[tuple[str, str]] | None:
        """None is no scope observation; an empty set explicitly clears scope."""
        if result.get("ok") is not True:
            return None
        data = result.get("data", {})
        if not isinstance(data, dict):
            return None
        for container in (data, data.get("result", {})):
            if not isinstance(container, dict):
                continue
            for key in ("servers", "types"):
                resources = container.get(key)
                if not isinstance(resources, list):
                    continue
                types = {
                    (resource["role"], resource["instance_type"])
                    for resource in resources
                    if isinstance(resource, dict)
                    and isinstance(resource.get("role"), str)
                    and isinstance(resource.get("instance_type"), str)
                }
                return set(sorted(types)[:32])
        return None

    async def _recall_scope(
        self, request: MemoryContextRequest, cutoff: str
    ) -> tuple[set[tuple[str, str]], dict]:
        latest = request.context.get("latest_result", {})
        if not isinstance(latest, dict):
            return set(), {"reason": "missing_public_result"}
        explicit = self._public_resource_types(latest)
        if explicit is not None:
            return explicit, {"source": "latest_public_result"}
        # Carry type applicability, never a server inventory or capacity estimate.
        # Unrelated tools do not become a reason to inject tariff knowledge.
        allowed = {"get_overview", "get_metrics", "get_operation", "advance_time_v2"}
        command = (
            latest.get("data", {}).get("command") if isinstance(latest.get("data"), dict) else None
        )
        if (
            latest.get("ok") is not True
            or latest.get("action_kind") not in allowed
            or (isinstance(command, str) and not command.startswith("server."))
        ):
            return set(), {"reason": "unrelated_or_failed_result"}
        iteration = request.context.get("iteration")
        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
            return set(), {"reason": "missing_iteration"}
        # Reconstruct from verified persisted experience: survives reopening and
        # cannot accidentally import a different run's working state.
        persisted = await self.learning._persisted_source_evidence()
        eligible = []
        for record in persisted.transition_records:
            transition = ExperienceTransition.model_validate(record.payload)
            if (
                transition.run_id == request.run_id
                and 0 < iteration - transition.iteration <= 128
                and transition.occurred_at < datetime.fromisoformat(cutoff)
            ):
                eligible.append((transition, record.content_hash))
        eligible.sort(
            key=lambda pair: (pair[0].iteration, pair[0].occurred_at, pair[0].transition_id),
            reverse=True,
        )
        for transition, content_hash in eligible:
            types = self._public_resource_types(transition.result)
            if types is not None:
                return types, {
                    "source": "persisted_public_result",
                    "run_id": transition.run_id,
                    "transition_id": transition.transition_id,
                    "content_hash": content_hash,
                    "iteration": transition.iteration,
                    "age_iterations": iteration - transition.iteration,
                    "max_age_iterations": 128,
                    "snapshot_id": persisted.evidence.snapshot.snapshot_id,
                }
        return set(), {"reason": "no_recent_same_run_scope"}

    @staticmethod
    def _reader_module_id(reader: Any) -> str:
        configured = getattr(reader, "module_id", None)
        if isinstance(configured, str) and configured:
            return configured
        return {
            "WorldModelMemory": "world_model",
            "ObservedLessonsMemory": "observed_lessons",
        }.get(type(reader).__name__, type(reader).__name__)

    @staticmethod
    def _observed_context_item(item: Any) -> Any:
        fact = item.envelope.item
        candidate = fact["candidate"]
        payload = {
            key: fact[key]
            for key in (
                "status",
                "support_count",
                "counter_count",
                "unknown_result_count",
                "evidence_snapshot_hash",
            )
            if key in fact
        }
        payload.update(
            candidate=candidate,
            active=False,
            causal_credit=False,
            source_run_ids=sorted(fact.get("source_run_outcome_statuses", {})),
            statement=(
                f"Observed {candidate['action_kind']} with hourly cost delta "
                f"{candidate['result_value']} minor/hour. Historical association "
                "only; current capacity and availability still require checking."
            ),
        )
        if candidate["action_kind"] == "request_admission":
            payload["statement"] = (
                "Observed SERVER_CAPACITY_EXCEEDED when the request's reported required load "
                "exceeded reported available capacity. These logs do not establish total "
                "installed capacity, a safe utilization threshold, or a required server count. "
                "Check current resource capacity and used load before sizing a correction."
            )
        for key in ("metric", "metric_polarity"):
            if key in fact:
                payload[key] = fact[key]
        provenance = list(
            {
                p.artefact_id: p
                for p in (*item.envelope.provenance[:2], item.envelope.provenance[-1])
            }.values()
        )
        envelope = item.envelope.model_copy(update={"item": payload, "provenance": provenance})
        return item.model_copy(update={"envelope": envelope})


def compose_sre_online_memory(
    store: Any,
    *,
    namespace: str,
    mode: str = "world",
    every_n_transitions: int = 8,
    learn_capacity: bool = True,
) -> SreObservedMemory:
    """Compose an explicit runnable online profile; legacy defaults are unchanged."""
    from uptick_agent.composition.memory import compose_experimental_runtime
    from uptick_agent.memory.config import MemoryConfiguration
    from uptick_agent.memory.observed_lessons import ObservedLessonMetric, ObservedLessonsMemory
    from uptick_agent.memory.online_learning import OnlineLearningMemory
    from uptick_agent.memory.world_model import WorldModelMemory

    if mode not in {"world", "lessons", "full"}:
        raise ValueError("online SRE memory mode must be world, lessons, or full")
    if mode == "full":
        # Reuse the declared A9 settings so every strict capability is a real
        # composition participant.  Online observed readers remain explicitly
        # descriptive and do not alter the strict promotion gates.
        from uptick_agent.evaluation_presets import experimental_presets

        configuration = next(
            preset.configuration.model_copy(deep=True)
            for preset in experimental_presets()
            if preset.condition_id == "A9"
        )
    else:
        configuration = MemoryConfiguration.episodic_only()
    configuration.context_budget.total_tokens = 8000
    configuration.context_budget.total_items = 3
    configuration.episodic.max_context_tokens = 8000
    configuration.episodic.max_context_items = 3
    source = f"{namespace}:episodes"
    base = compose_experimental_runtime(configuration, store, namespace=source)
    if mode in {"world", "full"}:
        world_reader = WorldModelMemory(
            store,
            namespace=f"{namespace}:world",
            source=None,
            settings=SRE_OUTCOME_SETTINGS,
            allow_observed_summaries=True,
            allow_current_run_observed=True,
        )
    if mode in {"lessons", "full"}:
        lesson_reader = ObservedLessonsMemory(
            store,
            namespace=f"{namespace}:lessons",
            settings=SRE_OUTCOME_SETTINGS,
            metric=ObservedLessonMetric(
                name="current_cost_per_hour", unit="minor/hour", direction="minimize"
            ),
            enabled=True,
            allow_current_run_observed=True,
        )
    readers = (
        (world_reader, lesson_reader)
        if mode == "full"
        else (world_reader,)
        if mode == "world"
        else (lesson_reader,)
    )
    learning = OnlineLearningMemory(
        base,
        store,
        source_namespace=source,
        # Separate checkpoint/projection space for the revised extraction policy.
        # Old frozen evidence and progress receipts remain untouched.
        derived_namespace=f"{namespace}:derived:{mode}:v2",
        writers=readers,
        pattern_settings=SRE_OUTCOME_SETTINGS,
        projector=project_sre_outcomes,
        every_n_transitions=every_n_transitions,
    )
    layers = {"cost": learning}
    capacity_reader = None
    if mode == "full" and learn_capacity:
        from uptick_agent.composition.sre_capacity import (
            CAPACITY_SETTINGS,
            project_capacity_observations,
        )

        capacity_reader = WorldModelMemory(
            store,
            namespace=f"{namespace}:capacity",
            source=None,
            settings=CAPACITY_SETTINGS,
            allow_observed_summaries=True,
            allow_current_run_observed=True,
        )
        # Decorator composition: the outer channel delegates to the cost channel,
        # which alone delegates to base persistence. Each raw event is written once.
        learning = OnlineLearningMemory(
            learning,
            store,
            source_namespace=source,
            derived_namespace=f"{namespace}:derived:capacity:v1",
            writers=(capacity_reader,),
            pattern_settings=CAPACITY_SETTINGS,
            projector=project_capacity_observations,
            every_n_transitions=every_n_transitions,
        )
        layers["capacity"] = learning
    return SreObservedMemory(
        learning,
        readers,
        mode=mode,
        base_runtime=base,
        capacity_reader=capacity_reader,
        learning_layers=layers,
    )
