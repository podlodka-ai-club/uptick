# Stage 0 baseline freeze

`profile.json` is preregistration input. It declares the B0/B1 conditions,
disjoint training/evaluation seeds, replicate indices, canonical metrics and
failure rules. It is not run evidence and must not contain fabricated results.

From the repository root, the offline planner resolves the current source
revision, scoped dirty-tree state, source-tree, project, runtime, planner, and
`uv.lock` hashes into a manifest. Prompt, settings, and endpoint fingerprints
remain unresolved in this checked-in preregistration profile, so any report
produced from it is fail-closed as non-promotable:

```bash
cd vadim
uv run python scripts/stage0.py plan \
  --profile experiments/stage0/profile.json \
  --output artifacts/stage0/baseline-v1/manifest.json
```

The report command consumes retained attempt records. One JSON object per line
is required in `attempts.jsonl`; every retry is retained with a globally unique
`attempt_id`, `attempt_index`, and `retry_of`. A retry is permitted only after a
retry-eligible `failed` or `interrupted` attempt, never after a completed or
excluded result. Every expected block/condition cell should have a terminal
attempt, including terminal `failed`, `interrupted`, and `excluded` attempts.
The latest attempt must itself be terminal before a cell is selected; this rule
and the full coverage inventory are included in the report.

```bash
uv run python scripts/stage0.py report \
  --manifest artifacts/stage0/baseline-v1/manifest.json \
  --attempts artifacts/stage0/baseline-v1/attempts.jsonl \
  --output artifacts/stage0/baseline-v1/report.json
```

Reports retain every attempt record and counts for every attempt status. Metric
distributions use only selected terminal attempts, and include only completed
attempts with that metric. Paired B1-minus-B0 deltas include only complete
blocks with both selected conditions completed; incomplete blocks remain listed
and are never silently treated as wins or losses. B0 terminal attempts must
carry a no-memory audit hash and no memory metadata. B1 attempts and frozen
snapshots must carry one decision namespace per replicate, strict carry order,
the same immutable frozen input across evaluation seeds, and exact non-visible
quarantine provenance/audit linkage before evidence can be complete. Reports
also bind the manifest hash and retained-attempts hash. Consumers must load the
sealed manifest and call `verify_report(manifest, report)` before trusting a
report; the report's redundant summaries are not independently authoritative.

Every retained terminal attempt also carries a content-addressed result and a
raw-capture audit bound to the resolved raw-content policy. Prompt,
observation, and decision-trace capture states are recorded independently, so
an enabled body cannot be silently reported as disabled and a disabled body
cannot be silently persisted. Completed attempts require every enabled class
to be captured; a body-less quarantine makes evidence incomplete. Earlier
failed or interrupted attempts may explicitly state that a body was not
emitted before the corresponding execution boundary. The offline reporter
checks these declarations, fingerprints, hashes, and reference relationships;
it does not dereference external bodies or claim to be their storage system.
The future live runner and retention audit remain responsible for producing
and verifying those external artifacts.

The canonical primary metric is `final_balance_minor`. Completion status,
lost purchases, lost revenue, server cost, and deployment cost are recorded as
guardrails. No pass@k or best-of-k selection is valid promotion evidence.

Current offline artifacts are `manifest.json`, externally supplied
`attempts.jsonl`, and generated `report.json`. A future live runner will also
produce the result, trace, and snapshot artifacts shown below. All generated
content is ignored by git:

```text
artifacts/stage0/<experiment>/
  manifest.json
  attempts.jsonl
  results.jsonl                 # future live runner
  traces/<block-id>.jsonl       # future live runner
  snapshots/*.json             # future live runner
  report.json
```

The planner and reporter perform only local file and git-revision operations,
write atomically, and only write under `artifacts/stage0/`; existing outputs
require `--force`. They reject input/output path collisions and source/lock
targets. They do not call the
simulator, an LLM provider, or any network service. A live baseline is blocked
until credentials/network and a runner integration exist that can resolve all
provenance fingerprints, capture retries, freeze B1 memory, allocate a distinct
quarantine overlay for every evaluation cell, and preserve raw content under
the declared redaction policy.
