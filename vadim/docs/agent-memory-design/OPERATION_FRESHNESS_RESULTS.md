# Operation status freshness: public-prefix probe

Four preregistered decision-only calls completed on 2026-09-05. Result:
**inconclusive under the frozen decision rule**. No simulator action was
executed. This does not demonstrate SRE success or justify another full run.

## Inputs and intervention

The fixed development checkpoints were dev06 decisions136 and140. At each,
the exact retained request was compared with the same public observations and
plan, but with old accepted statuses represented as last-observed facts with
their actual source iteration/time and current status unknown. Derived policy
metadata stopped asserting current pending work and stopped authorizing its
special wait; numeric SLO bounds were preserved. A new hint explicitly requested
status reconciliation. This tests a combined representation and policy-hint
package, not provenance alone. No completion status was inferred from elapsed
time, inventory effects, future observations or evaluator information.

Plan: `artifacts/operation-freshness-2026-09-05/plan.json`, SHA256
`3a03e01c00ff7f7dcff5c363672384826e2a0b910e0f5da021cd8183cffbd191`.
Source reused dev06's frozen capsule. Same Sol/low and response schema; four
logical calls,120seconds maximum each, stop on first error. Independent preflight
passed195checks before dispatch. Exact/treatment order alternated by checkpoint.

## Recorded decisions

| Checkpoint | Exact context | Changed context |
| --- | --- | --- |
| 136 | 300-second advance, no error stop | Select another server creation |
| 140 | 300-second advance, no error stop | Select a filtered log query |

Both exact arms reproduced blind waits. Both changed arms selected `other`;
neither selected the preregistered reconciliation action. The frozen directional
criterion required at least one reconciliation, so it was not met. Reclassifying
these other actions as successes after seeing them would change the criterion.

All four responses were returned, with133,257 inputtokens,1,820 outputtokens and
58.26seconds of reported generation time. Alternative actions were not executed;
their consequences and comparative value remain unknown. These are selected
development prefixes with one generation per cell, not independent holdout cases.

## Implementation decision

The code/data mismatch remains independently established: acknowledged operations
are stored indefinitely and described as current pending work. Correcting that
description is a semantic fix, not a proven behavioral improvement. A production
correction must preserve safe numerical headroom checks and the legitimate
command → fresh metrics → bounded wait sequence. It must not turn lack of a fresh
status into a fabricated completion, absence, or forced long time advance.
Such a correction differs from this probe's eligibility intervention and needs
its own verification before any effectiveness claim.

The policy-only correction is now implemented as version1.3. Public stored
statuses remain unchanged. Metadata separately reports unresolved recorded
operations and whether the latest successful result reports an active operation.
Old accepted/queued/running or unknown statuses do not become completed or
currently running by inference. Numerical headroom checks and the duration-floor
exemption remain available for unresolved evidence, preserving the legitimate
command → metrics → bounded wait path. The universal runner was not changed.
Full suite:689passed,2opt-in live skips; Ruff and one bounded local review passed.
This implementation has not yet been tested for behavioral utility.

Independent result verification passed88checks. One serialization caveat is
retained: the query_logs response was stored with Pydantic field names
`from_time`/`to_time` rather than schema aliases `from`/`to`. All4responses validate
as the frozen response model and all4validate against strict schema after alias
normalization;3/4stored raw decisions validate directly. Frozen outputs remain
unchanged. Future probe writers should serialize decisions with `by_alias=True`.

## Terra/medium and actual policy 1.3

The user switched new decisions to `gpt-5.6-terra`, reasoning `medium`.
Four additional decision-only calls used the actual frozen production policy,
not the earlier hand-written intervention. At both retained checkpoints136/140,
policy1.2 selected a300-second advance and policy1.3 selected `server.create`.
These are respectively the preregistered `blind_wait` and `other` classes.
No operation-status reconciliation was selected and no action was executed.
This demonstrates compatibility and a prompt-sensitive change in selected action;
it does not establish that further provisioning is correct or improves SRE results.

The model, effort, system prompt, response schema, raw observations, history,
previous plan and numerical bounds match across each pair. Only runtime-policy
metadata differs. One generation per selected development prefix gives no robust
estimate of reliability. The earlier Sol experiment remains inconclusive under
its own frozen rule; this follow-up does not change that classification.

Artifacts: `artifacts/terra-policy-2026-09-05/`. Plan SHA256
`fe51cbef52a0504076216f4843cba7e5130cc70afc46056ae71afe8467baed39`;
source tree `bca4f28aa2e2fa21e4c8728336ebe671803d3bd720f3f35f6fa9d2b20d28b448`.
All4calls completed without retry:131,790 inputtokens,2,879 outputtokens,
40,448 cachedtokens and72.65seconds of reported generation time. Monetary cost
was not reported. Independent result verification passed51checks; all decisions
round-trip using schema aliases. All model calls and their outputs are retained.
The original preflight caveat about an absent runner source-tree guard was stale:
the hash-pinned runner includes that guard, and its final modification preceded
preflight and dispatch. A correction is saved in `result-verification.json`.
