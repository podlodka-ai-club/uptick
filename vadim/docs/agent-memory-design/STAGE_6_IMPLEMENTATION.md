# Stage 6 implementation record

Status: experimental implementation verified; held-out evaluation gate remains open.

## Architecture decisions

The lessons module generalizes repeated, measured action outcomes and contributes
only automatically validated lessons to subsequent decisions. The first extractor
is deterministic: exact action, configured top-level observation conditions, one
configured metric/unit and its maximize/minimize direction. It describes observed
association, not causal attribution. Negative utility uses the same lesson schema.
No LLM, embedding, scheduler, or new dependency is needed for this stage.

Candidate generation and validation are separate capabilities. Validation uses
`simulator-candidate-validation-v1@1.0`; the fixed query contract is
`exact-observation-action-metric-v1@1.0`. Support requires completed, explicitly
eligible learning runs, first attempts only, distinct logical run IDs and distinct
immutable environment/scenario context hashes. Frozen evaluation supplies neither
support nor counter-evidence. Counter searches include matching learning evidence
from retries and failed/interrupted runs; absent run metadata prevents activation.
Zero or opposite signed utility is conservatively unresolved counter-evidence.
Every declared learning finalization revalidates the catalog, including failed,
interrupted, excluded and retried runs. Such runs cannot add eligible support,
but their counter-evidence must invalidate a prior lesson before the next run.
Frozen evaluation and runs without a declaration do not trigger learning writes.

The input is one persisted immutable bundle: a verified episodic store snapshot,
all of its verified member records, and immutable run declarations. The source
adapter owns episodic record decoding; lessons never import EpisodicMemory.
Declarations come from the experiment caller, never from model/environment text.
They bind physical and logical run IDs, attempt/phase/eligibility and immutable
environment/scenario IDs plus content hashes. Stage 7 must bind these declarations
to its preregistered manifest; Stage 6 does not infer eligibility from success.
The first attempt has index zero, matching Stage 0. Renaming identical context
content cannot provide additional independent support. A small persisted capture
intent freezes declaration membership before creating the source snapshot, so
partial-write retries preserve the same metadata and snapshot.

Provenance closure in v1 supports the assembler's observation/result leaf IDs and
hashes, recomputed from the embedded sanitized source payloads. Unknown or missing
references fail closed. Validation manifests bind candidate, input, every search
result and evidence hash, eligible support run/context IDs, grounding, polarity,
closure, contradiction count and disposition. All derived content remains untrusted.

One append-only batch per end-of-run operation stores the input, lessons and their
validation manifests atomically. Replays read the authoritative batch, not cached
write receipts. Full deterministic regeneration merges identical semantic lessons
and revalidates old lessons against new counter-evidence. Retrieval uses the newest
complete snapshot batch, validates it before exposing active items, and excludes
the contributing physical/logical run from seeing its own finalized lessons.
This deliberately favors a small auditable implementation over incremental indexes.
Confidence is a deterministic support/contradiction score, not a calibrated
probability. Utility is the mean signed delta of matching learning transitions,
including ineligible attempts found by the counter search. Neither is a causal
estimate. Reads currently regenerate and verify the retained batches; runtime
cost grows with retained history and must be measured by Stage 7.

## Public boundary and verification

`LessonSettings` declares the metric, direction and top-level observation keys.
`MemoryConfiguration.episodic_with_lessons(lesson_settings=...)` resolves the
experimental profile. `lessons_memory_runtime(...)` accepts the structured store,
separate episodic/lesson namespaces, explicit run declarations and configuration.
Disabling lessons constructs no source or lesson module and leaves episodic
operation available. Run identities and immutable context declarations must be
admitted by the experiment caller before their finalization; no model text can
declare a run eligible.

Contracts are in `memory/lesson_contracts.py`; the separate pure functions
`extract_candidates` and `validate_candidate` are in `memory/candidate_validation.py`.
`StoredEpisodicLessonSource` freezes the input and `LessonsMemory` persists and
retrieves it. The composition root alone imports concrete modules. All persistence
uses `StructuredMemoryStore`; no new dependency or provider was introduced.

Verification covers two-run activation, anti-lessons, exact JSON type distinctions,
context-content independence, zero/negative contradictions, complete provenance,
snapshot and declaration replay, corrupted receipts/manifests, redacted outcome
replay, disabled construction and architecture boundaries. A real `AgentRunner`
with a test environment and reopened SQLite verifies this sequence: two learning
runs activate a lesson, negative evaluation evidence is ignored, a negative failed
learning run disputes it, and the next run receives no active lesson.

The final offline suite has **333 passed, 1 live test skipped**; Ruff and diff
checks pass. Independent Terra High review is clean after fixing incorrect
environment hashes in the validation manifest. The live test was also run
separately and failed at the existing v1 endpoint, as recorded below.

## Stage boundary

The baseline and episodic-only profiles retain their behavior. Lessons are an
explicit experimental profile with extraction settings in its configuration hash.
Synthetic tests can verify automatic activation rules; they cannot establish
improvement over episodic-only memory. Stage 0 live evidence and Stage 7 paired
held-out experiments remain required before claiming that improvement or proposing
a default module promotion.

## Live simulator compatibility probe (2026-09-05)

The owner supplied `http://81.176.229.58:8080` and authorized trial runs.
`POST /v2/start` with seed 42 succeeded. A direct API smoke test retrieved the
18-command catalog, executed `server.types.list` and `site.config.get` (HTTP 200),
and advanced 300 simulated seconds. The observed interval had zero downtime;
the run was still running, so this is not a final SLO result. The local sanitized
evidence is `artifacts/live-v2-smoke-2026-09-05.json` (intentionally not committed).

The existing adapter's integration test failed with HTTP 404 at `/v1/start`.
API v2 changes the objective from the old economy model to uptime/cost and
introduces control-panel/server authentication and 18 infrastructure commands.
Changing a URL prefix is insufficient. A versioned simulator-adapter update is
required before live LLM runs or Stage 7 evaluation on this deployment. The probe
used direct API calls, not the current AgentRunner/LLM loop, and supplies no
lesson-activation or held-out improvement evidence.
