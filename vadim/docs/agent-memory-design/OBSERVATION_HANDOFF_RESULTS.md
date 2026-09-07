# Exact observation handoff: development pilot

Date: 2026-09-05. This is a short-term memory mechanism experiment, not a
successful SRE run or evidence of persistent-memory utility.

## Question and decision

Can an agent recover a fact omitted from a small observation preview by reading
an issued reference, and does this reduce its total work?

**Recovery worked; total cost increased.** Keep the archive experimental. Do not
replace the runner's complete latest result with a preview by default on this
evidence. Smaller individual messages and cheaper completed tasks are different
objectives. A useful follow-up must measure reuse across decisions and navigation
without a conveniently supplied byte-offset directory.

## Frozen protocol and boundary

The plan was written before execution at
`artifacts/observation-handoff-2026-09-05/plan.json`. Source capsule:
`5831cfd3af81485a5bbe8f4505fe94281726566f034a92157d927b3b83217681`;
runner SHA-256:
`9523e31d8ef537ea42692730526150fb6a33e1251dcc5752e415dea794a74cc0`.

Three synthetic public manuals contain 40 records each, with opaque item IDs,
revision tags, filler text, and an explicit public byte-offset directory. Each
serialized observation is 107,099 bytes. The requested facts lie beyond byte
8,192 and outside the 1,000-byte preview. The three conditions are full inline
observation, preview alone, and preview with an issued archive reference.
Condition order rotates across fixtures. Model, instructions and response schema
are fixed: Codex subscription, `gpt-5.6-sol`, low effort, at most four decisions
per episode and 120 seconds per request. One sample per fixture/condition.

Each episode creates a fresh client and archive. The model can request only
bounded slices of that episode's issued reference. Paths, invented references,
other archives and evaluator data cannot address this store. All answer facts
come from the public synthetic observation. The evaluator obtains the expected
revision from that observation only after the decision loop. No simulator action
or private simulator state was used.

Requests, responses, read results and telemetry are retained in
`results/events.jsonl`; episode outcomes and aggregates are in
`results/outcomes.json` and `results/summary.json` below the experiment directory.
All nine episodes completed in 15 logical generations. No failure or experiment
retry occurred. Each call's adapter telemetry reports `request_count=1`,
`usage_reported_requests=1`, and `retry_count=0`; this does not inspect hidden
SDK/provider retries.

Independent verification passed 403 checks: frozen runner/source/fixture hashes,
all 36 events (15 requests, 15 responses, six reads), equal fixed request fields,
exact receipt digests and UTF-8 slices, prior-read-only context construction,
label availability, and all outcome/aggregate measurements. No discrepancy or
oracle leakage was found in these inspected boundaries.

## Results

| Condition | Correct | Abstained | Model calls | Archive reads | Total input tokens | Total output tokens | Peak user-message bytes | Total generation seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Full inline | 3/3 | 0 | 3 | 0 | 104,701 | 253 | 108,208 | 23.49 |
| Preview only | 0/3 | 3 | 3 | 0 | 43,650 | 345 | 1,299 | 21.47 |
| Preview + exact read | 3/3 | 0 | 9 | 6 | 140,607 | 1,164 | 10,720 | 62.22 |

All six archive reads succeeded. The read condition used one call to request the
directory, another to request the target record, and a third to answer. Peak
user-message bytes fell **90.1%**, while total input tokens rose **34.3%** and
total generation time was **2.65×** full inline. Median episode generation time
was 8.38 seconds full inline and 19.95 seconds with reads. These measurements
exclude client setup and local preparation; user-message bytes exclude system
instructions and schema. Do not present them as whole-process latency or total
provider-context size.

The preview-only control correctly abstained; it did not hallucinate the missing
answer. This shows why truncation needs either recoverable evidence or an
explicit admission of missing information.

## Implementation and review

`runs/observation_archive.py` provides a runner-independent in-memory component:
sanitized immutable JSON, random opaque references, SHA-256 receipts, source step,
explicit historical-data markers, UTF-8-aligned bounded reads, and oldest-first
eviction. Defaults are 1 MiB per observation, 8 MiB total, and at most 8 KiB per
read. Receipt summary and action kind also have serialized-size bounds. It is
not wired into `AgentRunner`, the environment schema or the CLI. It adds no
filesystem tools, learning-policy exceptions or simulator-specific actions.

Review found a real performance defect in the new component: bounding metadata
removed one character and reserialized the entire large string repeatedly.
The helper took about 7.4 seconds for 100,000 characters in the review probe.
The fix bounds the candidate prefix before trimming, preserving exact archived
bytes. A megabyte-scale behavior test covers both summary and action kind without
a flaky timing assertion. This was a defect in the new prototype, **not** the
cause of historical dev-05's timeout. The experiment's frozen source and results
remain unchanged; its short summaries did not exercise this defect.

Seven archive tests pass. The full local suite after the fix passes **674 tests,
2 opt-in live skips**, and Ruff passes. Coverage includes exact sanitized Unicode
reconstruction, mutation isolation, receipt bounds, run isolation, eviction,
unavailable/pathlike refs and malformed or misaligned reads.

The targeted quick re-review after the performance fix was clean. Base/head:
`ae9cb6952df54d4a47a30c2d9e09695dc13767b6`; final two-file archive/test scope:
`1f749c1ec5d9bbcdeea15fbfac4176d3cc988e3f88f041b058f5d53e46b19057`.
Four v2 review markers are retained and validated in `review-markers.log`.

The frozen pilot aborts after recording a provider exception instead of
continuing later cells. That is a limitation to correct in a separately versioned
future runner; all nine planned cells completed here, so no later cell is missing
from these results. Do not silently rewrite the executed pilot.

## Limits and next gate

The public directory makes navigation deliberately easy; arbitrary logs and
documents may need more reads. Repeated filler and three cases do not represent
real-context diversity. The test asks for one exact fact, not action selection,
plan revision or cross-run transfer. It does not establish safety of acting on
stale evidence, crash recovery, or a complete runtime context budget.

The next useful memory gate is evidence reuse: retain a compact index and fetched
fragments, then compare completed objectives, total model calls/tokens and stale
evidence mistakes under the same within-run policy in both persistent-memory
conditions. A bounded search/index must use only admitted observations and cannot
use evaluator labels to choose snippets. No default archive promotion or new
long SRE run follows automatically from this pilot.
