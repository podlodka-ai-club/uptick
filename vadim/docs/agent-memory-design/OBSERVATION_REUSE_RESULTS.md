# Observation navigation and reuse — development result

Date: 2026-09-05. Active-goal continuation after the exact-read pilot.

## Protocol and boundary

Question: can literal search navigate an observed payload without a byte
directory, and can fetched evidence be reused without substituting old values
after a new observation?

Plan: `artifacts/observation-reuse-2026-09-05/plan.json`.
Source capsule SHA-256:
`680cd22cef29e3346683f3a9c5b8267d0bf5867d74b8e323939e02443f3916e9`.
Pilot SHA-256:
`6c11e295ef6edc6c958f718bc46107b31623985faa70aa623e6930e03e9014c5`.

Two designed public registries contain 40 records each, opaque item IDs, revision
tags, routing groups and mixed filler text. Their snapshots are 120,484 and
120,387 bytes respectively. No directory/offsets are supplied. Three sequential
questions request a record's revision, its routing group from the same snapshot,
and its revision from a new snapshot. The target occurs once, beyond byte 32,768;
the new revision differs. Future snapshots/questions never enter earlier inputs.

Three conditions use identical questions, model, instructions and schema: full
inline; preview with find/read; and the same preview/tools with carried fetched
chunks. Preview is 1,000 bytes, tool results at most 8 KiB, carried chunks at most
12,000 serialized bytes with oldest-first eviction. Each episode has a fresh
client/archive. Condition order rotates across two fixtures, not a fully balanced
design. Expected/stale answers are computed only after each question, never sent
to the model or used to select fragments. Only actual returned chunks enter carry.

Model: Codex subscription `gpt-5.6-sol`, low effort. Limits: four decisions per
question, 120 seconds per request, 18 questions and at most 72 generations. All
18 questions completed in 28 logical generations. All outcomes, requests, tool
results and telemetry remain in `results/` below the experiment directory.
No simulator actions, hidden state, arbitrary files or evaluator inputs were used.

Independent verification passed **719 checks**. All request prefixes, receipt
digests/source steps, ref chronology, ten exact tool chunks and retained-fragment
lists replay correctly; all outcome flags and aggregate measurements reconcile.
No future revision sentinel or evaluator field entered earlier requests. The
durable record is `independent-verification.json` in the experiment directory.

## Results

| Condition | Correct | Calls | Finds | Input tokens | Output tokens | Generation seconds | Peak user-message bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full inline | 6/6 | 6 | 0 | 193,592 | 455 | 35.49 | 121,718 |
| Find without carry | 6/6 | 12 | 6 | 179,553 | 1,184 | 88.64 | 6,212 |
| Find with carry | 6/6 | 10 | 4 | 153,540 | 944 | 65.67 | 10,758 |

Both second questions had their requested fact in the initial carried fragment;
both were answered without a new find/read. Both changed-snapshot questions
initially exposed the stale old revision and lacked the current revision. The
model searched the new reference and answered correctly in both. Evaluator-only
availability flags establish these were actual reuse/stale-exposure opportunities.

Carry used **14.5% fewer input tokens** and two fewer model calls than repeated
search. Versus full inline it used **20.7% fewer input tokens** and **91.2% fewer
peak user-message bytes**, but took **1.85×** the generation time. There were no
failed tools/questions. Adapter telemetry reports 28 requests and zero retries;
hidden SDK retries are not inspected. Times exclude setup/local work/cleanup;
user bytes exclude system/schema/provider context.

This supports navigation/reuse in two designed cases, not action-selection,
cross-run learning or SRE utility. The previous directory pilot has a different
fixture and is not a concurrent causal control. Do not pool the studies or infer
that truncating all results by default improves task performance.

## Implementation and checks

Experimental `ObservationArchive.find(ref, text, offset, max_bytes)` searches
sanitized canonical UTF-8 literally, case-sensitively, only inside an issued
run-local record. It is not regex or value-aware JSON search: escaped text may
need an escaped query. Queries are at most 256 bytes and must fit the requested
window. Exact byte coverage, digest, source step and historical markers survive.
No-match applies to the searched suffix; reads/finds do not refresh retention.
The component remains unwired from the runner/CLI.

Ten archive tests cover exact Unicode slices, caps, multiple/no matches, ref
isolation and actual A/B/find-A/C eviction. Full suite: **677 passed, 2 opt-in
live skips**. Ruff and quick review passed; scope SHA-256
`51f1b16ff83561ac1e156ef1541f66a14e0c40afa73463e8bae960d5a72234b4`.
Two persisted v2 review markers passed validation.

Pre-execution review fixed a harness failure: after timeout the Codex client may
be closed. The pilot replaces it before later questions and marks failed-call
usage unavailable. A local fake-client timeout test retains all 18 questions
and closes all replacement clients. Its first attempt had a test-only importlib
registration error; both attempts remain separate, and the corrected
`fault_preflight.py` passed. Neither contacted a provider.

## Next full-task gate

The generic runner validates the environment-owned response schema and has no
archive action path. A hidden retrieval loop would change budget/trace contracts.
Keep optional archive integration separately versioned and charge its actions.

The next full public SRE plan is
`artifacts/sre-completion-2026-09-05/sre-plan-06.json`: current history plus neutral
guidance, unchanged public briefing/schema, Sol/low, no persistent memory, seed42,
160 decisions and 2,400-second wall budget. Archive tools remain absent. Its
rationale is the earlier public-prefix decision evidence, not green archive tests.
Successful full SRE behavior and the controlled persistent-memory comparison
remain unfulfilled goal requirements.

Dev-06 subsequently started as run `Vp0ETbGWqA6FTmmZUWNg8aww`; consult its live
process/outcome rather than treating this document as a current-status source.
The first launcher invocation used a wrongly relative plan path and failed
before plan load/network calls; it is retained in `dev-06-launcher-failure.json`.
The corrected invocation uses the unchanged plan. Independent preflight passed
and records that the capsule also contains the pre-existing, untouched and inert
`EpisodicRecallSettings` declaration. This is disclosed dirty-tree inclusion,
not an activated factor in the no-memory run.
