"""Validation of training evidence before a frozen evaluation binding."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from uptick_agent.evaluation.contracts import (
    V2AttemptRecord,
    V2Condition,
    V2Manifest,
    environment_pin_for_seed,
)
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.stores.contracts import (
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
)


class TrainingProvenanceValidator:
    """Validate every training record against the sealed run split."""

    def __init__(self, manifest: V2Manifest, store: StructuredMemoryStore) -> None:
        self.manifest = manifest
        self.store = store

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

    async def validate(
        self,
        condition: V2Condition,
        training_attempts: tuple[V2AttemptRecord, ...],
        namespaces: Sequence[str],
    ) -> None:
        """Reject pre-existing or cross-run records before freezing inputs."""

        from uptick_agent.memory.audit import AuditTraceEvent
        from uptick_agent.memory.candidate_validation import (
            _validate_outcome_record,
            _validate_transition_record,
            validate_evidence,
        )
        from uptick_agent.memory.consolidation import (
            CONSOLIDATION_APPLY_RECORD_TYPE,
            CONSOLIDATION_PLAN_RECORD_TYPE,
            ConsolidationApply,
            ConsolidationDelta,
            ConsolidationPlan,
            StoredSnapshotEvidenceSource,
        )
        from uptick_agent.memory.lesson_contracts import (
            LessonEvidence,
            LessonRunDeclaration,
            ValidatedLesson,
            declaration_hash,
            snapshot_input_hash,
        )
        from uptick_agent.memory.lessons import LessonBatch
        from uptick_agent.memory.maintenance import MaintenancePlan
        from uptick_agent.memory.patterns import ValidatedPattern
        from uptick_agent.memory.playbooks import PLAYBOOK_BATCH_RECORD_TYPE, PlaybookBatch
        from uptick_agent.memory.stores.contracts import MemorySnapshot
        from uptick_agent.memory.tool_knowledge import (
            TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            ToolKnowledgeBatch,
        )
        from uptick_agent.memory.world_model import WORLD_BATCH_RECORD_TYPE, WorldHypothesisBatch

        attempts_by_run = {
            attempt.run_id: attempt for attempt in training_attempts if attempt.run_id
        }
        if len(attempts_by_run) != sum(attempt.run_id is not None for attempt in training_attempts):
            raise ValueError("training attempts must have unique physical run IDs")
        base = self._namespace(condition.condition_id, "training")

        def require_run(run_id: object, *, label: str) -> V2AttemptRecord:
            if not isinstance(run_id, str) or run_id not in attempts_by_run:
                raise ValueError(f"{label} references a run outside this training split")
            return attempts_by_run[run_id]

        def require_context(attempt: V2AttemptRecord, environment_id: object, scenario_id: object):
            if environment_id is not None and environment_id != attempt.environment_id:
                raise ValueError("training provenance environment identity changed")
            if scenario_id is not None and scenario_id != attempt.scenario_id:
                raise ValueError("training provenance scenario identity changed")

        def validate_declaration(value: object, *, label: str) -> LessonRunDeclaration:
            declaration = (
                value
                if isinstance(value, LessonRunDeclaration)
                else LessonRunDeclaration.model_validate(value)
            )
            attempt = require_run(declaration.run_id, label=label)
            if declaration.phase != "learning" or declaration.attempt_index != 0:
                raise ValueError("training provenance contains a non-learning declaration")
            require_context(attempt, declaration.environment_id, declaration.scenario_id)
            context = environment_pin_for_seed(self.manifest.profile, attempt.world_seed)
            if not context.context_identity_verified:
                raise ValueError("unverified training context cannot provide lesson provenance")
            if (
                declaration.environment_content_hash != context.environment_content_hash
                or declaration.scenario_content_hash != context.scenario_content_hash
            ):
                raise ValueError("training declaration context hashes do not match the manifest")
            return declaration

        def validate_record(record: StoredRecord, *, expected_namespace: str) -> None:
            if record.namespace != expected_namespace:
                raise ValueError("training provenance record crossed a memory namespace")
            payload = record.payload
            if record.record_type == "experience-transition":
                transition = _validate_transition_record(record)
                attempt = require_run(transition.run_id, label="experience transition")
                require_context(attempt, transition.environment_id, transition.scenario_id)
            elif record.record_type == "run-outcome":
                outcome = _validate_outcome_record(record)
                require_run(outcome.run_id, label="run outcome")
            elif record.record_type == "memory-entry":
                entry = MemoryEntry.model_validate(payload)
                require_run(entry.run_id, label="legacy memory entry")
            elif record.record_type == "audit-trace-event":
                event = AuditTraceEvent.model_validate(payload)
                require_run(event.run_id, label="audit trace")
            elif record.record_type == "lesson-run-declaration":
                validate_declaration(payload, label="lesson declaration")
            elif record.record_type == "lesson-capture-context":
                if set(payload) != {"snapshot_id", "outcome_run_id", "outcome_hash", "runs"}:
                    raise ValueError("lesson capture context payload shape is invalid")
                require_run(payload["outcome_run_id"], label="lesson capture context")
                runs = payload["runs"]
                if not isinstance(runs, list):
                    raise ValueError("lesson capture context declarations are invalid")
                for item in runs:
                    validate_declaration(
                        LessonRunDeclaration.model_validate(item), label="capture run"
                    )
            elif record.record_type == "lesson-batch":
                batch = LessonBatch.model_validate(payload)
                require_run(batch.outcome.run_id, label="lesson batch outcome")
                evidence = LessonEvidence.model_validate(batch.evidence.model_dump(mode="json"))
                evidence = validate_evidence(evidence)
                if evidence.snapshot.namespace != base:
                    raise ValueError("lesson evidence snapshot crossed the training namespace")
                for nested in evidence.records:
                    nested = StoredRecord.validate_integrity(nested)
                    validate_record(nested, expected_namespace=base)
                for declaration in evidence.runs:
                    validate_declaration(declaration, label="lesson evidence declaration")
            elif record.record_type in {
                "memory-maintenance-plan",
                "memory-maintenance-application",
            }:
                plan_payload = payload.get("plan")
                plan = MaintenancePlan.model_validate(plan_payload)
                if plan.namespace != base or plan.maintenance_namespace != expected_namespace:
                    raise ValueError("maintenance provenance crossed the training namespaces")
            elif record.record_type in {
                CONSOLIDATION_PLAN_RECORD_TYPE,
                CONSOLIDATION_APPLY_RECORD_TYPE,
            }:
                if expected_namespace != f"{base}:consolidation":
                    raise ValueError("consolidation provenance crossed the training namespace")
                if record.record_type == CONSOLIDATION_PLAN_RECORD_TYPE:
                    plan = ConsolidationPlan.model_validate(payload)
                    if record.record_id != f"consolidation-plan:{plan.plan_id}":
                        raise ValueError("consolidation plan record ID is invalid")
                    if record.created_at != plan.created_at:
                        raise ValueError("consolidation plan timestamp is invalid")
                else:
                    application = ConsolidationApply.model_validate(payload)
                    if record.record_id != f"consolidation-apply:{application.plan_id}":
                        raise ValueError("consolidation apply record ID is invalid")
                    if record.created_at != application.applied_at:
                        raise ValueError("consolidation apply timestamp is invalid")
            elif record.record_type in {
                WORLD_BATCH_RECORD_TYPE,
                PLAYBOOK_BATCH_RECORD_TYPE,
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            }:
                batch_types = {
                    WORLD_BATCH_RECORD_TYPE: ("world", WorldHypothesisBatch),
                    PLAYBOOK_BATCH_RECORD_TYPE: ("playbooks", PlaybookBatch),
                    TOOL_KNOWLEDGE_BATCH_RECORD_TYPE: ("tool-knowledge", ToolKnowledgeBatch),
                }
                suffix, batch_type = batch_types[record.record_type]
                if expected_namespace != f"{base}:{suffix}":
                    raise ValueError("derived memory provenance crossed its namespace")
                batch = batch_type.model_validate(payload)
                if record.created_at != batch.outcome.finished_at:
                    raise ValueError("derived memory batch timestamp is invalid")
                require_run(batch.outcome.run_id, label="derived memory outcome")
            else:
                raise ValueError(f"unknown training provenance record type {record.record_type!r}")

        async def validate_maintenance_record(
            record: StoredRecord, *, expected_namespace: str
        ) -> None:
            if record.record_type not in {
                "memory-maintenance-plan",
                "memory-maintenance-application",
            }:
                return
            plan = MaintenancePlan.model_validate(record.payload.get("plan"))
            if plan.namespace != base or plan.maintenance_namespace != expected_namespace:
                raise ValueError("maintenance provenance crossed the training namespaces")
            snapshot = await self.store.get_snapshot(snapshot_id=plan.snapshot_id)
            if snapshot is None:
                raise ValueError("maintenance plan references a missing training snapshot")
            snapshot = MemorySnapshot.validate_integrity(snapshot)
            if (
                snapshot.namespace != base
                or snapshot.content_hash != plan.snapshot_content_hash
                or snapshot.members != plan.snapshot_members
            ):
                raise ValueError("maintenance plan references a changed training snapshot")
            members = {member.record_id: member.content_hash for member in snapshot.members}
            for member in snapshot.members:
                source = await self.store.get(namespace=base, record_id=member.record_id)
                if source is None or StoredRecord.validate_integrity(source).content_hash != (
                    member.content_hash
                ):
                    raise ValueError("maintenance snapshot member is not training provenance")
            for delta in plan.deltas:
                if any(
                    members.get(member.record_id) != member.content_hash
                    for member in delta.source_members
                ):
                    raise ValueError("maintenance delta references a foreign source member")
                if any(
                    members.get(item.artefact_id) != item.content_hash for item in delta.provenance
                ):
                    raise ValueError("maintenance delta provenance is not training evidence")

        async def validate_consolidation_record(
            record: StoredRecord, *, expected_namespace: str
        ) -> None:
            """Verify consolidation receipts against the exact training evidence."""

            if record.record_type not in {
                CONSOLIDATION_PLAN_RECORD_TYPE,
                CONSOLIDATION_APPLY_RECORD_TYPE,
            }:
                return
            consolidation_namespace = f"{base}:consolidation"
            if expected_namespace != consolidation_namespace:
                raise ValueError("consolidation provenance crossed the training namespace")

            async def load_source_snapshot(
                snapshot_id: str,
                snapshot_content_hash: str,
                snapshot_members: Sequence[object],
            ) -> tuple[MemorySnapshot, tuple[StoredRecord, ...], LessonEvidence]:
                raw_snapshot = await self.store.get_snapshot(snapshot_id=snapshot_id)
                if raw_snapshot is None:
                    raise ValueError("consolidation plan references a missing training snapshot")
                snapshot = MemorySnapshot.validate_integrity(raw_snapshot)
                if snapshot.namespace != base:
                    raise ValueError("consolidation source snapshot crossed the training namespace")
                expected_members = tuple(snapshot_members)
                if snapshot.content_hash != snapshot_content_hash or tuple(snapshot.members) != (
                    expected_members
                ):
                    raise ValueError("consolidation plan references a changed training snapshot")
                records: list[StoredRecord] = []
                for member in snapshot.members:
                    source = await self.store.get(namespace=base, record_id=member.record_id)
                    if source is None:
                        raise ValueError("consolidation snapshot member is missing")
                    owned = StoredRecord.validate_integrity(source)
                    if owned.content_hash != member.content_hash:
                        raise ValueError("consolidation snapshot member hash changed")
                    validate_record(owned, expected_namespace=base)
                    if owned.record_type not in {"experience-transition", "run-outcome"}:
                        raise ValueError("consolidation snapshot contains an unsupported record")
                    records.append(owned)
                evidence_source = StoredSnapshotEvidenceSource(
                    self.store,
                    evidence_namespace=base,
                    declaration_namespace=f"{base}:lessons:declarations",
                )
                evidence = await evidence_source.load(snapshot.snapshot_id)
                return snapshot, tuple(records), evidence

            def require_hash_pairs(
                identifiers: Sequence[str],
                hashes: Sequence[str],
                expected: Mapping[str, str],
                label: str,
            ) -> None:
                if len(identifiers) != len(hashes):
                    raise ValueError(f"consolidation {label} IDs and hashes differ")
                for identifier, content_hash in zip(identifiers, hashes, strict=True):
                    if expected.get(identifier) != content_hash:
                        raise ValueError(f"consolidation {label} hash is not training evidence")

            def validate_item(
                item: object,
                *,
                snapshot: MemorySnapshot,
                records: Mapping[str, StoredRecord],
                evidence: LessonEvidence,
                source_refs: Mapping[str, str],
                plan_settings: object,
            ) -> object:
                if not isinstance(item, dict):
                    raise ValueError("consolidation candidate item is malformed")
                kind = item.get("kind")
                validated_payload = item.get("validated")
                if kind == "lesson":
                    validated = ValidatedLesson.model_validate(validated_payload)
                    manifest = validated.manifest
                elif kind == "world_hypothesis":
                    validated = ValidatedPattern.model_validate(validated_payload)
                    manifest = validated.manifest
                else:
                    raise ValueError("consolidation candidate kind is invalid")
                if item.get("status") != validated.status:
                    raise ValueError("consolidation candidate status is not canonical")
                if item.get("candidate") != validated.candidate.model_dump(mode="json"):
                    raise ValueError("consolidation candidate payload is not canonical")
                expected_settings = (
                    getattr(plan_settings, "lesson_settings", None)
                    if kind == "lesson"
                    else getattr(plan_settings, "pattern_settings", None)
                )
                if expected_settings is None or item.get("settings") != (
                    expected_settings.model_dump(mode="json")
                ):
                    raise ValueError("consolidation candidate settings are not canonical")
                if (
                    manifest.snapshot_id != snapshot.snapshot_id
                    or manifest.snapshot_content_hash != snapshot.content_hash
                    or manifest.input_hash != snapshot_input_hash(evidence)
                ):
                    raise ValueError("consolidation candidate is bound to another snapshot")
                record_hashes = {
                    member.record_id: member.content_hash for member in snapshot.members
                }
                require_hash_pairs(
                    manifest.record_ids, manifest.record_hashes, record_hashes, "record"
                )
                require_hash_pairs(
                    getattr(manifest, "outcome_record_ids", ()),
                    getattr(manifest, "outcome_record_hashes", ()),
                    record_hashes,
                    "outcome record",
                )
                declaration_hashes = {
                    f"declaration:{run.run_id}:{run.attempt_index}": declaration_hash(run)
                    for run in evidence.runs
                }
                require_hash_pairs(
                    manifest.declaration_ids,
                    manifest.declaration_hashes,
                    declaration_hashes,
                    "declaration",
                )
                require_hash_pairs(
                    manifest.source_leaf_ids,
                    manifest.source_leaf_hashes,
                    source_refs,
                    "source leaf",
                )
                for field_name in (
                    "searched_evidence",
                    "support_evidence",
                    "counter_evidence",
                ):
                    require_hash_pairs(
                        getattr(manifest, f"{field_name}_ids"),
                        getattr(manifest, f"{field_name}_hashes"),
                        record_hashes,
                        field_name,
                    )
                if any(run_id not in attempts_by_run for run_id in manifest.support_run_ids):
                    raise ValueError("consolidation support references a foreign training run")
                logical_run_ids = {attempt.logical_run_id for attempt in training_attempts}
                if any(
                    logical_id not in logical_run_ids
                    for logical_id in manifest.support_logical_run_ids
                ):
                    raise ValueError("consolidation support references a foreign logical run")
                return validated

            if record.record_type == CONSOLIDATION_PLAN_RECORD_TYPE:
                plan = ConsolidationPlan.model_validate(record.payload)
                if record.record_id != f"consolidation-plan:{plan.plan_id}":
                    raise ValueError("consolidation plan record ID is invalid")
                if record.created_at != plan.created_at:
                    raise ValueError("consolidation plan timestamp is invalid")
                expected_settings = condition.memory_configuration.consolidation_settings
                if expected_settings is None or plan.settings.model_dump(mode="json") != (
                    expected_settings.model_dump(mode="json")
                ):
                    raise ValueError("consolidation plan settings are not the resolved condition")
                snapshot, records, evidence = await load_source_snapshot(
                    plan.snapshot_id, plan.snapshot_content_hash, plan.snapshot_members
                )
                record_map = {item.record_id: item for item in records}
                if len(record_map) != len(records):
                    raise ValueError("consolidation source snapshot repeats a record")
                if plan.evidence_input_hash != snapshot_input_hash(evidence):
                    raise ValueError("consolidation evidence input hash changed")
                member_ids = set(record_map)
                if any(record_id not in member_ids for record_id in plan.replay_record_ids):
                    raise ValueError("consolidation replay references a foreign record")
                if not evidence.runs:
                    if plan.unavailable_reason is None:
                        raise ValueError("consolidation plan lacks authoritative run declarations")
                    if plan.candidate_dispositions or plan.active_items or plan.deltas:
                        raise ValueError(
                            "unavailable consolidation plan contains derived knowledge"
                        )
                    return
                if plan.unavailable_reason is not None:
                    raise ValueError("known consolidation evidence cannot be marked unavailable")
                evidence = validate_evidence(evidence)
                source_refs: dict[str, str] = {}
                for source_record in records:
                    if source_record.record_type != "experience-transition":
                        continue
                    transition = _validate_transition_record(source_record)
                    for ref in transition.provenance:
                        prior = source_refs.get(ref.artefact_id)
                        if prior is not None and prior != ref.content_hash:
                            raise ValueError("consolidation source leaf hash changed")
                        source_refs[ref.artefact_id] = ref.content_hash
                for item in plan.candidate_dispositions:
                    validate_item(
                        item,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                active_keys = {
                    canonical_json(item)
                    for item in plan.candidate_dispositions
                    if isinstance(item, dict) and item.get("status") == "active"
                }
                for item in plan.active_items:
                    if canonical_json(item) not in active_keys:
                        raise ValueError("consolidation active item is not a candidate disposition")
                    validate_item(
                        item,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                for delta in plan.deltas:
                    owned_delta = ConsolidationDelta.model_validate(delta)
                    if owned_delta.operation != "create":
                        raise ValueError("consolidation delta operation is unsupported")
                    validate_item(
                        owned_delta.payload,
                        snapshot=snapshot,
                        records=record_map,
                        evidence=evidence,
                        source_refs=source_refs,
                        plan_settings=plan.settings,
                    )
                return

            application = ConsolidationApply.model_validate(record.payload)
            if record.record_id != f"consolidation-apply:{application.plan_id}":
                raise ValueError("consolidation apply record ID is invalid")
            if record.created_at != application.applied_at:
                raise ValueError("consolidation apply timestamp is invalid")
            plan_record = await self.store.get(
                namespace=consolidation_namespace,
                record_id=f"consolidation-plan:{application.plan_id}",
            )
            if plan_record is None:
                raise ValueError("consolidation apply references a missing plan")
            plan_record = StoredRecord.validate_integrity(plan_record)
            if plan_record.record_type != CONSOLIDATION_PLAN_RECORD_TYPE:
                raise ValueError("consolidation apply references a non-plan record")
            await validate_consolidation_record(
                plan_record, expected_namespace=consolidation_namespace
            )
            plan = ConsolidationPlan.model_validate(plan_record.payload)
            if (
                application.request_id != plan.request_id
                or application.snapshot_id != plan.snapshot_id
                or application.snapshot_content_hash != plan.snapshot_content_hash
                or application.evidence_input_hash != plan.evidence_input_hash
                or tuple(application.delta_ids)
                != tuple(
                    f"{delta.artefact_type}:{sha256_json(delta.payload)}" for delta in plan.deltas
                )
            ):
                raise ValueError("consolidation apply does not match its plan")

        async def validate_derived_record(record: StoredRecord, *, expected_namespace: str) -> None:
            if record.record_type not in {
                WORLD_BATCH_RECORD_TYPE,
                PLAYBOOK_BATCH_RECORD_TYPE,
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE,
            }:
                return
            batch_types = {
                WORLD_BATCH_RECORD_TYPE: ("world", WorldHypothesisBatch),
                PLAYBOOK_BATCH_RECORD_TYPE: ("playbooks", PlaybookBatch),
                TOOL_KNOWLEDGE_BATCH_RECORD_TYPE: ("tool-knowledge", ToolKnowledgeBatch),
            }
            suffix, batch_type = batch_types[record.record_type]
            if expected_namespace != f"{base}:{suffix}":
                raise ValueError("derived memory provenance crossed its namespace")
            batch = batch_type.model_validate(record.payload)
            if record.created_at != batch.outcome.finished_at:
                raise ValueError("derived memory batch timestamp is invalid")
            require_run(batch.outcome.run_id, label="derived memory outcome")
            evidence = validate_evidence(batch.evidence)
            if evidence.snapshot.namespace != base:
                raise ValueError("derived memory evidence crossed the training namespace")
            if batch.input_hash != snapshot_input_hash(evidence):
                raise ValueError("derived memory evidence input hash changed")
            if batch.settings_hash != sha256_json(batch.settings.model_dump(mode="json")):
                raise ValueError("derived memory settings hash changed")
            for nested in evidence.records:
                owned_nested = StoredRecord.validate_integrity(nested)
                validate_record(owned_nested, expected_namespace=base)
            for declaration in evidence.runs:
                validate_declaration(declaration, label="derived memory declaration")

        for namespace in namespaces:
            records = await self.store.list(namespace=namespace)
            for record in records:
                owned_record = StoredRecord.validate_integrity(record)
                validate_record(owned_record, expected_namespace=namespace)
                await validate_maintenance_record(owned_record, expected_namespace=namespace)
                await validate_consolidation_record(owned_record, expected_namespace=namespace)
                await validate_derived_record(owned_record, expected_namespace=namespace)
