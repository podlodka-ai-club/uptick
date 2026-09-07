# Online observed-memory integration

Implemented as explicit experimental profiles. Legacy defaults and strict
causal/independent-world promotion rules are unchanged. Existing live series
use their original frozen source; these changes do not hot-patch them.

## Executable path

`cli._memory_factory` accepts `--memory online-world` and `--memory online-lessons`
with `--memory-database PATH` for a single v2 `run`. The persistent database is
an explicit cross-run memory source. Frozen `evaluate-v2` and benchmark paths
are unchanged; the online options reject unsupported commands before start.

`compose_sre_online_memory` constructs an episodic runtime, descriptive reader,
and `OnlineLearningMemory`. Each runner transition first passes the normal
persistence path. Every eight stored transitions, and at finalization, the
bridge freezes authoritative evidence, projects completed public operation
outcomes and persists verified descriptive summaries. Checkpoints preserve
idempotency and previously admitted run cutoffs across reopening. Changed
settings/projector/writer identities are rejected on checkpoint reuse.

The SRE projection joins operation initiation, explicit observed completion,
and the first subsequent hourly-cost sample, with a prior cost sample and a
32-iteration maximum interval. Missing samples or completion produce no fact.
Resource descriptors must come from public action parameters or prior successful
resource observations. Projection IDs are stable per run/operation/metric;
replaying a larger prefix cannot create additional support for the same outcome.

Online recall is constructor opt-in. Same-run facts require an aware host-owned
`decision_cutoff` and current iteration; all selected same-run source transitions
must be strictly earlier in both dimensions. Physical-run exclusion remains.
Older validated nested batches can supply eligible knowledge when a newer batch
contains future evidence. Strict active-world-hypothesis retrieval is untouched.
The SRE adapter supplies the cutoff, scopes retrieval to resource types in the
current public response and limits derived items to two, within three total
items and an 8000-byte serialized context cap. It also applies the project's
`utf8-byte-upper-bound` token estimator separately to request token caps.

## Verified integration

- Actual AgentRunner, public fixture responses, six executed actions: world
  summary forms during the run and reaches the sixth model decision context.
- Both world and lesson modes: before/after boundary, SQLite reopen, duplicate
  transition replay, correct support count, and actual AuditTraceWrite validation.
- Generic bridge: base-first writes, periodic/final flush, snapshots, cumulative
  trusted cutoffs, idempotency and configuration reuse checks.
- Temporal reader: opt-in/default exclusion, missing/equal/future boundaries,
  physical run exclusion and older-batch fallback.
- Real historical public-prefix replay: 120 transitions, two world summaries,
  seven recall events; no simulator or model calls. This is a mechanism test,
  not an efficacy comparison. Artifacts: `artifacts/sre-online-integration-01`.

## Other integration findings

1. **Fixed:** ordinary CLI previously exposed only legacy memory; explicit online
   profiles now reach the normal runner.
2. **Fixed:** config accepted module dependencies that actual composition rejects.
   Composition now checks episodic storage for derived modules and lessons for
   playbooks before constructing participants. The generic configuration remains
   usable with alternate injected modules; its contract is not narrowed.
3. **Fixed:** compact derived items previously retained stale/zero token estimates.
   They are now recomputed with the existing estimator; byte and token caps are
   separately enforced.
4. **Preserved intentionally:** observed lessons are a descriptive opt-in view,
   not a registered strict LessonsMemory promotion path. This online composition
   constructs that view explicitly. It does not claim the whole original tower
   is enabled.
5. **Still outside this change:** automatic playbook construction, maintenance
   scheduling, and promotion across verified independent worlds. No available
   public identity has been invented to unlock their gates.

## Limits

The SRE projector currently learns hourly-cost associations for server create
and delete. It does not yet learn DDoS remedies, capacity/SLO effects, or general
natural-language theories. Confounded intervals remain marked and are not used
by the clean-interval recall profile. Changing the extraction dimensions is a
separate experiment. Online learning introduces work on the decision path;
mechanical replay timing is not a production latency guarantee.

## Unified full profile

The later `online-full` mode now composes the existing A9 participants together
with both observed views. Both online writers consume shared projected evidence;
context selection reserves up to two balanced descriptive slots. Module
finalizers run through the normal lifecycle. Consolidation now receives a real
source snapshot, persists its dry-run plan and invokes apply with a separate
idempotency key. Forgetting is the configured maintenance read strategy.

Strict promotion eligibility remains unchanged: connected strict modules may
produce no accepted knowledge on unverified hosted-SRE identity. This supersedes
the earlier note that maintenance scheduling is entirely outside the change;
maintenance is now invoked at full-profile finalization. Periodic maintenance
between decisions is still not enabled.

Historical bootstrap uses `OnlineLearningMemory.learn_persisted_run` and cumulative
trusted cutoffs without synthesizing terminal outcomes. Verification and the
new live attempt are recorded in `artifacts/sre-full-live-01/README.md`.


## Bounded scope retention and long operations — 2026-09-07

The current development composition reconstructs type applicability from verified
same-run persisted public responses when an aggregate capacity/cost/operation
response has no types. It admits at most 32 types, uses only strictly prior
iterations and host timestamps, expires scope after 128 decisions, and records
the source transition hash and snapshot. A newer explicit inventory/catalog,
including an empty list, replaces older scope. Failed responses and unrelated
tools do not trigger carried tariff recall. This is type applicability, not a
cached current server count or capacity estimate. Reconstruction survives reopen;
it adds a verified source read on applicable context requests.

Operation tracking is separately bounded to 512 decisions in this SRE profile.
Before/after cost samples must each be within 32 decisions of initiation/explicit
completion respectively. Full interval provenance and mutation confounders remain;
no elapsed-time completion or causal promotion is inferred. Generic chain callers
retain legacy bounds unless they explicitly opt into the new options.

The projection/checkpoint namespace is now `:derived:<mode>:v2`, preventing reuse
of old extraction progress. Raw episodes and old learned views remain intact;
bootstrap desired historical runs through `learn_persisted_run` in an idle new
runtime to rebuild cumulative evidence under the new policy. No live source
capsule or live database was migrated. See `artifacts/memory-integration-fixes-01`.


## Observed recall validation reuse

World readers retain up to 16 previously validated observed batches per instance,
keyed by record identity, content hash and settings hash. Only deterministic
candidate/summary validation is reused. Each request rereads and validates batch
records, snapshots and authoritative evidence; source namespaces are read once
within that request. Missing/changed sources still fail closed. Ranking, physical
run exclusion and same-run cutoff checks are performed anew. Returned context
mutation cannot change retained validation state. Restarting a reader starts cold.

Stored record integrity still revalidates all fields and recomputes the same
canonical write hash, without constructing two redundant equivalent models.
Maintenance ranking returns immediately for empty candidate lists, avoiding a
full source scan when gated modules contribute nothing. Nonempty maintenance
ranking and its validation are unchanged. Timing/equivalence evidence lives in
`artifacts/memory-recall-speed-01`; source capsules of running simulations remain
unchanged.


## Public capacity observations

New development `online-full` composition enables a second observed-learning
channel, independently disabled with `compose_sre_online_memory(...,
learn_capacity=False)`. It reuses the generic bridge and world-pattern writer in
separate `:derived:capacity:v1` / `:capacity:observed` namespaces. Nested bridges
delegate to base persistence once; bootstrap explicitly visits both channels,
and finalization flushes both. Diagnostics expose each channel separately.

The SRE adapter projects successful public log-query rows whose explicit
SERVER_CAPACITY_EXCEEDED / status500 event reports matching required/load_units
and required > available. One event keyed by run/request/timestamp/error counts
once even when read on overlapping pages. Host observation time and iteration
bound learning; the simulator log timestamp is only part of event identity.
Missing, inconsistent or malformed fields yield no projection. Source record
hashes remain attached; source observations are not rewritten.

Recall is triggered by an explicit capacity-error log or a public backend
resource/metrics view. A capacity observation may use the world slot instead of
a duplicate tariff fact, within the existing item/byte budget. It says that
reported demand exceeded reported availability; it supplies neither total
installed capacity, a general utilization threshold nor a server-count policy.
This is an adapter-defined descriptive pattern family, not autonomous discovery
of arbitrary world laws. Failed-event-only sampling cannot establish sufficiency,
a causal remedy, or the absence of counterexamples in unobserved traffic.

The isolated actual-prefix replay in `artifacts/capacity-learning-01` produces
one support from33 public transitions and recalls it for the saved input of
decision32; disabling the channel omits it. No model or simulator calls are made,
and the running attempt's source capsule is unchanged.
