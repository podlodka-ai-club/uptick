"""Evaluation-specific memory composition and frozen binding creation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal

from uptick_agent.evaluation.contracts import (
    FrozenEvaluationBinding,
    V2AttemptRecord,
    V2Condition,
    V2Manifest,
    V2RunMatrixBlock,
    V2SnapshotRef,
    environment_pin_for_seed,
    freeze_evaluation_binding,
    sha256_json,
)
from uptick_agent.evaluation.provenance import TrainingProvenanceValidator
from uptick_agent.evaluation.snapshots import EvaluationMemoryFacade, SnapshotReadStore
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.in_memory import InMemoryMemory
from uptick_agent.memory.stores.contracts import RecordWrite, StructuredMemoryStore
from uptick_agent.memory.stores.in_memory import InMemoryStructuredStore
from uptick_agent.ports import AgentMemory


class DefaultEvaluationMemoryFactory:
    """Compose canonical memory modules with immutable evaluation read views."""

    def __init__(self, manifest: V2Manifest, store: StructuredMemoryStore | None = None) -> None:
        self.manifest = manifest
        self.store = store or InMemoryStructuredStore()
        self._legacy_training: dict[str, InMemoryMemory] = {}
        self._training_declarations: dict[str, tuple[object, ...]] = {}
        self._training_runtimes: dict[str, object] = {}
        self._prepared = False

    async def prepare(self) -> None:
        """Refuse a second execution against a nonempty training store."""

        if self._prepared:
            raise ValueError("evaluation memory factory cannot be reused")
        self._prepared = True
        for condition in self.manifest.profile.conditions:
            base = self._namespace(condition.condition_id, "training")
            namespaces = self._module_namespaces(
                condition.condition_id,
                condition.memory_configuration,
                base,
            )
            for namespace in namespaces:
                if await self.store.list(namespace=namespace):
                    raise ValueError(
                        "evaluation training namespace is nonempty; resume/replay is unsupported"
                    )
                for suffix in (":snapshot", ":pre-freeze"):
                    if await self.store.get_snapshot(snapshot_id=f"{namespace}{suffix}"):
                        raise ValueError(
                            "evaluation training snapshot already exists; "
                            "resume/replay is unsupported"
                        )

    def _namespace(self, condition_id: str, phase: str, suffix: str = "") -> str:
        digest = sha256_json(
            {
                "manifest_hash": self.manifest.manifest_hash,
                "condition_id": condition_id,
                "phase": phase,
                "suffix": suffix,
            }
        )[:48]
        return f"v2-{phase}:{digest}"

    def memory_metadata(
        self,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        phase: Literal["training", "evaluation"],
    ) -> dict[str, str]:
        base = self._namespace(condition.condition_id, "training")
        if phase == "training":
            memory_namespace = base
        else:
            suffix = sha256_json(
                {"attempt_id": attempt.attempt_id, "condition_id": condition.condition_id}
            )[:32]
            memory_namespace = self._namespace(condition.condition_id, "evaluation", suffix)
        return {
            "memory_namespace": memory_namespace,
            "audit_namespace": f"{memory_namespace}:audit",
        }

    async def stored_artifact_count(
        self,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        phase: Literal["training", "evaluation"],
    ) -> int | None:
        """Count records in namespaces owned by this factory.

        Training counts are cumulative across the condition's training
        namespace set.  Evaluation counts cover only the current attempt's
        isolated namespace set; frozen input is reported separately as
        ``snapshot_members``.  Store failures leave this optional measurement
        unavailable and never change the run outcome.
        """

        try:
            base = self.memory_metadata(condition, attempt, phase)["memory_namespace"]
            namespaces = self._module_namespaces(
                condition.condition_id,
                condition.memory_configuration,
                base,
            )
            records = [
                record
                for namespace in dict.fromkeys(namespaces)
                for record in await self.store.list(namespace=namespace)
            ]
            return len(records)
        except Exception:
            return None

    @staticmethod
    def _module_namespaces(
        condition_id: str, configuration: MemoryConfiguration, base: str
    ) -> list[str]:
        namespaces: list[str] = []
        if configuration.episodic.enabled:
            namespaces.append(base)
        if configuration.lessons.enabled:
            namespaces.append(f"{base}:lessons")
        if any(
            module.enabled
            for module in (
                configuration.lessons,
                configuration.world_model,
                configuration.playbooks,
                configuration.tool_knowledge,
                configuration.consolidation,
            )
        ):
            namespaces.append(f"{base}:lessons:declarations")
        if configuration.world_model.enabled:
            namespaces.append(f"{base}:world")
        if configuration.playbooks.enabled:
            namespaces.append(f"{base}:playbooks")
        if configuration.tool_knowledge.enabled:
            namespaces.append(f"{base}:tool-knowledge")
        if configuration.consolidation.enabled:
            namespaces.append(f"{base}:consolidation")
        if configuration.consolidation.enabled or configuration.forgetting.enabled:
            namespaces.append(f"{base}:maintenance")
        if configuration.compatibility_legacy.enabled:
            namespaces.append(f"{base}:legacy")
        if configuration.audit.enabled:
            namespaces.append(f"{base}:audit")
        return namespaces

    def _declaration(self, block: V2RunMatrixBlock, attempt: V2AttemptRecord, phase: str):
        from uptick_agent.memory.lesson_contracts import LessonRunDeclaration

        environment = environment_pin_for_seed(self.manifest.profile, block.world_seed)
        if not environment.context_identity_verified:
            return None
        return LessonRunDeclaration(
            run_id=attempt.run_id or "unknown",
            logical_run_id=attempt.logical_run_id,
            attempt_index=attempt.attempt_index,
            phase="learning" if phase == "training" else "frozen_evaluation",
            environment_id=attempt.environment_id,
            scenario_id=attempt.scenario_id,
            environment_content_hash=environment.environment_content_hash,
            scenario_content_hash=environment.scenario_content_hash,
            eligible=phase == "training" and attempt.attempt_index == 0,
        )

    def _audit_sink(
        self, store: StructuredMemoryStore, namespace: str, configuration: MemoryConfiguration
    ):
        if not configuration.audit.enabled:
            return None
        from uptick_agent.memory.audit import StructuredAuditTraceSink

        return StructuredAuditTraceSink(
            store,
            namespace=namespace,
            configuration=configuration.audit,
            runtime_configuration_fingerprint=configuration.fingerprint,
        )

    def _compose(
        self,
        store: StructuredMemoryStore,
        condition: V2Condition,
        *,
        episodic_namespace: str,
        lesson_namespace: str,
        audit_namespace: str,
        declarations: Sequence[object],
        legacy_delegate: InMemoryMemory | None = None,
        audit_store: StructuredMemoryStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> AgentMemory:
        configuration = condition.memory_configuration
        sink = self._audit_sink(audit_store or store, audit_namespace, configuration)
        from uptick_agent.composition.memory import compose_experimental_runtime

        return compose_experimental_runtime(
            configuration,
            store,
            namespace=episodic_namespace,
            condition_id=condition.condition_id,
            run_declarations=declarations,
            legacy_memory=legacy_delegate,
            clock=clock,
            audit_sink=sink,
        )

    async def __call__(
        self,
        block: V2RunMatrixBlock,
        condition: V2Condition,
        attempt: V2AttemptRecord,
        run_id: str,
        phase: Literal["training", "evaluation"],
        binding: FrozenEvaluationBinding | None,
    ) -> AgentMemory:
        base = self._namespace(condition.condition_id, "training")
        declaration = self._declaration(block, attempt, phase)
        if phase == "training":
            if declaration is not None:
                previous = self._training_declarations.setdefault(condition.condition_id, ())
                self._training_declarations[condition.condition_id] = (*previous, declaration)
            legacy = None
            if condition.memory_configuration.compatibility_legacy.enabled:
                legacy = self._legacy_training.setdefault(condition.condition_id, InMemoryMemory())
            runtime = self._compose(
                self.store,
                condition,
                episodic_namespace=base,
                lesson_namespace=f"{base}:lessons",
                audit_namespace=f"{base}:audit",
                declarations=self._training_declarations.get(condition.condition_id, ()),
                legacy_delegate=legacy,
            )
            self._training_runtimes[condition.condition_id] = runtime
            return runtime
        if binding is None:
            raise ValueError("evaluation memory requires a frozen binding")
        configuration = condition.memory_configuration
        read_store = SnapshotReadStore(self.store, binding.snapshot_refs)
        await read_store.load()
        legacy_enabled = configuration.compatibility_legacy.enabled
        read_legacy = None
        if legacy_enabled:
            read_records = await read_store.list(namespace=f"{base}:legacy")
            read_legacy = InMemoryMemory(
                MemoryEntry.model_validate(record.payload)
                for record in read_records
                if record.record_type == "memory-entry"
            )
        training_declarations = self._training_declarations.get(condition.condition_id, ())
        eval_declaration = (declaration,) if declaration is not None else ()
        eval_suffix = sha256_json(
            {"attempt_id": attempt.attempt_id, "condition_id": condition.condition_id}
        )[:32]
        eval_base = self._namespace(condition.condition_id, "evaluation", eval_suffix)
        read_runtime = self._compose(
            read_store,
            condition,
            episodic_namespace=base,
            lesson_namespace=f"{base}:lessons",
            audit_namespace=f"{eval_base}:audit",
            declarations=(*training_declarations, *eval_declaration),
            legacy_delegate=read_legacy,
            audit_store=self.store,
            clock=lambda: binding.created_at,
        )
        write_runtime = self._compose(
            self.store,
            condition,
            episodic_namespace=eval_base,
            lesson_namespace=f"{eval_base}:lessons",
            audit_namespace=f"{eval_base}:audit",
            declarations=eval_declaration,
            legacy_delegate=InMemoryMemory() if legacy_enabled else None,
            clock=lambda: binding.created_at,
        )
        return EvaluationMemoryFacade(
            read_runtime,
            write_runtime,
            frozen_snapshot_members=read_store.member_count,
        )

    async def freeze_binding(
        self, condition: V2Condition, training_attempts: tuple[V2AttemptRecord, ...]
    ) -> FrozenEvaluationBinding:
        base = self._namespace(condition.condition_id, "training")
        configuration = condition.memory_configuration
        namespaces = self._module_namespaces(condition.condition_id, configuration, base)
        training_runtime = self._training_runtimes.get(condition.condition_id)
        if configuration.consolidation.enabled:
            if training_runtime is None:
                raise ValueError("consolidation requires a composed training runtime")
            consolidate = getattr(training_runtime, "consolidate_before_freeze", None)
            if not callable(consolidate):
                raise ValueError("consolidation runtime does not expose its explicit operation")
            pre_snapshot_id = f"{base}:pre-freeze"
            await self.store.create_snapshot(
                namespace=base,
                snapshot_id=pre_snapshot_id,
                operation="evaluation-pre-freeze",
                idempotency_key=f"{pre_snapshot_id}:snapshot",
            )
            request_id = f"{self.manifest.manifest_id}:{condition.condition_id}:consolidate"
            await consolidate(
                pre_snapshot_id,
                request_id=request_id,
                idempotency_key=f"{request_id}:dry-run",
                apply=False,
            )
            await consolidate(
                pre_snapshot_id,
                request_id=request_id,
                idempotency_key=f"{request_id}:apply",
                apply=True,
            )
        if configuration.compatibility_legacy.enabled:
            legacy = self._legacy_training.get(condition.condition_id)
            if legacy is not None:
                for entry in legacy.entries:
                    await self.store.append(
                        RecordWrite(
                            namespace=f"{base}:legacy",
                            record_id=entry.id,
                            record_type="memory-entry",
                            payload=entry.model_dump(mode="json"),
                            created_at=entry.created_at,
                        ),
                        operation="evaluation-freeze-legacy",
                        idempotency_key=f"legacy:{entry.id}",
                    )
        await self._validate_training_provenance(condition, training_attempts, namespaces)
        refs: list[V2SnapshotRef] = []
        for namespace in namespaces:
            snapshot_id = f"{namespace}:snapshot"
            receipt = await self.store.create_snapshot(
                namespace=namespace,
                snapshot_id=snapshot_id,
                operation="evaluation-freeze-training",
                idempotency_key=f"{snapshot_id}:freeze",
            )
            refs.append(
                V2SnapshotRef(
                    namespace=namespace,
                    snapshot_id=snapshot_id,
                    content_hash=receipt.snapshot.content_hash,
                )
            )
        binding_digest = sha256_json(
            {
                "manifest_hash": self.manifest.manifest_hash,
                "condition_id": condition.condition_id,
            }
        )[:48]
        return freeze_evaluation_binding(
            self.manifest,
            condition_id=condition.condition_id,
            cache_namespace=f"v2-cache:{binding_digest}",
            audit_namespace=f"v2-audit:{binding_digest}",
            snapshot_refs=refs,
            training_attempt_ids=(item.attempt_id for item in training_attempts),
            training_world_contexts={
                item.world_seed: environment_pin_for_seed(self.manifest.profile, item.world_seed)
                for item in training_attempts
            },
        )


    async def _validate_training_provenance(
        self,
        condition: V2Condition,
        training_attempts: tuple[V2AttemptRecord, ...],
        namespaces: Sequence[str],
    ) -> None:
        await TrainingProvenanceValidator(self.manifest, self.store).validate(
            condition,
            training_attempts,
            namespaces,
        )
