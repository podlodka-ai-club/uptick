# Simulator v2 exploratory evaluation profile

Profile: `simulator-v2-uptime-cost-exploratory@1.0`.
This is the versioned v2 execution/diagnostic profile for Stage 7 and later
ablations. It does not replace the historical Stage 0 balance profile and does
not authorize module-default promotion.

## Task endpoints

The declared simulator horizon must be completed. Endpoint order is:

1. Authoritative terminal completion, reported separately from runner exit.
2. Completed-run `slo_passed == true` with uptime at least 0.99.
3. `total_cost_minor` among completed, SLO-passing runs.

Retain observed horizon and downtime in each available RunResult artifact.
The aggregate report includes uptime, total infrastructure cost, steps and wall
time; horizon/downtime remain inspectable in the underlying result artifacts. A `running` result, budget exhaustion,
startup error or interrupted attempt is not a successful task outcome. Cost
from such an attempt never ranks as a cheap success. Missing values remain
missing; legacy revenue/purchase/balance defaults are not v2 evidence.

## Preregistration and pairing

Persist an immutable manifest before starting its first environment or model
request. It declares the ordered condition/block matrix, split, replicate
indices, environment provenance, model/settings/prompt/source/dependency pins,
full resolved memory configurations, runtime policy version, budgets, raw-audit
and retention policies and failure rules. Every attempted cell gets an identity
before startup; failures without a simulator run ID remain in the record.

Freeze training output into separately hashed evaluation bindings before the
first evaluation request. Bind these to the original manifest hash; never
rewrite preregistration after observing training results. Every evaluation
condition reads only its bound training snapshots. Evaluation writes go to an
isolated, unreadable overlay, including between replicates and conditions.

Use the first declared attempt for primary paired comparisons. Retries may
diagnose infrastructure failures but cannot replace a failed first attempt with
a successful result. Retain both. Report missing and incomplete cells explicitly.

Paired completion and SLO contrasts include all declared blocks and their
unsuccessful outcomes. Cost differences use only blocks where both compared
first attempts completed and passed SLO, with that denominator shown alongside
the full matrix denominator. Conditional cost differences cannot compensate
for a completion/SLO regression. Report distributions, sample variance and
percentiles in addition to paired contrasts; tiny exploratory samples do not
establish a positive effect.

## Environment identity and eligibility

Endpoint and API-contract fingerprints pin observed integration metadata; they
do not prove immutable world content or distinct causal families. Unknown
environment/scenario content hashes remain explicit. Different positive seeds
alone do not constitute distinct contexts under candidate-validation v1.

Training declarations may supply eligible candidate support only when their
immutable context identity and admitted first-attempt ancestry are proven.
Otherwise raw episodes can still be evaluated, while higher knowledge remains
candidate/non-decision-visible under the existing validator. Test fixtures can
verify this boundary but cannot stand in for live generalisation evidence.

Seed 42 is already used for development. A final locked holdout needs a
separately identified causal family and access procedure established before
using it to claim improvement.

## Accounting and module evidence

Record model-call wall time and available provider-reported input/output/cache
tokens and cost without inventing missing values. Subscription cost is not
zero-priced API usage; unavailable monetary cost is null. Deterministic memory
context budget units remain separately labelled `utf8-byte-upper-bound@1.0`.

Record snapshot size, stored artefact counts, contributed items/context units,
module versions and construction/read/write/consolidation/contribution evidence.
Disabled modules must be absent from construction and decision-visible ancestry.
All conditions share the same bounded current-run state and v2 time policy.

Each later module has an explicit on/off contrast. A cumulative A0–A9 result
alone does not identify every interaction. Negative or absent effects leave
modules experimental and optional. Default promotion additionally requires the
complete profile, locked-family evidence, rollback verification and human
approval required by the normative evaluation strategy.
