# Stage 12 retention verification — 2026-09-05

The physical-deletion implementation is pinned to `98c0c783bcfd4b92529f542eb4dedce7f7790f97`.
The fresh 113-file source capsule has SHA-256
`801802af05e0b4db8aa39f96040a374be05cd6a444991438c6e8152e26e99e30`.
The isolated SQLite diagnostic exited 0 and passed independent read-only
reopening/count/integrity checks. No real experiment data was deleted.

## Seeded fixture outcome

| Measure | Before | After |
| --- | ---: | ---: |
| Bulk raw records | 200 | 6 |
| Bulk payload bytes in live rows | 981378 | 31264 |
| Live snapshots | 2 | 1 |
| Live snapshot members | 201 | 2 |
| SQLite file bytes | 2314240 | 2314240 |

All 194 eligible rows were removed, along with one explicitly owner-attested
old snapshot. The six bulk survivors were independently protected by an active
hold, a summary provenance reference, project-lifetime retention class, a
future retention deadline, candidate status, and young age. A second snapshot
and both its members stayed intact. All surviving record content hashes were
unchanged. A default plan without explicit snapshot retirement deleted nothing.

The test deliberately aged only its newly constructed snapshot metadata and
matching receipt to 120 days before planning; member hashes were unchanged,
and the young row was added after snapshot creation. This is a seeded retention
fixture, not a claim that a real deployment has run for 120 days.

After reopening SQLite there were 194 record tombstones and one snapshot
tombstone. The 194 write receipts were replaced by payload-free deletion
markers, with one corresponding deleted-snapshot receipt. The sealed plan was
idempotent under its original key. Recreating the retired snapshot ID in another
namespace was rejected. SQLite integrity and foreign-key checks passed.

The live raw payload shrank by about 96.8%; the database file did not shrink.
SQLite can reuse free pages. This does not establish forensic secure erase,
a constant total database size, or bounded project-lifetime audit metadata.

## Contract and regression verification

The final locked offline suite passed **655 tests**, with two explicit opt-in
live skips; Ruff passed. Stage 12 contributes 31 focused cases including backend
parity, stale/forged plans, direct-store replay, exact live bindings, unknown
provenance, snapshot identity, recursive forward-minor control rejection,
completion-based retention, SQLite rollback and concurrent apply.

Raw records associated with a run require its unique typed terminal outcome
in the same namespace. The 90-day floor starts at the later of row creation and
run completion; snapshots inherit member completion floors. Missing, ambiguous
or invalid completion evidence blocks deletion. Experiment-associated records
without authoritative completion also stay protected. `run-outcome` is mandatory
project-lifetime evidence. The seeded bulk fixture is standalone raw data;
completion-floor behavior has separate focused regressions.

Final local quick review and the independent targeted destructive-path review
were clean after the identified defects were fixed. The administrative API
requires a host-controlled owner record; hashes and boolean attestations are not
an identity or ACL system. Operational commands and authority requirements are
in [the retention guide](STAGE12_RETENTION_GUIDE.md).

## Retained evidence

Artifacts remain ignored under `artifacts/retention-evaluation-2026-09-05/`.
The plan precedes the run, pins the runner/source, and declares expected counts.
The saved runner refuses an existing output directory.

| Artifact | SHA-256 |
| --- | --- |
| `plan-01.json` | `59564a9f37516e4bf270c4c2da3f2672a368ca27d99e54ad9cef3919f98e72e0` |
| `run_smoke.py` | `3a186f629617a6d79c31397553118b9191059407bfd4a35a0905b9d6857aaa6a` |
| `sqlite-01/report.json` | `4b302b5305c1a69a4f4d68339c92b5889df89c2d3ae4a58463aedc87d3b29f14` |
| `sqlite-01/sealed-plan.json` | `1c39e9ae5510e183385eead37cb83c5c44135f1c82d67741c7e5fa47456a5bb3` |
| `sqlite-01/receipt.json` | `baf26381d8c640dd4ec7ee04c6cea56241742225e77ca9a8ca1786a4ff3baeba` |
| `sqlite-01/independent-verification.json` | `cc23fc6c68478a4f09ac39d5471d99289a9f20c0f5872f0f2648ad0f80a66d5b` |
