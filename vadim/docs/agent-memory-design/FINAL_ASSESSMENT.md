# Final implementation and evidence assessment — 2026-09-05

The implementation has a reusable agent loop, an external frozen environment
specification, independently selectable memory mechanisms, and separate
evaluation and administrative maintenance paths. Those facts establish a
testable architecture. They do not establish the North Star's independent-world
learning and transfer claim.

## Plan coverage

| Plan area | Implementation and evidence | Gate still open |
| --- | --- | --- |
| Stages 1–5: contracts, stores, orchestration, episodes, audit | Native in-memory/SQLite paths, bounded context, immutable snapshots, retained provenance and independent import checks | Broader deployment evidence |
| Stage 6: lessons | Candidate generation, separate validation, contradiction checks and activation rules | Live SRE learning needs authoritative immutable world identities |
| Stage 7: evaluation | Frozen manifests, every attempt retained, separate training/evaluation writes, telemetry and report verification | Locked and adequately powered effectiveness study |
| Stages 8–9: world hypotheses and consolidation | Evidence-backed hypotheses, revision/supersession, explicit consolidation; three retained real-model learning-cycle experiments | Independent causal-family transfer; automatic scientific discovery is not implemented |
| Stage 10: advanced retrieval | Lexical/structured, real semantic embeddings, confidence/recency/diversity, bounded evidence-neighborhood scoring and optional reasoned queries; 56-cell development comparison | No held-out relevance or SRE utility result; no default promotion |
| Stage 11: playbooks/tool knowledge | Separate evidence-backed modules and independently selectable compositions | Measured incremental decision utility |
| Stage 12: forgetting/compression | Retained-source summaries, duplicate/superseded handling, operational decay, explicit policy-mediated payload deletion and snapshot retirement | Long-lived deployment measurement; lifetime audit metadata is deliberately retained |
| Stage 13: final ablation | Earlier A0–A9 integration matrix retains all 42 attempts: 41 horizon completions, one interruption, zero SLO passes | The normative held-out effectiveness matrix remains unfulfilled |

This is not a claim that every planned function is fully integrated: Stage 10
graph neighborhoods stay inside the admitted pool, and semantic/reasoned
strategies require explicit composition rather than the ordinary `evaluate-v2`
factory. Stage 12's operational boundedness has fixture evidence, not a
long-lived deployment measurement. The remaining gates include integration
work as well as outcome evaluation.

The original Stage 0 balance baseline remains a separate historical protocol.
The public v2 simulator optimizes a completed 99% uptime SLO and then cost;
these outcome definitions cannot be silently mixed.

## What the experiments support

Four recent no-memory SRE diagnostics produced zero demonstrated successes.
The policy-1.1 run completed seven days with 28.67% uptime. Corrected-policy
Sol low/high runs stopped at our 40-minute wall limit after 156/132 decisions
and about one fifth of the horizon. Astra failed before its first action
because the installed provider client was too old. These results do not
measure memory utility. See [SRE diagnostics](SRE_COMPLETION_RESULTS.md) and
the ongoing [strategy diagnosis](SRE_STRATEGY_DIAGNOSIS.md).

Three controlled learning-cycle experiments each retained 24 attempts, trained
8/8 incidents, reopened SQLite, and read frozen evidence. Recovery without
memory was 4/8 in each experiment; with hypotheses it was 6/8, 8/8 and 8/8.
The mechanism closes the experience → hypothesis → later-decision loop on a
designed local family. These are separate development experiments, not 24
independent held-out worlds. See [learning-cycle results](LEARNING_CYCLE_RESULTS.md).

The Stage 10 relevance fixture contains 16 documents and eight queries.
Lexical Hit@1 was 2/8, semantic 6/8, hybrid 7/8. Provenance-neighborhood scoring
did not improve hybrid's first result; reasoned reformulation also gave 7/8
while adding about 54 seconds across eight requests. Keep the complex variants
optional. The graph ranks evidence already admitted to the candidate pool;
it does not discover causal links or bypass snapshot provenance. See
[retrieval results](STAGE10_RETRIEVAL_RESULTS.md).

The Stage 12 seeded SQLite diagnostic removed 194 eligible raw records out of
200, preserved all six protected records and a second snapshot, scrubbed
payload copies from receipts, and passed reopening and integrity checks. Live
bulk payload dropped from 981378 to 31264 bytes; the SQLite file stayed the
same size. This verifies row reclamation under retention policy, not secure
erase or constant lifetime archive size. See
[retention results](STAGE12_RETENTION_RESULTS.md).

## Architectural judgment

The principal responsibilities are visible in `runs`, `decisions`,
`environment`, `memory`, `evaluation`, `integrations`, and `composition`.
Canonical execution does not name simulator actions or concrete providers.
The environment supplies the frozen prompt and decision schema before model
construction. In the v2 adapter, the startup document is the actual sanitized
server `commands_markdown`; tools and HTTP dispatch are simulator-owned.
Adding an environment requires its adapter and composition, without adding
its business rules to memory or the runner.

Static checks deny new memory implementation dependencies outside explicit
boundaries, resolve relative and package imports, detect runtime import cycles,
restrict provider SDK imports to exact adapters, and check fresh-process lazy
imports. A tracked fixture pins 56 historical schema identities. This is
meaningful dependency enforcement, with explicitly documented legacy bridges;
it is not Python process isolation or a proof against reflection/dynamic imports.
See [architecture hardening](ARCHITECTURE_HARDENING_RESULTS.md).

## Oracle and evaluation separation

The decision provider receives public observations and admitted memory. The
Codex adapter uses an isolated workspace, disables available tool families,
and rejects forbidden tool events. Evaluator labels are absent from retained
decision/retrieval requests. Evaluation writes do not feed the frozen reader;
failed attempts stay in the denominator. No simulator internals or hidden world
state were used in these diagnostics.

Request inspection and provenance checks support that boundary claim; they
are not a formal noninterference proof. A content hash proves content identity,
not causal truth. The public simulator does not return the authoritative
immutable world hash and causal-family identifier needed by the learning
validator. Seed 42, a run ID and an API schema hash cannot substitute for them.
That gate has not been weakened to make the results look better.

## xMemory and the neighboring agent

The optional integration targets the research library HU-xiaobai/xMemory under
the documented assumption. Its facade was exercised with an injected fake
memory engine; the full upstream embedding/generation pipeline has not been
validated. The public facade lacks immutable snapshot export, so enabled
xMemory remains excluded from frozen evaluation. Native FastEmbed testing does
not supply missing xMemory evidence. See [integration limits](../XMEMORY_INTEGRATION.md).

There is no basis to say that this agent outperforms Alexey's. The adjacent
`simple_agent` was inspected, but its identification as Alexey's actual agent
was withdrawn, and there is no fair common-protocol comparison. More modules
and more tests do not imply better incident handling. See
[agent comparison](AGENT_COMPARISON.md).

## Remaining completion criteria

Obtain authoritative immutable world/family identities; lock an independent
causal-family split; establish a successful public-observation SRE baseline;
then measure each memory mechanism against that baseline with complete
attempt accounting and uncertainty. Run a separate full xMemory integration
only if it remains a useful candidate and its frozen-read boundary can be
satisfied. Promotion requires this evidence, not implementation completeness.

## Final local verification

The Stage 10/12 closeout suite passed **655 tests**, with two opt-in live checks
skipped. The subsequent generic decision-continuity and terminal-result
corrections passed **661 tests**, again with two opt-in live skips.
Ruff passed; all 56 historical schema identities matched the tracked fixture.
The final Stage 12 quick review and independent targeted review were clean.
Real retrieval, learning-cycle and retention evidence is reported separately
from offline tests. All implementation changes remain under `vadim/`.
