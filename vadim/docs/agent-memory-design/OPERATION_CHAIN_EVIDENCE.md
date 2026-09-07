# Operation-chain evidence (experimental)

Purpose: retrieve related observed episodes as a unit instead of learning only
that an asynchronous command was accepted. This is an index over experience,
not a promoted causal lesson or a new world-identity authority.

The input is validated LessonEvidence with explicit learning-run cutoffs. Frozen
evaluation declarations remain excluded. Opaque operation IDs are scoped by run;
initiated and observed links provide the join. An environment adapter resolves
public completion status. Core extraction must not know SRE commands or response
field names. Generic recorded objective metrics retain their exact names/units.

A chain preserves source record hashes, initiation, observed completion if any,
and metric observations bracketing the operation. Full intervening evidence is
retained within a configured iteration bound. Missing completion or metrics remain
missing. Pending results cannot borrow a terminal response beyond the cutoff.
Duplicate initiations, time/order ambiguity and context changes must not silently
produce a confident chain. Multiple operations and other activity in a metric
interval prevent attribution; all chains explicitly have causal_credit=false.

No source observations are rewritten or backfilled. Stored zero objective deltas
are not used to fabricate new causal deltas: bracketing distinct public metric
observations produces a labelled temporal comparison. Absence of another command
does not exclude changing workload or exogenous events.

Scope of the first implementation: extraction and offline corpus verification.
No automatic promotion, live module registration, decision-context contribution,
persistence migration or altered default profile. Request-sensitive retrieval and
charged source expansion require a separate bounded integration and validation.
The public operation-status adapter belongs to the offline SRE probe; it must
match the exact operation ID and a successful public read before resolving status.

Acceptance evidence: cutoff/pending, failed operation, same ID in different runs,
ambiguous initiation, context/order changes, missing measurements, unchanged
metrics, bounded intervals and overlapping operations; then the frozen public
corpus through the same extractor. Replay remains development evidence, never
counterfactual policy success or an economic experiment.
