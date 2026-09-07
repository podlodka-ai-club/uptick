# External environment and observability results

Date: 2026-09-05. These are development diagnostics against the authorized public
v2 simulator. They are separate from the controlled memory experiment in
`LEARNING_CYCLE_RESULTS.md`. No hidden simulator source, oracle, world files or
internal endpoints were used.

## Declared conditions and retained attempts

Both model attempts used seed 42, Codex subscription `gpt-5.6-sol`, reasoning
`low`, memory disabled, a 60-decision ceiling and a 600-second wall timeout.
Their source capsules, startup prompt and tool schema were fixed before model
construction. The actual sanitized server startup text matched the previously
observed external description. A source/schema correction required a separately
retained attempt; the first failure was not overwritten.

| Attempt | Source | Physical run | Outcome |
| --- | --- | --- | --- |
| `agent-dev-01` | `373f6ee369c757898d1a35d854b4cc0b446988de` | `bk9rZlWjelDngsa97veov1Po` | Provider rejected CIDR schema; 0 completed decisions; process exit 1 |
| `agent-dev-02` | `2fc633b36a913039ae4eddcc63f101745314afc3` | `K1Ov1amam35yi4XjfXD4Uw1Q` | 42 completed decisions; interrupted at the declared 600-second limit; process exit 1 |

The second attempt reached **14.4836%** of the public seven-day horizon. Its
last objective measurement was at decision 40: uptime **0.9993373478400721**,
downtime **58.016577125 seconds**, observed time **87552.083330291 seconds**,
and total cost **1079416344 minor units**. The last completed decision was 42.
These are intermediate measurements, not terminal SLO or comparative cost
results. Neither attempt demonstrated an SLO success: **0/2**.

The model selected the new `query_logs` **three times** and `query_metrics`
**twice**. Other completed actions were overview 1, resources 5, metrics 8,
time advance 7, incremental logs 10, control commands 2, operation polls 2 and
inbox 2. This establishes actual selection and execution of the new tools,
not their causal contribution to success.

## Bugs exposed by the live tests

The first model attempt failed with provider HTTP 400 `invalid_json_schema`:
Pydantic's `ipv4network` format is unsupported by Structured Outputs. Commit
`2fc633b` represents CIDR using the public string/pattern schema while retaining
strict IPv4/IPv6 network validation. Standard `ipv4`/`ipv6` address formats remain
supported. See the official [Structured Outputs schema constraints](https://developers.openai.com/api/docs/guides/structured-outputs).
The second model attempt accepted that corrected schema and executed decisions.

A second concrete defect was discovered from empty log queries after a public
`log_error` time-advance stop. The server emits nanosecond timestamps, but the
new action's Python `datetime` fields reduced them to microseconds. An inclusive
`to` bound could then fall just before the event that stopped time advancement.

An independent probe on smoke world `h7fZImU63QEDCjrRn36xBGjJ` established the
failure directly:

| Same error filter, upper bound | Matching rows |
| --- | ---: |
| `2030-01-14T04:13:50.524467+00:00` after datetime coercion | 0 |
| Original public `2030-01-14T04:13:50.524467913Z` | 1 |

The returned error row has exactly the original public timestamp. This supports
a diagnosis of timestamp precision loss in the query boundary. It does not
establish that this defect caused every unsuccessful decision in the model run.
Commit `c92e094599e6c439332a185ce3b91b0c952216a5` preserves the original RFC3339
text through actions, decision revalidation and HTTP. Exact fractional ordering
and timezone normalization reject reversed windows even one nanosecond apart.
Python datetime inputs remain supported, but cannot restore precision already
lost by a caller.

A live post-correction check used the canonical decision schema and normal
adapter execution on the same historical window. It returned **one boundary
error**, repeated the same log rows, and retained the exact upper bound in a
metric response containing **15 series points**. The retained result is
`timestamp-fix-verification/verification.json`.

A separate two-decision, 90-second-ceiling model acceptance check used source
`c92e094`, run `T3t3dlx2KGrNrlqhWv1qNJLC`, under `agent-schema-03`. External
startup/schema pins matched; the provider accepted the corrected schema and
executed overview and time advance. The CLI exited **0**, with the world still
running and SLO unavailable. This is a short integration success, not an SLO
success or an additional 60-decision effectiveness comparison. All three model
attempts have independently verified source/startup/action records.

Final offline verification: **583 passed, 2 opt-in live skips**. The separate
live checks above supply their own evidence. Ruff and changed-file formatting
passed; all **56 historical schemas and identities** remain identical.

## Direct API coverage

`tools-smoke-01`, physical run `hGq4ExLrvJiSMf2wBs6EvdBd`, made 11 recorded
lifecycle/tool calls on source `2fc633b`. External startup/schema checks, independent
bounds, query/snapshot metrics and incremental-state isolation passed. Its tiny
initial log window was empty; repeat/content/cursor checks were explicitly
vacuous and are not counted as successful nonempty coverage.

`tools-smoke-02`, physical run `h7fZImU63QEDCjrRn36xBGjJ`, added a public
172800-second requested time advance with the default first-new-error stop.
It made 13 recorded lifecycle/tool calls, exercised a nonempty status-200 page
and its explicit cursor, and preserved the incremental reader's state. It
changed virtual time, not infrastructure configuration. Its error-window checks
remained empty because of the timestamp defect above.

A four-query follow-up on that same fixed historical window verified two
identical nonempty status-200 reads and a disjoint next page, each containing
two rows. The error filter remained empty with the truncated upper bound.
The first local verification-helper attempt omitted two required session fields
and failed before HTTP; its intent, script and failure record are retained
separately from the corrected helper. These checks are adapter verification,
not additional model or memory-effectiveness attempts.

## Evidence and limits

All raw sanitized artifacts remain ignored under
`artifacts/observability-live-2026-09-05/`. Source/configuration plans precede
external calls, and independent verification recomputed capsule/startup seals,
physical IDs, sequential typed decisions and outcomes for all three model attempts.

| Binding | SHA-256 |
| --- | --- |
| `373f6ee` executed source capsule | `958cec24c2363c9f6a36103308282e102d53c81016ff22cbe35918540e8b439d` |
| `2fc633b` executed source capsule | `8459f198b077629afb117f32f433855adc9fc1497598925ead8d12be4439d354` |
| First model plan | `d1d8ce7723c0abc4669e7041e87f4f97567cf843307342544085ce9bc30a0255` |
| Second model plan | `47c22d502e1b9fb2879c681528f912217f92e0913884a01afb7ce03f1a23e15f` |
| `c92e094` executed source capsule | `1de76c33f563f9a14c579801d1c32f7ff0de2c01c4ead3f2c22ae8865a0c3ce3` |
| Short final schema-check plan | `aa3d92ddb9323b371e184a728f2829dcbabbdf912b0420f2567f140be6bc49d7` |

The CLI's disabled-memory path does not retain a raw provider request journal
for every decision. This verification establishes source/configuration and
retained startup/action evidence; it is not an audit of each internal provider
request or validation retry. The controlled learning-cycle experiments have
separate request/provenance verification and must not be conflated with this
narrower diagnostic evidence.

The public API still lacks immutable world-content and causal-family identity.
Schema and prompt hashes cannot substitute for those identities. Live derived
knowledge remains ineligible under the unchanged validator, seed 42 remains
development data, and no SRE memory-benefit, xMemory-benefit, superiority over
Alex's actual agent or generalization claim follows from these runs.

## Conclusions and next experiment

The controlled memory loop survived the architecture change (4/8 versus 8/8 in
sol-low-03). The live work exposed two integration defects that offline tests
had missed, and both now have regressions and real verification. The model can
select the new tools; their presence alone has not produced a completed SRE
success.

The next effectiveness experiment should diagnose from exact public error
windows and test sufficient corrective capacity through a complete horizon.
Freeze its source, environment instructions and budgets before running it, and
retain this unsuccessful diagnostic as development evidence. Establish a
successful no-memory SRE policy before interpreting another live memory
ablation. Independently, authoritative immutable world/family identity remains
necessary for live knowledge activation and a valid held-out comparison.
