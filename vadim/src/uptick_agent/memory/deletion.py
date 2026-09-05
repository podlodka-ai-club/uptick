"""Policy-mediated physical deletion for unreferenced raw memory records.

The existing maintenance module deliberately preserves its source archive.  This
module is the separate, destructive boundary: it plans from a complete store
inventory, requires an owner authorization record, and applies only an exact,
sealed plan.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from uptick_agent.memory.contracts import (
    MemoryConflictError,
    MemoryPermanentError,
    MemoryValidationError,
    RunOutcome,
)
from uptick_agent.memory.deletion_contracts import (
    DEFAULT_TOMBSTONE_NAMESPACE,
    DELETED_RECEIPT_KIND,
    DELETION_OPERATION,
    DELETION_TOMBSTONE_RECORD_TYPE,
    DeletedReceipt,
    DeletedSnapshotReceipt,
    DeletionAuthorization,
    DeletionBlock,
    DeletionCandidate,
    DeletionHold,
    DeletionPolicy,
    DeletionTombstone,
    PhysicalDeletionPlan,
    PhysicalDeletionReceipt,
    ReceiptEntry,
    RecordRef,
    SnapshotDeletionBlock,
    SnapshotRef,
    SnapshotRetirement,
    StoreInventory,
    make_inventory,
    receipt_entry,
)
from uptick_agent.memory.deletion_ports import DeletionStore
from uptick_agent.memory.stores.contracts import (
    StoredRecord,
    canonical_json,
    sha256_json,
    validate_namespace,
)


def plan_hash(plan: PhysicalDeletionPlan) -> str:
    body = plan.model_dump(mode="json")
    body.pop("plan_id", None)
    return sha256_json(body)


def _record_ref(record: StoredRecord) -> RecordRef:
    return RecordRef(
        namespace=record.namespace,
        record_id=record.record_id,
        content_hash=record.content_hash,
    )


def _snapshot_ref(snapshot) -> SnapshotRef:
    return SnapshotRef(
        namespace=snapshot.namespace,
        snapshot_id=snapshot.snapshot_id,
        created_at=snapshot.created_at,
        content_hash=snapshot.content_hash,
        members=snapshot.members,
    )


def _payload_refs(value: object) -> Iterable[tuple[str, str | None]]:
    if isinstance(value, dict):
        artefact_id = value.get("artefact_id")
        if not isinstance(artefact_id, str):
            artefact_id = value.get("record_id")
        content_hash = value.get("content_hash")
        if isinstance(artefact_id, str):
            yield artefact_id, content_hash if isinstance(content_hash, str) else None
        for nested in value.values():
            yield from _payload_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _payload_refs(nested)


def _payload_snapshot_refs(value: object) -> Iterable[tuple[str, str | None]]:
    if isinstance(value, dict):
        snapshot_id = value.get("snapshot_id")
        if isinstance(snapshot_id, str):
            snapshot_hash = value.get("snapshot_content_hash")
            if not isinstance(snapshot_hash, str):
                snapshot_hash = value.get("content_hash")
            yield snapshot_id, snapshot_hash if isinstance(snapshot_hash, str) else None
        for nested in value.values():
            yield from _payload_snapshot_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _payload_snapshot_refs(nested)


def _is_control_reference_record(record: StoredRecord) -> bool:
    """Return whether references in a control record are declarations.

    Deletion tombstones are validated metadata about already-retired
    identities.  They are never live provenance and must not keep a future
    same-ID record in another namespace from being eligible.
    """

    model_type = {
        "retention-authorization": DeletionAuthorization,
        "retention-hold": DeletionHold,
        DELETION_TOMBSTONE_RECORD_TYPE: DeletionTombstone,
    }.get(record.record_type)
    if model_type is None:
        return False
    if not _is_exact_control_payload(model_type, record.payload):
        raise MemoryPermanentError(f"{record.record_type} payload is invalid")
    return True


def _is_exact_control_payload(model_type, payload: object) -> bool:
    """Recognize only typed control metadata without unreviewed extra fields."""

    if not isinstance(payload, dict) or set(payload) - set(model_type.model_fields):
        return False
    try:
        parsed = model_type.model_validate(payload).model_dump(mode="json")
    except (TypeError, ValueError, ValidationError):
        return False
    return _input_keys_survive(payload, parsed)


def _input_keys_survive(input_value: object, parsed_value: object) -> bool:
    """Reject nested fields discarded by forward-minor model normalization."""

    if isinstance(input_value, dict):
        if not isinstance(parsed_value, dict):
            return False
        return all(
            key in parsed_value and _input_keys_survive(value, parsed_value[key])
            for key, value in input_value.items()
        )
    if isinstance(input_value, list):
        if not isinstance(parsed_value, list) or len(parsed_value) < len(input_value):
            return False
        return all(
            _input_keys_survive(value, parsed_value[index])
            for index, value in enumerate(input_value)
        )
    return True


def _retained_until(value: object) -> datetime | None:
    """Find the latest explicit future retention deadline in a payload."""

    deadlines: list[datetime] = []
    if isinstance(value, dict):
        raw = value.get("retained_until")
        if raw is not None and not isinstance(raw, str):
            raise MemoryPermanentError("retained_until obligation is invalid")
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.utcoffset() is not None:
                deadlines.append(parsed.astimezone(UTC))
            elif parsed is None or parsed.utcoffset() is None:
                raise MemoryPermanentError("retained_until obligation is invalid")
        for nested in value.values():
            deadline = _retained_until(nested)
            if deadline is not None:
                deadlines.append(deadline)
    elif isinstance(value, list):
        for nested in value:
            deadline = _retained_until(nested)
            if deadline is not None:
                deadlines.append(deadline)
    return max(deadlines) if deadlines else None


def _aware_utc(value: object) -> datetime | None:
    """Return an aware UTC timestamp, rejecting historical naive values."""

    if not isinstance(value, datetime) or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _retention_anchor(
    record: StoredRecord,
    records: dict[tuple[str, str], StoredRecord],
) -> tuple[datetime | None, str | None]:
    """Find the timestamp from which a raw record's age may be counted.

    Run-linked records are retained from their one authoritative terminal
    outcome, rather than from an earlier row timestamp.  An explicit
    experiment association without that outcome is retained conservatively.
    """

    created_at = _aware_utc(record.created_at)
    if created_at is None:
        return None, "record created_at timestamp is invalid or naive"
    payload = record.payload
    if "experiment_id" in payload and payload.get("experiment_id") is not None:
        experiment_id = payload.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            return None, "experiment_id is invalid"
        return None, "experiment-associated record has no authoritative completion"
    if "run_id" in payload:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None, "record run_id is invalid"
        outcomes: list[datetime] = []
        for outcome_record in records.values():
            if outcome_record.namespace != record.namespace:
                continue
            if outcome_record.record_type != "run-outcome":
                continue
            if outcome_record.payload.get("run_id") != run_id:
                continue
            try:
                outcome = RunOutcome.model_validate(outcome_record.payload)
            except (TypeError, ValueError, ValidationError):
                return None, "authoritative run outcome is invalid"
            finished_at = _aware_utc(outcome.finished_at)
            outcome_created_at = _aware_utc(outcome_record.created_at)
            if finished_at is None or outcome_created_at is None:
                return None, "authoritative run outcome timestamp is invalid or naive"
            if outcome.run_id != run_id or outcome_created_at != finished_at:
                return None, "authoritative run outcome identity or timestamp is invalid"
            outcomes.append(finished_at)
        if not outcomes:
            return None, "authoritative terminal run outcome is missing"
        if len(outcomes) != 1:
            return None, "authoritative terminal run outcome is ambiguous"
        return max(created_at, outcomes[0]), None
    if record.record_type == "experience-transition":
        return None, "experience-transition record has no run_id"
    return created_at, None


def _record_eligible_at(
    record: StoredRecord,
    records: dict[tuple[str, str], StoredRecord],
    minimum_raw_age_days: int,
) -> tuple[datetime | None, str | None]:
    anchor, reason = _retention_anchor(record, records)
    if anchor is None:
        return None, reason
    return anchor + timedelta(days=minimum_raw_age_days), None


def _snapshot_eligible_at(
    snapshot,
    records: dict[tuple[str, str], StoredRecord],
    minimum_raw_age_days: int,
) -> tuple[datetime | None, str | None]:
    """Derive a snapshot floor from its immutable members and run outcomes."""

    anchor = _aware_utc(snapshot.created_at)
    if anchor is None:
        return None, "snapshot created_at timestamp is invalid or naive"
    for member in snapshot.members:
        record = records.get((snapshot.namespace, member.record_id))
        if record is None:
            return None, f"snapshot member {member.record_id} is missing"
        if record.content_hash != member.content_hash:
            return None, f"snapshot member {member.record_id} content hash changed"
        member_anchor, reason = _retention_anchor(record, records)
        if member_anchor is None:
            return None, f"snapshot member {member.record_id}: {reason}"
        anchor = max(anchor, member_anchor)
    return anchor + timedelta(days=minimum_raw_age_days), None


def _validate_authorization(record: StoredRecord, policy: DeletionPolicy) -> DeletionAuthorization:
    if record.record_type != "retention-authorization":
        raise MemoryValidationError("owner authorization must be a retention-authorization record")
    authorization = _validated_control_payload(
        DeletionAuthorization, record.payload, "owner authorization payload is invalid"
    )
    if (
        authorization.policy_id != policy.policy_id
        or authorization.policy_version != policy.policy_version
        or authorization.scope != policy.scope
        or authorization.minimum_raw_age_days != policy.minimum_raw_age_days
        or sorted(authorization.deletable_record_types) != sorted(policy.deletable_record_types)
    ):
        raise MemoryConflictError("owner authorization is not bound to the deletion policy")
    return authorization


def _validated_control_payload(model_type, payload: object, message: str):
    """Parse administrative metadata only when no nested field was discarded."""

    if not _is_exact_control_payload(model_type, payload):
        raise MemoryPermanentError(message)
    try:
        return model_type.model_validate(payload)
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryPermanentError(message) from error


def _hold_records(
    inventory: StoreInventory, now: datetime
) -> tuple[tuple[RecordRef, ...], set[str]]:
    protected: list[RecordRef] = []
    hold_ids: set[str] = set()
    for record in inventory.records:
        if record.record_type != "retention-hold":
            continue
        hold = _validated_control_payload(
            DeletionHold, record.payload, "retention-hold record is invalid"
        )
        if hold.hold_id in hold_ids:
            raise MemoryConflictError(f"duplicate retention hold {hold.hold_id}")
        hold_ids.add(hold.hold_id)
        protected.append(_record_ref(record))
        if hold.is_active(now):
            protected.extend(hold.target_refs)
    return tuple(protected), hold_ids


def _held_snapshot_keys(inventory: StoreInventory, now: datetime) -> set[tuple[str, str, str]]:
    held: set[tuple[str, str, str]] = set()
    for record in inventory.records:
        if record.record_type != "retention-hold":
            continue
        hold = _validated_control_payload(
            DeletionHold, record.payload, "retention-hold record is invalid"
        )
        if hold.is_active(now):
            held.update(
                (ref.namespace, ref.snapshot_id, ref.content_hash) for ref in hold.snapshot_refs
            )
    return held


def _ambiguous_hold_keys(
    inventory: StoreInventory, now: datetime
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return active hold identifiers whose content hash cannot be trusted.

    A typed hold can still refer to a stale record or snapshot hash.  Such a
    reference must not be treated as absent merely because it fails an exact
    triple match.
    """

    record_ids: set[tuple[str, str]] = set()
    snapshot_ids: set[tuple[str, str]] = set()
    record_hashes = {
        (record.namespace, record.record_id): record.content_hash for record in inventory.records
    }
    snapshot_hashes = {
        (snapshot.namespace, snapshot.snapshot_id): snapshot.content_hash
        for snapshot in inventory.snapshots
    }
    for record in inventory.records:
        if record.record_type != "retention-hold":
            continue
        hold = _validated_control_payload(
            DeletionHold, record.payload, "retention-hold record is invalid"
        )
        if not hold.is_active(now):
            continue
        for ref in hold.target_refs:
            if record_hashes.get((ref.namespace, ref.record_id)) != ref.content_hash:
                record_ids.add((ref.namespace, ref.record_id))
        for ref in hold.snapshot_refs:
            if snapshot_hashes.get((ref.namespace, ref.snapshot_id)) != ref.content_hash:
                snapshot_ids.add((ref.namespace, ref.snapshot_id))
    return record_ids, snapshot_ids


def _validate_plan(plan: PhysicalDeletionPlan) -> PhysicalDeletionPlan:
    try:
        owned = PhysicalDeletionPlan.model_validate(plan.model_dump(mode="json"))
    except (TypeError, ValueError, ValidationError) as error:
        raise MemoryValidationError("deletion plan is invalid") from error
    if plan_hash(owned) != owned.plan_id:
        raise MemoryConflictError("deletion plan content hash mismatch")
    return owned


def validate_deletion_plan(plan: PhysicalDeletionPlan) -> PhysicalDeletionPlan:
    """Validate and own a sealed plan before any store replay shortcut."""

    return _validate_plan(plan)


def validate_plan_against_inventory(
    plan: PhysicalDeletionPlan,
    inventory: StoreInventory,
    *,
    now: datetime | None = None,
) -> tuple[dict[tuple[str, str], StoredRecord], list[ReceiptEntry]]:
    """Recheck policy, references, holds and eligibility against live state."""

    plan = _validate_plan(plan)
    if inventory.content_hash != plan.inventory_hash:
        raise MemoryConflictError("deletion plan inventory is stale")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if now < plan.as_of:
        raise MemoryConflictError("deletion plan is from the future")
    records = {(record.namespace, record.record_id): record for record in inventory.records}
    auth = records.get((plan.policy.authorization.namespace, plan.policy.authorization.record_id))
    if auth is None or _record_ref(auth) != plan.policy.authorization:
        raise MemoryConflictError("owner authorization record is missing or changed")
    authorization = _validate_authorization(auth, plan.policy)
    hold_refs, _ = _hold_records(inventory, now)
    hold_keys = {(ref.namespace, ref.record_id, ref.content_hash) for ref in hold_refs}
    ambiguous_hold_record_ids, ambiguous_hold_snapshot_ids = _ambiguous_hold_keys(inventory, now)
    snapshots = {
        (snapshot.namespace, snapshot.snapshot_id): snapshot for snapshot in inventory.snapshots
    }
    retired_keys = {
        (item.snapshot.namespace, item.snapshot.snapshot_id) for item in plan.retired_snapshots
    }
    authorized_snapshots = {
        (item.namespace, item.snapshot_id): item for item in authorization.retire_snapshot_refs
    }
    if not retired_keys <= set(authorized_snapshots):
        raise MemoryConflictError("deletion plan retires an unattested snapshot")
    held_snapshot_keys = _held_snapshot_keys(inventory, now)
    for item in plan.retired_snapshots:
        current = snapshots.get((item.snapshot.namespace, item.snapshot.snapshot_id))
        if current is None or _snapshot_ref(current) != item.snapshot:
            raise MemoryConflictError(f"snapshot {item.snapshot.snapshot_id} is missing or changed")
        if (
            authorized_snapshots.get((item.snapshot.namespace, item.snapshot.snapshot_id))
            != item.snapshot
        ):
            raise MemoryConflictError(f"snapshot {item.snapshot.snapshot_id} is not owner-attested")
        eligible_at, retention_reason = _snapshot_eligible_at(
            current, records, plan.policy.minimum_raw_age_days
        )
        if retention_reason is not None or eligible_at is None:
            raise MemoryConflictError(
                f"snapshot {item.snapshot.snapshot_id} is not retention-eligible: "
                f"{retention_reason}"
            )
        if now < eligible_at or item.eligible_at != eligible_at:
            raise MemoryConflictError(
                f"snapshot {item.snapshot.snapshot_id} is not retention-eligible"
            )
        if (
            item.snapshot.namespace,
            item.snapshot.snapshot_id,
            item.snapshot.content_hash,
        ) in held_snapshot_keys:
            raise MemoryConflictError(
                f"snapshot {item.snapshot.snapshot_id} is under an active hold"
            )
        if (item.snapshot.namespace, item.snapshot.snapshot_id) in ambiguous_hold_snapshot_ids:
            raise MemoryConflictError(
                f"snapshot {item.snapshot.snapshot_id} has an ambiguous active hold"
            )
    for item in plan.blocked_snapshots:
        current = snapshots.get((item.snapshot.namespace, item.snapshot.snapshot_id))
        if current is None or _snapshot_ref(current) != item.snapshot:
            raise MemoryConflictError(
                f"blocked snapshot {item.snapshot.snapshot_id} is missing or changed"
            )
    snapshot_keys = {
        (snapshot.namespace, member.record_id, member.content_hash)
        for snapshot in inventory.snapshots
        if (snapshot.namespace, snapshot.snapshot_id) not in retired_keys
        for member in snapshot.members
    }
    snapshot_member_ids = {
        (snapshot.namespace, member.record_id)
        for snapshot in inventory.snapshots
        if (snapshot.namespace, snapshot.snapshot_id) not in retired_keys
        for member in snapshot.members
    }
    candidate_keys = {(item.ref.namespace, item.ref.record_id) for item in plan.candidates}
    if len(candidate_keys) != len(plan.candidates):
        raise MemoryConflictError("deletion plan repeats a candidate")
    for item in plan.candidates:
        record = records.get((item.ref.namespace, item.ref.record_id))
        if record is None or _record_ref(record) != item.ref:
            raise MemoryConflictError(
                f"deletion candidate {item.ref.record_id} is missing or changed"
            )
        if record.record_type != item.record_type:
            raise MemoryConflictError(f"deletion candidate {item.ref.record_id} type changed")
        if record.record_type not in plan.policy.deletable_record_types:
            raise MemoryConflictError(f"record type {record.record_type} is not deletable")
        if record.record_type in plan.policy.project_lifetime_record_types:
            raise MemoryConflictError(f"record {record.record_id} is project-lifetime protected")
        if record.payload.get("retention_class") not in (None, "raw"):
            raise MemoryConflictError(f"record {record.record_id} has protected retention class")
        if record.payload.get("status") in {"candidate", "active", "validated", "promoted"}:
            raise MemoryConflictError(f"record {record.record_id} is decision-visible")
        eligible_at, retention_reason = _record_eligible_at(
            record, records, plan.policy.minimum_raw_age_days
        )
        if retention_reason is not None or eligible_at is None:
            raise MemoryConflictError(
                f"record {record.record_id} is not retention-eligible: {retention_reason}"
            )
        if now < eligible_at or item.eligible_at != eligible_at:
            raise MemoryConflictError(f"record {record.record_id} is not retention-eligible")
        retained_until = _retained_until(record.payload)
        if retained_until is not None and now < retained_until:
            raise MemoryConflictError(f"record {record.record_id} has an active retained_until")
        key = (item.ref.namespace, item.ref.record_id, item.ref.content_hash)
        if key in snapshot_keys:
            raise MemoryConflictError(f"record {record.record_id} is a snapshot member")
        if (item.ref.namespace, item.ref.record_id) in snapshot_member_ids:
            raise MemoryConflictError(
                f"record {record.record_id} has a snapshot member hash mismatch"
            )
        if key in hold_keys:
            raise MemoryConflictError(f"record {record.record_id} is under an active hold")
        if (item.ref.namespace, item.ref.record_id) in ambiguous_hold_record_ids:
            raise MemoryConflictError(f"record {record.record_id} has an ambiguous active hold")

    # Namespace-less provenance is resolved conservatively: an exact matching
    # ID/hash protects every record with that ID, while an ID/hash mismatch also
    # blocks the candidate rather than guessing which namespace was intended.
    incoming: dict[tuple[str, str], list[str]] = {key: [] for key in candidate_keys}
    for source in inventory.records:
        if _is_control_reference_record(source):
            continue
        for artefact_id, content_hash in _payload_refs(source.payload):
            for item in plan.candidates:
                if item.ref.record_id != artefact_id:
                    continue
                if (source.namespace, source.record_id) == (
                    item.ref.namespace,
                    item.ref.record_id,
                ):
                    continue
                reason = (
                    f"provenance:{source.namespace}:{source.record_id}"
                    if content_hash == item.ref.content_hash
                    else f"ambiguous-provenance:{source.namespace}:{source.record_id}"
                )
                incoming[(item.ref.namespace, item.ref.record_id)].append(reason)
    if any(incoming.values()):
        raise MemoryConflictError(
            "deletion plan has live provenance references: "
            + ", ".join(sorted(reason for reasons in incoming.values() for reason in reasons))
        )

    for source in inventory.records:
        if _is_control_reference_record(source):
            continue
        for snapshot_id, _snapshot_hash in _payload_snapshot_refs(source.payload):
            for item in plan.retired_snapshots:
                if item.snapshot.snapshot_id != snapshot_id:
                    continue
                raise MemoryConflictError(
                    f"snapshot {snapshot_id} has a live or ambiguous payload reference"
                )

    expected_receipts = [
        receipt
        for receipt in inventory.receipts
        if (
            receipt.record_ref is not None
            and (receipt.record_ref.namespace, receipt.record_ref.record_id) in candidate_keys
        )
        or (
            receipt.snapshot_ref is not None
            and (receipt.snapshot_ref.namespace, receipt.snapshot_ref.snapshot_id) in retired_keys
        )
    ]
    if sorted(
        expected_receipts, key=lambda value: canonical_json(value.model_dump(mode="json"))
    ) != sorted(
        plan.purge_receipts, key=lambda value: canonical_json(value.model_dump(mode="json"))
    ):
        raise MemoryConflictError("deletion plan receipt set is stale")
    return records, expected_receipts


class PhysicalDeletionManager:
    """Create and apply sealed deletion plans for a deletion-capable store."""

    def __init__(
        self,
        store: DeletionStore,
        *,
        policy: DeletionPolicy,
        tombstone_namespace: str = DEFAULT_TOMBSTONE_NAMESPACE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = DeletionPolicy.model_validate(policy.model_dump(mode="json"))
        self._tombstone_namespace = validate_namespace(tombstone_namespace)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_plan(self, *, request_id: str) -> PhysicalDeletionPlan:
        inventory = await self._store.deletion_inventory()
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise MemoryValidationError("deletion clock must return a timezone-aware datetime")
        now = now.astimezone(UTC)
        records = {(record.namespace, record.record_id): record for record in inventory.records}
        auth = records.get(
            (self._policy.authorization.namespace, self._policy.authorization.record_id)
        )
        if auth is None or _record_ref(auth) != self._policy.authorization:
            raise MemoryConflictError("owner authorization record is missing or changed")
        authorization = _validate_authorization(auth, self._policy)
        # Parse every durable hold before producing a plan.  Malformed controls
        # fail closed rather than becoming a silent empty candidate set.
        hold_refs, _ = _hold_records(inventory, now)
        protected = {
            (ref.namespace, ref.record_id, ref.content_hash): "active retention hold"
            for ref in hold_refs
        }
        ambiguous_hold_record_ids, ambiguous_hold_snapshot_ids = _ambiguous_hold_keys(
            inventory, now
        )
        snapshots = {
            (snapshot.namespace, snapshot.snapshot_id): snapshot for snapshot in inventory.snapshots
        }
        held_snapshot_keys = _held_snapshot_keys(inventory, now)
        blocked_snapshots: list[SnapshotDeletionBlock] = []
        retired_snapshots: list[SnapshotRetirement] = []
        for attested in authorization.retire_snapshot_refs:
            current = snapshots.get((attested.namespace, attested.snapshot_id))
            if current is None or _snapshot_ref(current) != attested:
                raise MemoryConflictError(
                    f"owner-attested snapshot {attested.snapshot_id} is missing or changed"
                )
            eligible_at, retention_reason = _snapshot_eligible_at(
                current, records, self._policy.minimum_raw_age_days
            )
            reasons: list[str] = []
            if retention_reason is not None or eligible_at is None:
                reasons.append(retention_reason or "snapshot retention floor is unavailable")
            elif now < eligible_at:
                reasons.append(f"retention until {eligible_at.isoformat()}")
            snapshot_key = (current.namespace, current.snapshot_id, current.content_hash)
            if snapshot_key in held_snapshot_keys:
                reasons.append("active retention hold")
            if (current.namespace, current.snapshot_id) in ambiguous_hold_snapshot_ids:
                reasons.append("ambiguous active retention hold")
            for source in records.values():
                if _is_control_reference_record(source):
                    continue
                for snapshot_id, snapshot_hash in _payload_snapshot_refs(source.payload):
                    if snapshot_id != current.snapshot_id:
                        continue
                    if snapshot_hash != current.content_hash:
                        reasons.append(
                            f"ambiguous or live binding {source.namespace}:{source.record_id}"
                        )
                    else:
                        reasons.append(f"live binding {source.namespace}:{source.record_id}")
            if reasons:
                blocked_snapshots.append(
                    SnapshotDeletionBlock(snapshot=attested, reasons=sorted(set(reasons)))
                )
            else:
                retired_snapshots.append(
                    SnapshotRetirement(
                        snapshot=attested,
                        eligible_at=eligible_at,
                        reason="owner-attested immutable snapshot past minimum retention",
                    )
                )
        retired_keys = {
            (item.snapshot.namespace, item.snapshot.snapshot_id) for item in retired_snapshots
        }
        snapshot_keys = {
            (snapshot.namespace, member.record_id, member.content_hash)
            for snapshot in inventory.snapshots
            if (snapshot.namespace, snapshot.snapshot_id) not in retired_keys
            for member in snapshot.members
        }
        snapshot_member_ids = {
            (snapshot.namespace, member.record_id)
            for snapshot in inventory.snapshots
            if (snapshot.namespace, snapshot.snapshot_id) not in retired_keys
            for member in snapshot.members
        }
        candidates: list[DeletionCandidate] = []
        blocked: list[DeletionBlock] = []
        for record in records.values():
            ref = _record_ref(record)
            key = (ref.namespace, ref.record_id, ref.content_hash)
            reasons: list[str] = []
            if record.record_type in self._policy.project_lifetime_record_types:
                reasons.append("project-lifetime record type")
            elif record.record_type not in self._policy.deletable_record_types:
                reasons.append("record type is not declared low-value raw")
            if record.payload.get("retention_class") not in (None, "raw"):
                reasons.append("explicit protected retention class")
            if record.payload.get("status") in {"candidate", "active", "validated", "promoted"}:
                reasons.append("decision-visible or high-impact status")
            eligible_at, retention_reason = _record_eligible_at(
                record, records, self._policy.minimum_raw_age_days
            )
            if retention_reason is not None or eligible_at is None:
                reasons.append(retention_reason or "record retention floor is unavailable")
            elif now < eligible_at:
                reasons.append(f"retention until {eligible_at.isoformat()}")
            retained_until = _retained_until(record.payload)
            if retained_until is not None and now < retained_until:
                reasons.append(f"retained until {retained_until.isoformat()}")
            if key in snapshot_keys:
                reasons.append("snapshot member retained; snapshot is not explicitly retired")
            elif (ref.namespace, ref.record_id) in snapshot_member_ids:
                reasons.append("snapshot member hash mismatch")
            if key in protected:
                reasons.append(protected[key])
            if (ref.namespace, ref.record_id) in ambiguous_hold_record_ids:
                reasons.append("ambiguous active retention hold")
            if reasons:
                blocked.append(DeletionBlock(ref=ref, reasons=sorted(set(reasons))))
            else:
                candidates.append(
                    DeletionCandidate(
                        ref=ref,
                        record_type=record.record_type,
                        eligible_at=eligible_at,
                        reason="unreferenced low-value raw record past minimum retention",
                    )
                )
        candidate_keys = {(item.ref.namespace, item.ref.record_id) for item in candidates}
        # Incoming references are evaluated before sealing.  A mismatch is
        # blocked as well, because ProvenanceRef has no namespace field.
        incoming: dict[tuple[str, str], list[str]] = {key: [] for key in candidate_keys}
        for source in records.values():
            if _is_control_reference_record(source):
                continue
            for artefact_id, content_hash in _payload_refs(source.payload):
                for item in candidates:
                    if item.ref.record_id != artefact_id:
                        continue
                    if (source.namespace, source.record_id) == (
                        item.ref.namespace,
                        item.ref.record_id,
                    ):
                        continue
                    incoming[(item.ref.namespace, item.ref.record_id)].append(
                        f"provenance:{source.namespace}:{source.record_id}"
                        if content_hash == item.ref.content_hash
                        else f"ambiguous-provenance:{source.namespace}:{source.record_id}"
                    )
        if any(incoming.values()):
            replacement: list[DeletionCandidate] = []
            for item in candidates:
                reasons = incoming[(item.ref.namespace, item.ref.record_id)]
                if reasons:
                    blocked.append(DeletionBlock(ref=item.ref, reasons=sorted(set(reasons))))
                else:
                    replacement.append(item)
            candidates = replacement
        candidate_keys = {(item.ref.namespace, item.ref.record_id) for item in candidates}
        purge_receipts = [
            receipt
            for receipt in inventory.receipts
            if (
                receipt.record_ref is not None
                and (receipt.record_ref.namespace, receipt.record_ref.record_id) in candidate_keys
            )
            or (
                receipt.snapshot_ref is not None
                and (
                    receipt.snapshot_ref.namespace,
                    receipt.snapshot_ref.snapshot_id,
                )
                in retired_keys
            )
        ]
        plan = PhysicalDeletionPlan(
            plan_id="0" * 64,
            request_id=request_id,
            created_at=now,
            as_of=now,
            inventory_hash=inventory.content_hash,
            policy=self._policy,
            candidates=candidates,
            blocked=blocked,
            blocked_snapshots=blocked_snapshots,
            purge_receipts=purge_receipts,
            retired_snapshots=retired_snapshots,
            tombstone_namespace=self._tombstone_namespace,
            warnings=[
                "only explicitly owner-attested, expired snapshots can be retired; "
                "all others remain retained"
            ],
        )
        return plan.model_copy(update={"plan_id": plan_hash(plan)})

    async def apply(
        self, plan: PhysicalDeletionPlan, *, idempotency_key: str
    ) -> PhysicalDeletionReceipt:
        owned = _validate_plan(plan)
        if owned.policy != self._policy or owned.tombstone_namespace != self._tombstone_namespace:
            raise MemoryConflictError("deletion plan uses another policy or tombstone namespace")
        return await self._store.apply_deletion(owned, idempotency_key=idempotency_key)


__all__ = [
    "DEFAULT_TOMBSTONE_NAMESPACE",
    "DELETED_RECEIPT_KIND",
    "DELETION_OPERATION",
    "DELETION_TOMBSTONE_RECORD_TYPE",
    "DeletedReceipt",
    "DeletedSnapshotReceipt",
    "DeletionAuthorization",
    "DeletionBlock",
    "DeletionCandidate",
    "DeletionHold",
    "DeletionPolicy",
    "DeletionTombstone",
    "DeletionStore",
    "PhysicalDeletionManager",
    "PhysicalDeletionPlan",
    "PhysicalDeletionReceipt",
    "ReceiptEntry",
    "RecordRef",
    "SnapshotDeletionBlock",
    "SnapshotRef",
    "SnapshotRetirement",
    "receipt_entry",
    "StoreInventory",
    "make_inventory",
    "plan_hash",
    "validate_deletion_plan",
    "validate_plan_against_inventory",
]
