# Stage 12 retention and deletion guide

This guide describes the finalized maintenance and physical-deletion surfaces
under `uptick_agent.memory`. No deletion was run while writing it.

## Two separate operations

| Operation | Entry point | Effect on source evidence |
| --- | --- | --- |
| Archive-preserving maintenance and summaries | `memory_maintenance_cli` / `MemoryMaintenance` | Reads one exact snapshot, then records a maintenance plan or application manifest. Source records and the input snapshot remain. Supersession and age decay affect the operational retrieval view only. |
| Physical deletion of eligible raw data | `memory_deletion_cli` / `PhysicalDeletionManager` | Applies one sealed, owner-authorized plan. Eligible raw rows may be removed; deletion metadata, hashes, tombstones and the authorizing decision remain. |

Maintenance is the right operation for extractive summaries, links,
supersession, and index reduction. It is intentionally not a deletion bypass:
its plan records `physical_delete` as unsupported and its applied manifest says
`source_archive=retained`. A summary does not replace the evidence that
supports it.

Physical deletion is a separate administrative capability. The online model,
planner, and runner do not self-authorize it. A planner can propose a dry-run
plan, but both planning and application require a typed owner authorization
record and the store rechecks that authorization and the live inventory.
This is an administrative API boundary, not an identity/ACL system: the host
must restrict who can write retention-control records and invoke deletion.
A content hash is an integrity check, not an authenticated human signature.

## Retention classes and floors

The audit profile retains raw prompt, observation, and trace bodies and their
snapshots for at least 90 days after the producing run or experiment completes.
The physical deletion contract enforces `minimum_raw_age_days >= 90`. For a
run-associated row it requires a unique valid terminal `run-outcome` in the
same namespace and computes eligibility from the later of record creation and
`finished_at`. Missing, ambiguous, malformed or timezone-naive completion
evidence blocks deletion. Explicit experiment associations without an
authoritative completion contract also remain protected. Only standalone raw
records without a producing run/experiment use creation time alone. Snapshot
retirement also respects its members' completion floors. A `retained_until`
obligation in any payload extends the effective floor.

Summaries, validation and candidate-validation records, manifests, promotion,
approval, rollback, audit, lesson, hypothesis, playbook, consolidation, and
retention-control records are project-lifetime evidence. Project lifetime ends
only through an explicit project decommissioning and archival or deletion
decision. The policy rejects an incomplete lifetime-protection list and rejects
an overlap between deletable record types and lifetime types.

Records are also blocked when they have a protected retention class, a
decision-visible status (`candidate`, `active`, `validated`, or `promoted`), a
live snapshot membership, an active hold, or live/ambiguous provenance. Raw
provenance needed by an active decision-visible candidate cannot expire merely
because its age crossed 90 days.

## Holds and conservative references

An active `retention-hold` record protects its exact `RecordRef` and/or
`SnapshotRef`. A hold with `active_until=null` remains active; otherwise it is
active until its timezone-aware deadline. Malformed or duplicate hold metadata
fails closed.

All snapshots are protected by default. A snapshot is considered for retirement
only when the owner authorization contains the exact snapshot namespace,
snapshot ID, creation time, content hash, and member list. Every other snapshot
remains unchanged and continues protecting its members; `blocked_snapshots`
lists requested retirements that were blocked.

Provenance payloads can contain an artefact ID without a namespace. The planner
therefore resolves them conservatively: an exact ID/hash reference protects the
matching candidate, and an ID with a missing or different hash is ambiguous and
also blocks deletion. Snapshot-member ID/hash mismatches block as well. Administrative control payloads must pass recursive no-discard validation;
unknown nested fields cannot silently disappear through forward-minor schema
compatibility. Valid controls and already-created deletion tombstones are
metadata, not live causal provenance.

Snapshot identity is never just a display ID. Plans and holds compare
`(namespace, snapshot_id, content_hash)` plus the immutable member list. The
SQLite live snapshot table currently keys `snapshot_id` globally, and the store
also rejects a retired ID from being recreated in another namespace; callers
must not treat a same-ID cross-namespace snapshot as interchangeable.

## Owner authorization and snapshot retirement

`DeletionAuthorization` is a typed record with the exact `policy_id`,
`policy_version`, `scope="physical-delete"`, minimum raw age, and deletable
record types. Its `retire_snapshot_refs` list is empty by default. If it is
non-empty, `no_active_external_bindings_attested=true` is mandatory. This is an
operator attestation that external consumers were checked; it is not inferred
from a model response or fabricated by the planner.

For each requested snapshot, planning and application recheck the current
snapshot against the owner-attested `SnapshotRef`, the 90-day floor, active
holds, and every live or ambiguous payload binding. A changed or missing
snapshot, stale hash, active hold, or external-binding concern blocks
retirement.

The smallest typed record-creation example is:

```python
from datetime import UTC, datetime

from uptick_agent.memory.deletion_contracts import DeletionAuthorization
from uptick_agent.memory.stores.contracts import RecordWrite
from uptick_agent.memory.stores.sqlite import SqliteStructuredStore

store = SqliteStructuredStore("/var/lib/uptick/memory.sqlite3")
authorization = DeletionAuthorization(
    policy_id="project-memory-retention",
    policy_version="1.0",
    scope="physical-delete",
    minimum_raw_age_days=90,
    deletable_record_types=["experience-transition", "episode", "generic-evidence"],
)
receipt = await store.append(
    RecordWrite(
        namespace="controls",
        record_id="retention-owner-2026-09-05",
        record_type="retention-authorization",
        payload=authorization.model_dump(mode="json"),
        created_at=datetime.now(UTC),
    ),
    operation="retention-authorization",
    idempotency_key="retention-owner-2026-09-05-write",
)
```

The CLI takes `--authorization-namespace controls` and
`--authorization-id retention-owner-2026-09-05`; it obtains the stored
content hash itself. Do not hand-write a hash or copy one from an earlier
inventory.

To authorize a snapshot as well, first read its current `MemorySnapshot` and
construct the exact `SnapshotRef` from that returned value, then include it in
the same typed authorization:

```python
from uptick_agent.memory.deletion_contracts import DeletionAuthorization, SnapshotRef

snapshot = await store.get_snapshot(snapshot_id="raw-freeze")
assert snapshot is not None
snapshot_ref = SnapshotRef.model_validate(snapshot.model_dump(mode="json"))
authorization = DeletionAuthorization(
    policy_id="project-memory-retention",
    policy_version="1.0",
    scope="physical-delete",
    minimum_raw_age_days=90,
    deletable_record_types=["experience-transition", "episode", "generic-evidence"],
    retire_snapshot_refs=[snapshot_ref],
    no_active_external_bindings_attested=True,
)
```

The second example only constructs the authorization value; it does not retire
the snapshot. The external-binding boolean is required because the store
cannot inspect consumers outside its inventory.

## CLI workflow

The physical-deletion CLI defaults to a dry run. These are the actual argument
forms; the dry run requires the owner authorization reference:

```bash
uv run --locked python -m uptick_agent.memory_deletion_cli \
  --sqlite-path /var/lib/uptick/memory.sqlite3 \
  --plan-path /var/lib/uptick/retention/plan-2026-09-05.json \
  --request-id retention-2026-09-05 \
  --authorization-namespace controls \
  --authorization-id retention-owner-2026-09-05 \
  --policy-id project-memory-retention \
  --policy-version 1.0 \
  --minimum-raw-age-days 90 \
  --tombstone-namespace memory:deletion:tombstones
```

The plan file is sealed atomically. An existing path containing different
bytes is a conflict. Review `candidates`, `blocked`, `blocked_snapshots`,
`purge_receipts`, `retired_snapshots`, the inventory hash, and warnings before
any apply step.

Apply loads that exact plan; it does not recompute candidates from a current
model or accept new authorization arguments:

```bash
uv run --locked python -m uptick_agent.memory_deletion_cli \
  --sqlite-path /var/lib/uptick/memory.sqlite3 \
  --plan-path /var/lib/uptick/retention/plan-2026-09-05.json \
  --request-id retention-2026-09-05 \
  --idempotency-key physical-delete:retention-2026-09-05 \
  --apply
```

For archive-preserving summaries, the separate maintenance CLI is:

```bash
uv run --locked python -m uptick_agent.memory_maintenance_cli \
  --sqlite-path /var/lib/uptick/memory.sqlite3 \
  --namespace raw \
  --snapshot-id raw-freeze \
  --request-id maintenance-2026-09-05

uv run --locked python -m uptick_agent.memory_maintenance_cli \
  --sqlite-path /var/lib/uptick/memory.sqlite3 \
  --namespace raw \
  --snapshot-id raw-freeze \
  --request-id maintenance-2026-09-05 \
  --plan-id <persisted-plan-hash> \
  --apply
```

The first command creates and persists a snapshot-bound dry-run plan. The
second applies only that persisted plan after checking the snapshot ID, content
hash, members, and current hold protection. It records an application manifest
and keeps the source archive.

## Store guarantees at apply time

SQLite opens a fresh connection for each operation. Physical deletion runs in a
`BEGIN IMMEDIATE` transaction. It validates the sealed plan, recomputes the
current inventory, checks the inventory hash, verifies every exact record and
snapshot reference, rechecks owner authorization, holds, provenance, and
receipt kinds, then commits all row changes together. A validation or storage
failure rolls the transaction back.

The deletion idempotency key is bound to the deletion operation and plan hash.
Replaying the same key and plan returns the prior receipt with
`already_applied=true`. Reusing a key for another plan conflicts, and applying
the same plan under a different key also conflicts. Concurrent SQLite applies
are serialized by the store lock and transaction.

The plan's inventory hash and exact content-addressed references make stale
plans fail closed. A changed or missing record, snapshot, owner record, live
provenance source, receipt, hold, or snapshot member set must be replanned.
No namespace-less fallback is used to turn a stale or cross-namespace identity
into permission to delete.

## Receipts, tombstones, and limits

For a deleted raw record, the store removes the source row, retains a
`RecordRef` containing namespace, record ID, and content hash, and writes a
`memory-deletion-tombstone` in the configured tombstone namespace. For a retired
snapshot it removes the snapshot and members, retains the exact `SnapshotRef`
in a snapshot tombstone, and writes a corresponding tombstone record.

Receipts listed in `purge_receipts` are converted to typed
`DeletedReceipt` or `DeletedSnapshotReceipt` markers containing operation,
idempotency key, input hash, deleted identity, timestamp, and plan ID. They do
not retain the deleted payload. Tombstones and deletion receipts remain
project-lifetime audit metadata, so a retired record or snapshot cannot be
silently recreated under the same identity.

This contract describes logical SQLite row deletion and retained audit
metadata. It makes no claim about secure erase, filesystem disk shrink, or a
constant total database size.
