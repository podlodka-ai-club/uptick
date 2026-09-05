# Agent Memory implementation handoff

Updated: 2026-09-05 (Asia/Yekaterinburg).

## Resume point

- This continuation is implemented and verified. Environment/tool boundary:
  `62bce25`; filtered logs and metric history: `373f6ee`; provider-compatible
  CIDR schema: `2fc633b`; lossless public timestamps: `c92e094`. Branch remains
  `codex/vadim-agent-memory`; resolve the latest documentation/remote SHA in Git.
- Canonical model construction follows physical startup. The environment
  supplies a frozen decision schema and the actual sanitized server
  `commands_markdown`, without a local startup fallback. The generic runner
  preserves environment actions through context, traces and SQLite. Startup
  artifacts are bound to sealed evaluation traces; missing/mismatched input
  fails before the provider while retaining the physical ID.
- `simulator/decisions.py` publishes `SimulatorV2Decision`; simulator actions
  and dispatch remain adapter-owned. Historical `V2NextStep` is a compatibility
  schema. `query_logs` exposes public filters/cursors and preserves incremental
  reader state; `query_metrics` exposes historical windows and selected series.
  Both preserve RFC3339 fractions exactly through decision validation and HTTP.
  All 18 public control commands remain covered. See `PUBLIC_TOOL_COVERAGE.md`.
- Final verification: **583 passed, 2 opt-in live skips**, Ruff and changed-file
  formatting clean. All **56 historical schemas/identities** remain identical.
  Root's focused implementation reviews are clean. Real tests additionally
  verified nonempty repeat/cursor reads, the exact boundary error and metric
  window, plus final model schema acceptance. No background model run remains.
- Three separately retained controlled learning-cycle experiments passed
  independent request/provenance/outcome verification. Each trained 8/8,
  retained 24 attempts and 28 logical decisions, reopened SQLite and read
  44 frozen members with six selected hypothesis IDs. `sol-low-01`: none 4/8,
  hypotheses 6/8 (4 wins, 2 losses, 2 ties). `sol-low-02`: 4/8 versus 8/8.
  `sol-low-03`, on frozen `62bce25` after the architecture refactor: 4/8 versus
  8/8. The latter two each have 4 wins, 0 losses and 4 ties. No retained runtime
  failures, timeouts or cleanup errors. Keep all three results separate.
- These are variants of one designed causal family, not a held-out SRE or
  xMemory utility result. Evaluator answers stay outside model requests;
  observed training transitions supply evidence. Frozen evaluation writes
  stay isolated. No activation threshold was weakened. Exact pins and limits:
  `LEARNING_CYCLE_RESULTS.md`.
- Live observability work retained two 60-decision/600-second diagnostics.
  First, `bk9rZlWjelDngsa97veov1Po`, failed before a decision because the provider
  rejected `ipv4network`. Second, `K1Ov1amam35yi4XjfXD4Uw1Q`, accepted the CIDR
  correction, executed 42 decisions and timed out. It selected query_logs 3
  times and query_metrics twice, reaching 14.4836% of the horizon. Last metric
  at step 40: uptime 0.9993373478400721, downtime 58.016577125 seconds. These
  are intermediate measurements; **0/2 demonstrated SLO successes**.
- The live empty-window investigation found nanosecond truncation: a to-bound
  ending .524467 returned zero errors; original .524467913Z returned the exact
  stopping error. The c92e094 canonical decision/HTTP path now returns that row,
  repeats it and preserves the metric window. A separate 2-decision schema
  check, `T3t3dlx2KGrNrlqhWv1qNJLC`, exited 0 on c92e094, still nonterminal.
  Direct smokes and the failed pre-HTTP verification helper remain recorded.
  See `OBSERVABILITY_RESULTS.md`; do not turn these checks into SLO claims.
- Next effectiveness priority: use precise public error windows to diagnose and
  test sufficient corrective capacity through a full SRE horizon. Declare a
  new development experiment before running; preserve the failed diagnostics.
  A successful no-memory SRE policy is still unproven. Live memory activation
  and held-out benefit also require authoritative immutable world/family
  identities, absent from the public API. Seed 42 remains development data.
- Stages 1–5 are complete. Stage 6 and A0–A9 compositions are implemented
  experimentally; their integration matrix is complete. Held-out utility and
  default-promotion gates remain open. xMemory is optional, with no full
  upstream generation/embedding effectiveness run or immutable snapshot export.
  No fair comparison with Alex's actual agent has been executed.
- Work only below `vadim/`. The modified root `README.md` and untracked root
  `docs/` belong to the user; do not edit or stage them. Scoped commits/pushes
  on this branch remain authorized. Runtime artifacts are ignored and must
  never be committed. Root owns planning, architecture, review and verification;
  delegate bounded implementation. Ponytail remains active.

## Read next

1. `docs/agent-memory-design/EXPERIMENTAL_MEMORY_GUIDE.md`: current modules,
   configuration, evaluation/maintenance commands and operational limits.
2. `docs/agent-memory-design/REMAINING_EXECUTION.md`: implementation scope,
   accepted integration decisions and outstanding research work.
3. `docs/agent-memory-design/SIMULATOR_V2_EVALUATION_PROFILE.md`: separately
   versioned uptime/cost protocol; the historical Stage 0 balance profile remains
   unchanged.
4. `docs/agent-memory-design/STAGE6_V2_DIAGNOSIS.md`: why current live evidence
   does not activate lessons or demonstrate learning utility.
5. `docs/SIMULATOR_V2_ADAPTER.md`: API details and historical development pilots.
6. `docs/agent-memory-design/V2_LIVE_INTEGRATION_RESULTS.md`: sealed smoke and
   A0–A9 integration identities, outcomes and verification.
7. `docs/agent-memory-design/ARCHITECTURE_AUDIT.md`: current completeness,
   import-boundary correction and remaining evidence/implementation gaps.
8. `docs/agent-memory-design/AGENT_COMPARISON.md`: comparison with inspected
   `simple_agent`; its provisional identification as Alex's agent is withdrawn.
9. `docs/XMEMORY_INTEGRATION.md`: optional research xMemory integration and its
   upstream verification limits.
10. `docs/agent-memory-design/V2_POLICY_GUARD_RESULTS.md`: observed blind-wait
    failure, policy 1.1 correction and operation-polling diagnostics.
11. `docs/agent-memory-design/LEARNING_CYCLE_PLAN.md`: controlled durable learning
    experiment, paired decisions, evaluator separation and limits.
12. `docs/agent-memory-design/LEARNING_CYCLE_RESULTS.md`: all three controlled real-model results,
    independent request/provenance verification and exact source/manifest seals.

13. `docs/agent-memory-design/OBSERVABILITY_RESULTS.md`: real tool/model attempts,
    discovered CIDR/timestamp defects, fixes and remaining SRE limitations.

## Shared Granola notes

The supplied share token resolves to Hacker Sprint #2, containing four Team
Uptick notes. Public summaries were saved under ignored
`artifacts/granola-hacker-sprint-2026-09-05/`. These are not verbatim transcripts:
the connected transcript tool returned not found for all four meeting IDs, and
the shared pages expose notes/summary popovers. Never describe the extraction as
full transcripts. Sync 3/4 distinguish a simple/oracle baseline from Alex's
multi-step agent, but do not prove this repository's `simple_agent` is an oracle.

## Architecture audit and optional xMemory

- A focused contract import previously loaded concrete memory modules through
  eager package exports and settings imports. Lazy public exports and neutral
  settings now isolate contracts; recursive architecture checks enforce memory
  independence from simulator, provider and xmemory implementations.
- Existing public imports and all four historical sealed experiment hashes are
  compatible. The optional `xmemory` field is absent from old serialization;
  enabling it requires configuration schema 1.3.
- The assumed target is HU-xiaobai/xMemory, upstream revision
  `375ae1495095aa14a39eb169f83737f4779391c6`. This is distinct from hosted
  xmemory.ai; the user has not yet answered the disambiguation question.
  The adapter is outside the memory core, injected through a small protocol,
  and composes through the native orchestrator and runner-facing port.
- xMemory does not support immutable snapshot export through its public facade.
  Enabled xmemory configurations are rejected by `evaluate-v2` before artifact
  or client creation. It is not a new A0–A9 effectiveness result.
- Upstream facade forwarding was exercised with an injected fake memory system
  and stubbed heavy imports. A full upstream embedding/LLM pipeline was not
  installed or run; do not describe that smoke as end-to-end xMemory validation.
- Sibling `simple_agent` locked tests passed 40/40 with one live test skipped.
  Our richer architecture does not establish better incident handling. No fair
  shared-protocol agent comparison has been executed.

## Delivered runtime

- v2 remains the CLI default; explicit v1 compatibility is preserved. A
  versioned decision policy budgets error-stopping waits against the remaining
  horizon. Policy 1.1 restores first-error stopping unless current public metrics
  prove the full-horizon SLO unrecoverable. Pending operations preserve their
  requested duration. The v2 policy exposes public horizon/pending-operation
  hints; canonical startup instructions come from the environment. Resource
  summaries distinguish backend/database counts. Portable reasoning effort,
  filtered historical observability and provider telemetry are supported.
- A0–A9 are real compositions: no memory, legacy, episodes, lessons, world
  hypotheses, explicit consolidation, advanced retrieval, playbooks, tool
  knowledge and operational episode decay. Four targeted ablations are
  supported. Disabling mandatory contradiction validation is unsupported.
- Candidate generation is separate from activation validation. Activation needs
  two completed eligible first learning runs across two immutable contexts,
  complete assembler provenance and counter search, and no unresolved
  contradictions. Evaluation contributes neither support nor counters.
- Lesson manifests use schema 1.1 with required acceptance and retention
  metadata. Legacy batches missing those fields fail closed; revalidate retained
  evidence explicitly rather than synthesizing acceptance.
- Consolidation runs only through an explicit out-of-band operation. Immutable
  dry-run plans are revalidated before idempotent apply. New applied snapshots
  cannot discard previously admitted evidence. The latest complete plan governs
  decision visibility; old receipts cannot resurrect disputed candidates.
- Retrieval is replaceable and budgeted before ranking. Presets allocate 4000
  UTF-8-byte upper-bound units per module and 16000 globally. The old 1000 cap
  admitted none of 26 measured real episode views. Semantic embeddings, graph
  expansion and learned queries are unimplemented research alternatives.
- Maintenance plans retain sources, summaries/links and supersession evidence,
  enforce holds and retention floors, and support operational episode age decay.
  Physical deletion is unimplemented; stored history can continue growing.

## Telemetry instrumentation and live checks

The historical matrix identified by source `2e0b411` is the old null-counter
baseline: its attempt telemetry does not provide stored-artifact totals or
aggregate module-lifecycle counters. The telemetry instrumentation patch is
applied in commit `fdd7865`. It adds typed per-module telemetry for construction,
reads, validated nonempty contributions entering the global merge, writes,
finalization and consolidation; forwards those rows through the runtime and
evaluation facade; and reports stored-record counts. Training counts are
cumulative across a condition's training namespaces, while evaluation counts
cover only the current attempt's isolated output namespaces. Frozen input is
reported separately as `snapshot_members`. The legacy `remember()` compatibility
path bypasses the structured orchestrator and is therefore outside these module
counters. `finalization_events` exists on per-module runtime telemetry; the
retained attempt `MemoryTelemetry` schema has no aggregate finalization field.
`module_contribution_events` counts validated nonempty contributions entering the
global merge, not selected unique-item counts. Missing or partial values remain
unavailable (`null`).

The SDK shutdown fix is applied in commit `e35fd58`: production-owned turns use
shielding, public client close, draining the turn task, and cancellation
propagation in that order; borrowed-turn behavior is unchanged. The historical
matrix process was terminated with SIGTERM (exit 143) after printing and
independently verifying its report. The shutdown regression was red with the old
five-second timeout and green through the real router. These checks do not
rewrite the historical matrix or close an evidence gate.

Live verification is complete against source
`e35fd581b57318ff062fc01ea1d62c1e92268978`. The four-cell telemetry smoke
completed 4/4 cells with 0/4 passing SLO and CLI exit 0; verification covered
12 lifecycle events, 11 durable artifacts and 2 bindings. Training A0 measured
zero module events with `stored_artifacts=5`; training A3 measured construction
`2`, reads `2`, writes `1` and `stored_artifacts=8`, with the remaining A3
reported aggregate counters at zero. The four-cell short-wall cancellation probe used
A0/A3, training/evaluation seeds 43/44, eight decisions and a 12-second wall
budget; all 4/4 cells were interrupted and each retained
`provider_telemetry.request_count=1`. After all four cells, the CLI exited with
code 0 and the evidence passed verification. Probe details are recorded in
`docs/agent-memory-design/V2_LIVE_INTEGRATION_RESULTS.md`. These checks do not
close an evidence gate.

## Evaluation integrity

- Manifest precedes all external calls; each cell retains requested/running/
  terminal lifecycle, including startup failures and incomplete horizons.
- Source, dependency, prompt, generation settings, policy, estimator and endpoint
  pins must match actual execution. Namespaces bind to the sealed manifest hash.
- Training provenance is validated before immutable snapshot binding. Evaluation
  reads only the bound records, including verified nested historical snapshots;
  writes/finalization/audit use isolated output namespaces.
- Real v2 sessions receive experiment-owned environment/scenario attribution.
  Unknown immutable world identity creates no eligible learning declaration.
- First attempts remain primary. Failed/incomplete cells stay in denominators;
  paired costs require both completed runs to pass SLO. Unknown/partial usage
  and monetary costs remain unavailable, and mixed currencies are not averaged.
- Existing lifecycle/training namespaces cannot be resumed. Each diagnostic
  rerun needs a new sealed manifest/output directory and keeps previous evidence.

## Live evidence and outstanding gates

The owner authorized simulator runs at `http://81.176.229.58:8080`.
Raw sanitized artifacts remain ignored under `artifacts/`.

- v2 client smoke passed 14 checks; the live adapter integration test passed
  again during this continuation.
- The completed A0–A9 and targeted integration matrix is recorded in
  `docs/agent-memory-design/V2_LIVE_INTEGRATION_RESULTS.md`: 42/42 cells have
  terminal records, with 41 completed and 1 interrupted; 0/42 passed SLO.
  Training completed 27/28 attempts and evaluation completed 14/14. All 14
  bindings were valid, and independent verification passed for 126 lifecycle
  events and 98 durable artifacts. This verifies bounded execution and
  reporting only; it does not close the held-out utility or promotion gates.
- Development pilots 6 and 8 exhausted 40 decisions without completing the
  horizon. Pilots 7 and 9 completed seven days but failed SLO: uptime about
  0.266. Pilot 9 used catalog-verified `gpt-5.6-sol`, low effort, 27 decisions,
  268.86 wall seconds, run `XQqybzunrxvpO8ul2VvQYZ7w`.
- Policy-1.1 diagnostic `TaQUWekTn2Vq3YM3ch0MUTc8` stopped at 80 decisions,
  14.54% of the horizon, current uptime 0.9954124323 and SLO null. The prompt-only
  follow-up `baRQeQU3gPUvlcfkkh7XzOT0` timed out after 900 seconds/78 steps,
  covering 14.55%; its last measured uptime was 0.9957498183 at step 74, not
  a terminal result. Frozen source pins and exact metrics are in
  `docs/agent-memory-design/V2_POLICY_GUARD_RESULTS.md`.
- Seed 42 is a development seed, never an unseen holdout. Model/budget diagnostic
  retries are not a controlled memory ablation.
- Public API world-content hashes and causal-family identity are unavailable.
  Do not replace them with seed labels or API-schema hashes. This prevents
  qualifying new live derived knowledge and closing causal-family holdout gates.
- The bounded A0–A9 integration matrix is complete as recorded in
  `docs/agent-memory-design/V2_LIVE_INTEGRATION_RESULTS.md`; it checks execution and reporting. A final
  learning-utility experiment still needs authoritative world identities, a
  locked causal-family split, sufficient complete runs and the specified
  promotion/rollback/approval evidence. No successful SRE policy is claimed.

## Verification

Current source checkpoint `c92e094`: **583 passed, 2 skipped** in 19.81 seconds.
The skipped checks are opt-in live tests; separate real API/model verification
is recorded in `OBSERVABILITY_RESULTS.md`. All three controlled learning-cycle
experiments passed independent manifest, snapshot, evidence ancestry, request
and outcome verification. The original learning-cycle checkpoint had 552 passing
tests and two skips.

Historical policy-guard checkpoint: **536 passed, 2 skipped in 6.68s**.
The subsequent prompt-only operation-polling correction passed 53 applicable
policy, provider-boundary and CLI tests. Earlier architecture/xmemory checkpoint:
524 passed, 2 skipped in 6.37s.
The subsequent focused xmemory/architecture/CLI verification passed 31/31,
including 256-character journal keys and reopened-SQLite finalization replay.
Ruff, changed-file formatting and `git diff --check` passed. The separate live
v2 adapter integration check passed **1/1 in 3.14s**. The real pinned xMemory
facade plus our adapter and SQLite also passed a smoke with an injected fake
memory system; full upstream generation/embedding remains untested.

Run from `vadim/`:

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  uv run --extra codex --locked pytest -q -ra --tb=short
UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --extra codex --locked ruff check .
git diff --check -- .
```

The two integration tests require explicit simulator URL variables. Their
normal offline skips are not live failures. Format only changed Python files;
17 unchanged files had formatting deviations at the preceding checkpoint.
