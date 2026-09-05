# Remaining implementation and evidence work

Started 2026-09-05 from `0fb3e03182f8ec90f736bb329f2c62d7cdd7ee5d`.
The owner requested correcting v2 time planning and continuing through the end
of the implementation plan, including further live simulator tests as needed.
Root owns architecture, integration, review and verification; bounded
implementation work is delegated. All changes remain below `vadim/`.

## Execution order and ownership

1. Correct the v2 decision policy's use of simulation horizon and decision
   budget. Preserve typed raw action semantics and observable-only information.
   Validate a full live run; record every attempt, including startup failures.
2. Stage 7: introduce a separately versioned uptime/cost experiment profile,
   preregistered ordered paired matrix, isolated training namespaces, immutable
   frozen memory, attempt lifecycle, telemetry and reports. Reuse the existing
   stores, audit and orchestration boundaries. Preserve the legacy Stage 0
   balance profile. Evaluation owns experiment declarations and phase isolation.
3. Measure episodic versus lessons and investigate the Stage 6 evidence gate
   before adding further cognitive complexity. A zero or negative effect is a
   result to explain, not permission to claim improvement.
4. Stage 8: experimental world hypotheses with explicit scope, uncertainty,
   support/counter-evidence, revision and bounded untrusted retrieval. Shared
   validation remains distinct from candidate generation.
5. Stage 9: explicit out-of-band consolidation, immutable dry-run deltas and
   idempotent application. Never implicitly run it from the decision loop.
6. Stage 10: replaceable, deterministic retrieval strategies and declared
   comparisons under the existing global context budget.
7. Stage 11: separate optional playbook and tool-knowledge modules, grounded in
   retained evidence rather than embedded simulator rules.
8. Stage 12: bounded operational memory, duplicate/stale/superseded handling and
   policy-mediated compaction; protect retained evidence and active holds.
9. Stage 13: run and report the available paired A0–A9 and targeted ablations,
   with frozen input provenance, all attempts and explicit evidence coverage.
   Keep unsupported modules optional; do not equate implementation completion
   with a positive result or default promotion. The completed matrix and the
   subsequent telemetry smoke and short-wall cancellation probe are recorded in
   `V2_LIVE_INTEGRATION_RESULTS.md`.

## Evidence constraints

- Seed 42 is a development seed. It is not an unseen holdout.
- The live simulator's public start response does not supply an immutable world
  content hash or causal-family identity. An API-contract hash or renamed seed
  is not a substitute for that evidence. Unknown identity remains explicit.
- The normative candidate validator requires two distinct completed eligible
  learning runs across two immutable contexts. Frozen evaluation and retries
  never supply eligible support.
- Final generalisation/default-promotion claims require the specified locked
  causal-family holdout, a complete preregistered profile and appropriate
  approval. Additional synthetic contract checks cannot supply live learning
  effectiveness evidence.
- The original 90-day and project-lifetime retention guarantees remain in force.
  Dry-run planning is separate from destructive deletion authority.

## Progress

- Remaining normative stages and existing extension points inventoried.
- V2 policy implementation: a versioned decision-model wrapper raises undersized
  error-stopping waits to `ceil(remaining_seconds / max(1, decisions_left // 2))`,
  with the API minimum of 300 seconds. Policy 1.1 preserves pending-operation
  durations and permits explicit no-stop waits only when current public evidence
  proves the full-horizon SLO unrecoverable. Other no-stop proposals regain the
  default first-error stop and ordinary duration planning. Prompts and effective
  decisions record arithmetic, eligibility and adjustments; the HTTP adapter
  remains exact. The operation-polling prompt correction and new diagnostic
  outcomes are recorded in `V2_POLICY_GUARD_RESULTS.md`.
- Stage 7 contracts/reporting, CLI execution and the standalone retrieval
  strategy are implemented. The exploratory v2 protocol is recorded in
  `SIMULATOR_V2_EVALUATION_PROFILE.md`.
- Runtime design: record/start the physical environment before composing its
  immutable run declaration, then pass a one-shot prestarted environment facade
  to the unchanged runner. Frozen evaluation reads snapshot-bound memory;
  transition/finalization writes and audit go to isolated output namespaces.
  The frozen read path must never consult that output overlay.
- Provider accounting must retain measured token/time metadata, including
  validation retries, with missing monetary/token values explicitly unavailable.
  An explicit portable reasoning-effort setting removes the diagnostic wrapper's
  dependence on local Codex defaults for subsequent reproducible experiments.
- Telemetry instrumentation is applied in commit `fdd7865`, and the SDK shutdown
  fix is applied in `e35fd58`. The latter sequences production-owned turns as
  shielding, public client close, draining the turn task, and cancellation
  propagation; borrowed
  turns are unchanged. Local verification is **504 passed, 2 skipped in 6.12s**
  with Ruff clean. The historical matrix was terminated with SIGTERM (exit 143) after its report
  was printed and independently verified;
  its shutdown regression was red with the old five-second timeout and green
  through the real router.
- Real experimental compositions exist through A9. Their evidence gates remain
  open; integration coverage does not establish causal utility.
- Stage 6 investigation is recorded in `STAGE6_V2_DIAGNOSIS.md`: unavailable
  immutable world identity, no adjacent objective-metric pairs in development
  pilots 3–5, and limits of exact parameterized action matching. These findings
  permit informed implementation of optional experiments; they do not close the
  held-out effectiveness gate.
- Development pilot 6, run `oHYrv6cMFNipb78wrJXDRm3Y`, executed the frozen
  `simulator-v2-time-budget@1.0` source with Codex `gpt-5.4-mini`, effort `low`,
  seed 42 and 40 decisions. It advanced 32640.388456408 seconds, then exhausted
  the decision budget: status `running`, uptime `0.9975761867223897`, SLO `null`,
  total cost `257006093` minor RUB. The wait floor worked; first-error stops and
  repeated investigation without resource changes prevented horizon completion.
  This is an unsuccessful, incomplete attempt, not an SLO pass.
- Pilot 7 uses the same frozen source capsule, seed 42, Codex `gpt-5.4`, effort
  `medium` and 160 decisions. It is a diagnostic model/budget comparison, not a
  controlled one-factor ablation or an unseen-seed evaluation.

- Pilot 7 completed the seven-day horizon in 159 decisions, run
  `HnO73c9kpqjlz1VK91K9OzKP`: SLO false, uptime `0.2657543556929663`,
  cost `8404012903` minor RUB, wall time `1944.3039373749634` seconds.
  Authenticated server creation and asynchronous completion worked; diagnosis
  remained ineffective and the final wait skipped the remaining horizon.
- Pilot 8, run `LPmofpKzcX0p17ZsLw2US9Yd`, verified portable effort and
  corrected log-page visibility with mini/low/40 decisions. Status remained
  running, observed `32592.52497722` seconds, uptime `0.99769220832456`,
  SLO null and cost `257006084` minor RUB. Filtered logs showed capacity
  errors, but the model made no corrective change.
- Retrieval, maintenance, knowledge and composition have focused verification.
  The evaluation read/write split has a real activated-lesson test
  over two immutable contexts, including frozen nested source snapshots.

- Pilot 9 made a diagnostic comparison against attempt 8 with the same
  frozen source, seed 42, low effort, no memory and 40-decision budget; only
  the decision model changes to catalog-verified `gpt-5.6-sol`. This remains
  development evidence, not a held-out memory comparison.

- Pilot 9 completed in 27 decisions and `268.85892212501494` wall seconds,
  run `XQqybzunrxvpO8ul2VvQYZ7w`. Uptime `0.2659386936335913`,
  SLO false, cost `8455412903` minor RUB. It corrected capacity at decision
  11, but ultimately skipped the remaining horizon. Model improvement alone
  did not produce a successful SRE policy.

## Integration review decisions

- Frozen input reads and evaluation writes use separate real memory runtimes.
  A mutable overlay is not an acceptable frozen reader. Historical nested
  snapshots are admitted only when their complete member hashes belong to the
  frozen input. The writer owns audit and finalization.
- Before freezing, validate training run ownership and transitive assembler
  provenance for every admitted record. A list of training attempt IDs alone
  does not prove snapshot ancestry.
- Real v2 sessions need experiment-owned context attribution at the evaluation
  boundary; otherwise the runner records null environment/scenario IDs even
  when the profile supplies verified content identities.
- Declared source, dependency, prompt, policy, settings, estimator and endpoint
  identities must match the actual CLI execution. A selected reference source
  directory cannot attest a different installed package.
- Declared wall/context budgets must constrain execution. Unknown telemetry
  stays unknown; partial usage cannot masquerade as complete totals.
- Retrieval strategies receive verified token estimates before applying local
  limits, preserve admitted envelopes, and remain separate from module write
  and finalization capabilities.
- The structured-retrieval ablation changes only structured features, keeping
  the other retrieval controls fixed. Consolidation supersession is effective
  independently of the forgetting/age-decay flag.
- Derived validators exclude frozen-evaluation runs from both support and
  counter-evidence. Failed/retried learning runs remain searchable counters,
  and type-sensitive comparisons distinguish JSON booleans from numbers.
- New lesson validation manifests require explicit acceptance authority,
  checks, timestamps, decision references and retention policy. Legacy batches
  missing required acceptance metadata fail closed and require explicit
  revalidation from retained evidence; no approval is inferred.

- Real-trace budget check: 26 reconstructed pilot 9 episode views use 2109–3081
  units of the declared UTF-8-byte upper-bound estimator (median 2181.5). None
  fits the original experimental module cap of 1000. Experimental presets now
  use a 4000 module cap and 16000 global cap; generic legacy defaults remain
  unchanged. A regression requires the default A2 profile to retrieve a real-
  shaped episode rather than passing only an empty-store smoke check.

## Delivered scope and still-open research work

- Stage 7: versioned v2 profile, durable lifecycle, exact source/provider pins,
  train/frozen-evaluation isolation, declared contrasts and honest accounting.
  Context totals, snapshot sizes and provider usage are measured, with detailed
  module evidence retained in structured audit. The historical matrix identified
  by source `2e0b411` is the old null-counter baseline: attempt-level
  stored-artifact totals and aggregate module-lifecycle counters remain
  unavailable there. The telemetry instrumentation patch is applied in commit
  `fdd7865`; it adds typed construction/read/validated-nonempty-contribution/
  write/finalization/consolidation hooks and forwards their rows through the
  runtime/evaluation facade. Its stored-record counts distinguish cumulative
  training namespaces from isolated evaluation attempt namespaces; frozen input
  remains separate as `snapshot_members`, and legacy `remember()` remains
  outside structured module counters. `finalization_events` exists on
  per-module runtime telemetry; the retained attempt `MemoryTelemetry` schema
  has no aggregate finalization field. `module_contribution_events` counts
  validated nonempty contributions entering the global merge, not selected
  unique-item counts. The four-cell telemetry smoke against source
  `e35fd581b57318ff062fc01ea1d62c1e92268978` completed 4/4 cells with 0/4
  passing SLO and CLI exit 0; verification covered
  12 lifecycle events, 11 durable artifacts and 2 bindings. Training A0 had
  zero module events with `stored_artifacts=5`; training A3 had construction `2`,
  reads `2`, writes `1` and `stored_artifacts=8`, with its remaining reported aggregate
  counters at zero. The four-cell short-wall cancellation probe (A0/A3,
  training/evaluation seeds 43/44, eight decisions, 12-second wall budget)
  interrupted all 4/4 cells; each retained `provider_telemetry.request_count=1`.
  After all four cells, the CLI exited with code 0 and the evidence passed
  verification. Probe details are
  recorded in `V2_LIVE_INTEGRATION_RESULTS.md`; these checks do not close the
  held-out effectiveness or default-promotion gates.
- Stage 8: scoped world hypotheses, independent validation, version/history and
  removal of disputed knowledge from the decision view.
- Stage 9: explicit snapshot replay/contrast selection, semantic candidate merge,
  independent validation, immutable plans and idempotent application. Maintenance
  adds retained provenance links and summary/supersession deltas. This is a
  deterministic first implementation; it does not perform LLM dreaming.
- Stage 10: replaceable lexical/structured ranking, diversity/deduplication and
  operational decay. Semantic embeddings, graph expansion and learned query
  formulation are unimplemented alternatives, not silently enabled features.
- Stage 11: separate evidence-backed playbooks and tool knowledge.
- Stage 12: retained-source summaries/links, duplicates, supersession, age decay,
  holds and deletion eligibility policy. Age decay currently applies to source
  episode IDs; derived knowledge is governed by validation/supersession. Physical
  deletion is unimplemented;
  retained storage can continue growing while the decision view is bounded.
- Stage 13: A0–A9 plus four supported targeted configurations are executable.
  The completed matrix is recorded in `V2_LIVE_INTEGRATION_RESULTS.md`: 42/42
  cells have terminal records, with 41 completed and 1 interrupted; 0/42 passed
  SLO. Training completed 27/28 attempts and evaluation completed 14/14. All
  14 bindings were valid, and independent verification passed for 126 lifecycle
  events and 98 durable artifacts. This verifies bounded execution and
  reporting only; it cannot satisfy the final held-out
  effectiveness/default-promotion gate because world content/family identity
  and eligible learning evidence remain unavailable. The telemetry smoke and
  short-wall cancellation probe are complete and recorded separately from this
  historical matrix in `V2_LIVE_INTEGRATION_RESULTS.md`.

See `EXPERIMENTAL_MEMORY_GUIDE.md` for executable commands and operational
limitations. The original Stage 0 balance protocol and normative evidence gates
remain intact. No new module has been promoted to default.
