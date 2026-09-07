# Mechanism recovery — 2026-09-05

## North star and boundaries

Complete environment objectives using public observations, within an explicit
decision/time budget. Demonstrate improvement in retained runs, then measure
incremental persistent-memory utility. Architecture and test counts are supporting
evidence, not substitutes for successful behavior.

Keep fixes in reusable mechanisms. Do not encode simulator incident schedules,
world signatures, evaluator labels, or correct actions in the runtime or memory.
No simulator internals are an input to this investigation. A development replay
may use only observations preceding its selected decision; later outcomes stay
outside model requests. Both memory and no-memory agents must receive the same
within-run working context. Historical attempts remain unchanged.

## Recovered checkpoint

Base implementation: `ae9cb69`. The previous task was stopped before this
continuation. Existing uncommitted documentation and the unfinished
`EpisodicRecallSettings` declaration are preserved separately from this work.

The independent artifact verifier accepted dev-05's source, startup, schema,
prompt and request/trace bindings. The attempt timed out after 2400 seconds:
160 structured results, 159 recorded actions, no completed-horizon result.
This is a retained failure, not evidence of an effective memory system.

## Hypotheses and falsification

1. **Lost tool evidence:** dev-05 repeatedly asks whether a larger backend type
   exists, after observing a singleton catalog. Catalog reads occurred at steps
   20, 29, 117, 124, 131, 136, 142 and 149. The generic context retains only the
   previous decision and six action summaries; the adapter retains overview,
   resources and metrics. The catalog itself disappears. Test bounded run-local
   action/result retention without adding world-specific selection rules. In
   prefix-only decision replays, measure redundant catalog reads and explicit
   use of already observed limits, including failures and added context cost.
2. **Inefficient information gathering:** 30 unfiltered incremental log reads
   compete with 11 targeted queries. Check whether tool selection, backlog or
   missing returned evidence causes these choices before changing dispatch.
3. **Plan quality and revision:** carrying a plan does not preserve the evidence
   that falsified alternatives. Serial undersized changes, premature reversal,
   and repeated disproven hypotheses need outcome-based tests, not just a field
   asserting that a plan exists.
4. **Transport and context cost:** 160 logical generations consumed about
   4.49M input tokens. Measure latency and additional context; do not infer that
   more reasoning, longer deadlines or larger memory automatically help.
5. **Observability boundary:** review credential sanitization for destruction of
   public operational information. Any correction must still protect actual
   credentials and retain externally supplied startup/schema ownership.

## Execution sequence

Confirm evidence loss; implement the smallest general correction; check it in
an unrelated environment and verify trace parity, bounds and run reset. Run
preregistered public-prefix model probes before another expensive simulator
attempt. Preserve every outcome, review the diff, and update this record and
HANDOFF with actual results and remaining work.

## Implemented and locally verified

The generic runner now retains the latest observed result for each exact action
in a redacted, run-local observation view. Records are capped at 1,000 UTF-8
bytes, the serialized collection at 8,000 bytes, and count at 24. The latest
result is excluded from that view when constructing a decision because it is
already present in `latest_result`. Repeated actions still execute. Action
parameters form the identity before redaction; only their digest is retained
as the private index key. No environment action names enter the generic policy.

Truncated records retain step/provenance, success and terminal status, and
bounded summary/action-kind metadata. Metadata budgets account for JSON
escaping as well as UTF-8. This prevents a large action body from hiding a
failure and a large result action-kind from aborting the runner. Environment
and observer mutation cannot rewrite the recorded action/result snapshot.

This is a **latest observation view**, not a complete event journal or a
crash-resumable store. Re-reading the same action replaces its older view;
complete transitions remain in the normal trace/storage paths. Short-term
context is constructed identically regardless of persistent-memory selection.
The new view contains only executed in-run actions/results, never future trace
rows, evaluator labels or another run's records. The full latest result is
still unbounded; this patch does not claim a global prompt-size limit.

The deterministic replay checked all 159 dev-05 action prefixes. Both the
initial 2,000/24,000-byte variant and the final 1,000/8,000-byte variant retain
a complete prior catalog in **six of seven repeated catalog-read situations**.
Neither retains the catalog across the 88-step gap preceding step 117.
Total user-context bytes, excluding system/schema, are:

| Variant | Bytes across 159 requests |
|---|---:|
| Original context | 2,964,665 |
| 2 KB record / 24 KB history | 6,024,515 |
| 1.2 KB record / 12 KB history | 4,495,302 |
| 1 KB record / 8 KB history | 3,935,377 |

The smaller default is an explicit development-informed tradeoff: it adds
about 33% to this part of the prompt instead of doubling it, while retaining
the measured catalog evidence. These are byte counts, not token/latency
measurements. Availability is not proof of model use. Reproduce with
`uv run python artifacts/mechanism-recovery-2026-09-05/audit_prefixes.py`;
the source and trace hashes accompany `local-prefix-audit-final.json`.

The final full local suite passed **667 tests, 2 opt-in live skips**. Ruff and
scoped diff checks passed. Tests cover an unrelated toy environment that needs
an observation after more than six intervening steps, runner reuse/reset,
identical-action replacement without skipped execution, parameter identity,
Unicode/escaped-text bounds, secrets, and mutable boundary isolation. The
architecture and historical-contract checks are included in the full suite.

Quick closeout review accepted and corrected unbounded truncation metadata and
excessive context growth. The full-latest-result issue was recorded as an
existing limitation requiring recoverable reads, not fixed by dropping data.
The suggestion to preserve all changed results in this bounded view was not
adopted: this component deliberately retains the latest result per exact
action; it does not replace the full transition history. The targeted follow-up
review was clean. Scope: five implementation/test paths, base `ae9cb69`, final
patch hash `7d32e745ba125b661624ad771e7d397754bef9a1acd6c357609bdbe1ca17fd4e`.

## Model experiment status and next action

The initial 16-cell prototype plan was **not executed**: automatic approval
review rejected external transmission before process creation, requiring
explicit authorization for trace-derived inputs and the OpenAI destination.
That rejected plan is preserved, with no invented outcome.

A replacement 16-cell plan using the final smaller context is prepared at
`artifacts/mechanism-recovery-2026-09-05/final-probes/probe-plan.json`.
It compares four public-prefix situations under the 2×2 history/guidance
variants, with Sol/low and the original public briefing/schema fixed. The
largest user context is 52,675 bytes; the largest complete message text is
80,701 bytes, plus the approximately 21 KB public response schema. No simulator
actions will be executed. The final capsule hash is
`e9db60d186dac4ade60868ddf9c5891d4f4a95c2381fdc84e6432c55f57a752e`.
The user subsequently explicitly authorized these OpenAI subscription requests
and continued experiments. All 16 final-plan requests completed; see below.

The final plan and all outcomes remain retained. For file handoff and
general-agent controls, see [the focused research](WORKING_MEMORY_RESEARCH.md).
The subsequent exact-read pilot is now completed below; direct filesystem
access and broad agent governors remain out of scope.

## Completed public-prefix model probe

All 16 preregistered requests completed without provider retries. Independent
read-only verification reproduced the source/request hashes and all eight
history variants from strictly preceding observations. Public startup text,
response schema, model and effort matched; only the declared factors changed.
No environment actions were executed and no future/evaluator/world-private
inputs entered the requests. This verifies the input boundary, not output safety.

| Context variant | Step 14 | Step 51 | Step 124 | Step 131 | Input tokens, four calls | Median seconds |
|---|---|---|---|---|---:|---:|
| Original | Broad log page | Status-500 log page | Re-read catalog | Create server | 133,737 | 12.93 |
| History | Broad log page | Broad log page | Firewall rule | Create server | 141,098 | 14.38 |
| Guidance | Broad log page | Broad log page | Re-read catalog | Create server | 134,237 | 13.03 |
| History + guidance | Error/time-window query | Error/time-window query | Firewall rule | Create server | 141,598 | 17.59 |

The combined variant selected targeted diagnosis at both log-backlog points.
At step 124, history removed a redundant catalog read, but the replacement
was a rule denying a user-agent based on an incomplete traffic sample. Neither
its effectiveness nor the absence of legitimate-client harm was established.
**Fewer repeated reads is not an adequate success metric.** No firewall rule
from this probe was applied. At step 131 even the original-context replay
avoided the historical repeat; the original failure is not deterministic.

History added about 5.5% to measured provider input tokens in this four-point
sample. The combined variant had a 71.72-second call with no reported retry;
its total latency was 121.07 seconds versus 55.60 for the original variant.
Do not generalize speed from four calls or suppress this outlier. These are
development-selected states, one sample per cell, not an SLO experiment or a
held-out persistent-memory comparison. Results and telemetry are retained at
`final-probes/probe-results/results.json` and `summary.json`.

## Completed exact-observation handoff pilot

The [separately frozen nine-episode experiment](OBSERVATION_HANDOFF_RESULTS.md)
completed in 15 logical model generations. Full inline observations and previews
with exact reads both recovered 3/3 public facts; previews alone produced three
correct abstentions. Exact reads cut peak user-message bytes by 90.1%, but added
34.3% total input tokens and took 2.65 times the generation time. Two read-request
decisions per answer outweighed the smaller messages in this one-use fixture.

The new run-local archive remains an isolated experimental component, with no
CLI/runner integration or default truncation of the latest result. Review fixed
quadratic metadata truncation in this new component; the complete local suite
now passes 674 tests, with 2 opt-in live skips, and Ruff passes. The frozen pilot
source is preserved separately from the later performance fix.

Do not spend another long SRE run merely because the microtests are green.
Next test compact evidence reuse and navigation cost across decisions, including
cases without a ready-made directory. Apply the same within-run mechanism in
memory and no-memory conditions. Successful SRE behavior and incremental
persistent-memory utility remain open north-star gates.

## Active-goal navigation/reuse continuation

The [second handoff experiment](OBSERVATION_REUSE_RESULTS.md) completed 18
questions in 28 model generations. All three conditions answered 6/6 correctly.
Literal search required no byte directory. Carry reused both available fragments
without another search and avoided both stale answers when the old value was
actually present. It reduced input tokens 14.5% versus repeated search and 20.7%
versus full inline, while generation time remained 1.85× full inline. Full local
checks now pass 677 tests with 2 live skips, Ruff and a clean scoped review.

The next full public SRE diagnostic is separately preregistered as dev-06,
using existing history/guidance and unchanged model/tool surface/budgets.
Archive navigation remains experimental and is not silently integrated into the
environment's decision schema. The active goal is still incomplete.
