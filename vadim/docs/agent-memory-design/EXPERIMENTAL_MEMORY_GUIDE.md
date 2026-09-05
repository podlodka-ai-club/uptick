# Experimental memory and v2 evaluation

All newly implemented memory mechanisms remain experimental. A completed
implementation or passing integration test does not demonstrate better SRE
behavior, generalisation, or eligibility for default promotion.

## Composition

`AgentRunner` still consumes one `AgentMemory` port. The experiment composition
root in `experimental_runtime.py` builds the real enabled modules, with no
simulator rules or provider SDK objects inside memory modules. The A0–A9
catalog and targeted conditions live in `evaluation_presets.py`.

| Condition | Added behavior |
| --- | --- |
| A0 | No decision-visible persistent memory |
| A1 | Legacy lexical memory |
| A2 | Structured episodes, without the legacy adapter |
| A3 | Independently validated lessons |
| A4 | Scoped observational world hypotheses |
| A5 | Explicit consolidation after training and before evaluation freeze |
| A6 | Configurable lexical/structured ranking, diversity and deduplication |
| A7 | Reusable observed action sequences with explicit result guards |
| A8 | Separate tool input/response knowledge |
| A9 | Age decay for source episodes, with evidence retained |

The catalog also declares A6 minus world model, consolidation and structured
retrieval, and A8 minus tool knowledge. The minus-contradiction condition is
explicitly unsupported: contradiction validation is mandatory acceptance
infrastructure. The structured ablation removes structured features while
retaining the same advanced lexical, diversity and deduplication controls.
Semantic retrieval is unsupported and rejected explicitly when requested.

Every implementation setting is part of the resolved configuration or the
pinned implementation version/source. Experimental presets allocate 4000
estimated context units per module and 16000 globally. The estimator is a
conservative UTF-8 byte upper bound, not provider-reported token usage. The
smaller earlier module cap of 1000 rejected all 26 real episode views checked
from development pilot 9. Generic legacy defaults are unchanged.

## Evidence and visibility

Generators and validators are separate. Active derived knowledge requires two
completed eligible first-attempt learning runs in two immutable content
contexts, complete retained provenance, complete deterministic counter search,
and no unresolved contradictions. Failed and retried learning runs can supply
counter-evidence; frozen evaluation supplies neither support nor counters.

World hypotheses describe scoped observations. Playbooks describe repeated
sequences with an explicit result guard. Tool knowledge describes scoped
input/response behavior. Their descriptive support fractions are not calibrated
probabilities or proof of causal utility. All retrieved items remain
`derived_untrusted`, and the decision model chooses actions and parameters.

Lesson validation manifests now use schema 1.1 with required authority, checks,
decision reference/timestamp, omitted-counter count and retention reference.
Old persisted batches missing these acceptance fields fail closed. Preserve
raw records and regenerate knowledge into a fresh derived namespace through
explicit revalidation; missing acceptance metadata is never inferred.

## Evaluation command

From `vadim/`, first build an offline integration declaration (the command
refuses to overwrite an existing manifest):

```bash
uv run --extra codex --locked python scripts/build_v2_integration_manifest.py \
  --source-root "$PWD" --output artifacts/matrix-manifest.json
```

Defaults: 14 supported conditions, training seeds 51/52, evaluation seed 53,
one replicate, 8 decisions and 120 wall seconds per cell. `--smoke` selects A0/A3
with seeds 43/44 and two decisions. Inspect the sealed configuration before
execution; these defaults define an integration exercise, not a powered utility
study. To execute a preregistered profile or sealed manifest:

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  uv run --extra codex --locked uptick-agent evaluate-v2 \
  --source-root "$PWD" \
  --profile path/to/manifest.json \
  --simulator-url http://81.176.229.58:8080 \
  --artifacts artifacts/new-experiment
```

The source root must contain the executing package under `src/`,
`pyproject.toml` and `uv.lock`. Execution verifies the actual source revision,
scoped dirty state, source and lock hashes, pyproject/runtime fingerprint,
prompt, policy, resolved generation settings, context estimator and declared
endpoint fingerprint before constructing clients. Referenced immutable world
content identities must come from the experiment owner; API schema hashes and
seed labels do not establish them.

The profile fixes train/evaluation seeds, replicate indices, full configurations,
directed comparisons, budgets and failure policy. A0 receives the same bounded
current-run state as other conditions. Store and audit namespaces bind to the
sealed manifest hash. Reusing an existing lifecycle or training namespace is
rejected; resuming a partially executed evaluation is not implemented.

Training is followed by explicit consolidation for enabled conditions, then
immutable snapshots and a persisted binding before the first evaluation
request. The freeze validates training ownership and source-leaf hashes.
Evaluation reads only admitted snapshot members, including verified nested
historical snapshots; its writes and audit use isolated output namespaces.
Evaluation finalization never modifies or learns into the frozen reader.

The artifact directory retains the manifest, lifecycle journal, SQLite memory,
bindings, result/trace artifacts and final report. Failed, cancelled and
incomplete attempts remain visible. User cancellation stops further cells;
per-attempt wall-budget expiry records an interruption and permits subsequent
cells. Partial traces survive execution errors when persistence is available.

Reports use first attempts. A successful diagnostic retry cannot replace a
failed first attempt. Completion and SLO rates use the declared cell count;
cost contrasts include only pairs where both first attempts completed and
passed SLO. Directed comparisons can be preregistered explicitly. Unknown
usage remains unknown, partial token fields are not complete totals, and mixed
currencies are not averaged. Provider request counts cover adapter-visible
calls; internal SDK transport retries are not observable.

Per-attempt context totals and frozen snapshot member counts are measured. The
historical matrix identified by source `2e0b411` is the old null-counter
baseline: its attempt telemetry still has null stored-artifact totals and
aggregate module-lifecycle counters. These are unavailable measurements, not
zero activity, and the old artifact is not rewritten. The telemetry
instrumentation patch is applied in commit `fdd7865`. It adds typed per-module
counters for construction, reads, validated nonempty contributions entering the
global merge, writes, finalization and consolidation, forwards them through the
runtime/evaluation facade, and measures stored-record counts. Training counts
are cumulative across the condition's training namespace set; evaluation counts
cover only the current attempt's isolated output namespaces. Frozen input is
reported separately as `snapshot_members`. The legacy `remember()` compatibility
path bypasses the structured orchestrator and is outside these module counters.
`finalization_events` exists on per-module runtime telemetry; the retained
attempt `MemoryTelemetry` schema has no aggregate finalization field.
`module_contribution_events` counts validated nonempty contributions entering the
global merge, not selected unique-item counts. Missing or partial values remain
unavailable (`null`).

The SDK shutdown fix is applied in commit `e35fd58`: production-owned turns use
shielding, public client close, draining the turn task, and cancellation
propagation in that order; borrowed-turn behavior is unchanged. The historical
matrix process was terminated with SIGTERM (exit 143) after printing and
independently verifying its report. The shutdown regression was red with the old
five-second timeout and green through the real router. Live verification against
source `e35fd581b57318ff062fc01ea1d62c1e92268978` is complete. The four-cell
telemetry smoke completed 4/4 cells with 0/4 passing SLO and CLI exit 0;
verification covered 12 lifecycle events, 11 durable artifacts and 2 bindings.
Training A0 measured zero module events with `stored_artifacts=5`; training A3
measured construction `2`, reads `2`, writes `1` and `stored_artifacts=8`, with
the remaining A3 reported aggregate counters at zero. The four-cell short-wall
cancellation probe used A0/A3, training/evaluation seeds 43/44, eight decisions
and a 12-second wall budget; all 4/4 cells were interrupted and each retained
`provider_telemetry.request_count=1`. After all four cells, the CLI exited with
code 0 and the evidence passed verification. Probe details are recorded in
`V2_LIVE_INTEGRATION_RESULTS.md`; these checks provide no gate-closing evidence.

After completion, independently verify the persisted evidence:

```bash
uv run --extra codex --locked python scripts/verify_v2_experiment.py \
  artifacts/new-experiment --per-attempt
```

This recomputes the report, validates lifecycle hashes/transitions/final states,
checks durable result/trace/binding references, and verifies frozen snapshot
members against SQLite. Null historical telemetry is preserved; a reported
snapshot count that disagrees with the actual binding is rejected.

## Explicit maintenance

Maintenance is independent of the decision loop and finalizers. The standalone
archive-maintenance command defaults to a persisted dry run:

```bash
uv run --locked python -m uptick_agent.memory_maintenance_cli \
  --sqlite-path path/to/memory.sqlite3 --namespace source-namespace \
  --snapshot-id frozen-snapshot --request-id maintenance-1
```

To apply the saved plan, repeat the same arguments with
`--apply --plan-id <the-returned-plan-hash>`. The source snapshot and members
must still match. Holds protect referenced records. Raw records and snapshots
receive at least a 90-day floor from the post-training plan; audit and derived
knowledge records follow project-lifetime retention.

Physical deletion is not implemented. Supersession and age decay affect the
operational episode retrieval view while retaining source records. Derived items
without a source-record identity are not age-decayed; their validity is controlled
by revalidation and supersession. Extractive summaries
remain candidate artifacts. Knowledge consolidation separately replays retained
evidence, records contrast pairs and merged candidate dispositions, and exposes
only independently validated knowledge from applied plans. No human approval
or module-level default promotion is manufactured by these commands.

## Limits of the live evidence

The public v2 API used in the development pilots does not provide immutable
world content hashes or causal-family identities. Profiles must report that
identity as unknown; derived knowledge cannot qualify for activation from
those runs. Seed 42 was used for debugging and is not an unseen holdout.

The available live integration matrix checks transport, lifecycle, snapshots,
module composition and reporting under a declared bounded budget. Synthetic
activation and contradiction tests check contracts. Neither closes the final
held-out learning-utility or default-promotion gates. A separate locked causal-
family holdout, sufficient complete runs, and the required approval evidence
are still necessary for those claims.
