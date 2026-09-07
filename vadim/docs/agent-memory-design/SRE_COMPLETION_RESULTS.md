# SRE completion diagnostic results

This document records four retained development diagnostics under
`artifacts/sre-completion-2026-09-05/`. None demonstrated SLO success.
The first, using policy 1.1, reached the seven-day horizon and failed SLO;
two policy-1.2 follow-ups timed out and one failed at the provider boundary.
The opening sections describe dev-01; the follow-up sections account for all
four attempts. These are no-memory baseline diagnostics.
The subsequent dev-05 diagnostic uses the decision-continuity correction and
is currently in progress; it is excluded from this four-attempt result table.

## Frozen bindings and evidence

The run was declared for the Codex subscription provider, model
`gpt-5.6-sol`, reasoning effort `low`, seed `42`, at most 160 decisions, and a
2,400-second wall budget. It used the source capsule at revision
`c519d34185e2b3731d44ca214b4a3dc8cc2d2a85`, with policy version `1.1` and no
memory. The independent verifier recomputed these bindings from the retained
files without starting a simulator or inspecting hidden simulator state.

| Artifact or binding | Exact value |
| --- | --- |
| `sre-plan-01.json` | `9606f53255dd1b5b73b07238951247cc479b4e3724f54f5a709b60600ce04e8b` |
| `run_diagnostic.py` | `eb207123e5e4e626819639e38803028473c2c893a547c549672b70470eb02673` |
| Source revision | `c519d34185e2b3731d44ca214b4a3dc8cc2d2a85` |
| Source capsule tree | `1de76c33f563f9a14c579801d1c32f7ff0de2c01c4ead3f2c22ae8865a0c3ce3` |
| Source `uv.lock` | `02e9796facefb5f44da68fbd115a4db6958d1a72785e5ead2cc100f26a0c2191` |
| Source `pyproject.toml` | `4cea9d976a0c988dca0338c146e94fd0f4d4b998d7d0be5e403c4b5d30a27e81` |
| Expected external startup briefing | `645f9492e4aa0123537b6cc9201981f16e6f0d78d7da6ce2ee1adb3a90c23d29` |
| Prompt fingerprint | `1ef42b9da212eb3c12be4f5c99dadf7646c549f17bd2ce2b222172000be7869a` |
| Decision schema | `e0764afbb63a57a229ecec7759d69e8015edacfbc25b1c7ea01649cb3973865b` |
| `outcome.json` | `9c9ee930cda942117c9f689ebd0118e1536194fcb487fb331f2dfaf92f28ed3f` |
| `provider-events.jsonl` | `bf11c4249564eccc9bfc26768dbaff0c599be9b0fad93fea0c00b7775619e05a` |
| `seed-42/trace.jsonl` | `fdf80a8cfb8798bf42207f512a55cc5aa9c6ac28d8d5a095a40ea6ee00e7110c` |
| `independent-verification.json` | `63b09feeceb116ebc76b18e1b5fe5adcf9598e0312e05c59086aff9ca345583e` |

The startup result contained the externally supplied sanitized briefing. Its
text matched `expected-startup.md` byte-for-byte and its SHA-256 matched the
declared value before the first structured model request. All 158 requests
used the pinned `uptick_agent.simulator.decisions.SimulatorV2Decision` schema;
the response-schema hash, prompt fingerprint, model, and generation settings
matched the plan and outcome pins.

## Provider and runner accounting

The durable provider journal contains 318 records: one startup result, one
startup-spec verification, 158 structured requests, and 158 structured
results. Request and result IDs pair exactly, with no retained structured-error
record. Those are 158 logical model decisions. Provider telemetry reports 159
underlying provider calls and one internal retry, so logical decisions must not
be reported as a provider request count. The summed reported usage is 4,200,383
input tokens, 51,832 output tokens, 4,252,215 total tokens, 2,922,752 cached
tokens, and 14,141 reasoning tokens. Monetary cost was not reported. Internal
SDK retry details beyond the recorded telemetry are not independently visible.

The runner trace contains 158 step records for iterations 1 through 158 and
one `run_finished` record. The first action was `get_overview`; the last was
`advance_time_v2`. The physical simulator run ID was
`vPsXY2XYp2WxhkHJZPCe5YUJ`. The retained outcome has `cli_exit=0` and runner
status `completed`; this describes process and horizon completion, not SLO
success.

## Objective outcome

| Metric | Retained value |
| --- | ---: |
| Simulation horizon / observed seconds | 604800.0 |
| Decisions | 158 |
| Duration (wall seconds) | 2340.058921875083 |
| Uptime ratio | 0.28668857914560514 |
| Downtime seconds | 431410.747332738 |
| Allowed downtime at 99% | 6048.0 |
| Available seconds | 173389.252667262 |
| Total cost (minor units) | 19403612903 |
| SLO passed | `false` |

The run therefore completed the full seven-day horizon with an objective
failure: downtime exceeded the 99% allowance by a wide margin. No retained
provider, runtime, timeout, or cleanup error row explains the outcome.

## What policy 1.1 did at the pending operation

At iterations 90 and 92, the model’s structured results selected
`advance_time_v2` with `duration_seconds=300` and `stop_when=null` while a
public `server.create` operation was still running. The policy annotation and
effective action in the trace restored the default first-error stop guard.
Both advances stopped on a public `log_error` after only a short simulated
interval:

| Iteration | Operation status / progress before wait | Proposed stop | Effective stop | Applied seconds | New logs |
| ---: | --- | --- | --- | ---: | ---: |
| 90 | running / 0.7285196621066666 | `null` | default | 4.060199463 | 1 |
| 92 | running / 0.78366754025 | `null` | default | 35.394038215 | 1 |

The operation later reached `succeeded` in the public trace at iteration 95.
The result is evidence that policy 1.1 blocked the requested no-stop pending
waits; it does not show that the public API failed to return data. The run lost
the intended long observation interval and spent decisions on repeated short
advance/poll cycles instead.

## Remaining decision and scaling evidence

The public trace shows incremental backend additions while the model was still
reacting to capacity errors. Before the third added backend, resources reported
two active backends with capacity 200 and observed load 632. After additions,
public metric snapshots reported load/capacity pairs of 633/300, 632/400, and
3841/500. The final retained snapshot reported 3830 load units on capacity 500,
with uptime 0.9304248 at that point; the run then advanced to the horizon with
the much worse final SLO result. The model also issued targeted firewall rules
for `WebCamera/1.0`, but the trace does not establish that this label or user
agent was hostile traffic or that the rule caused recovery.

These observations support a narrow diagnosis: policy 1.1’s pending wait guard
reduced the model’s opportunity to observe a completed operation in one
continuous window, while subsequent decisions remained reactive and
incremental. They do not identify a unique hidden-world cause or prove that a
particular scaling or firewall action would have met the SLO.

## Limits

The preceding dev-01 evidence is one development diagnostic on seed 42, not a holdout, randomized
comparison, or final evaluation. It makes no claim about memory utility,
generalization, policy effectiveness, or cost optimality. The verifier used
only retained public startup text, structured requests/results, trace records,
and declared artifact hashes; it did not read hosted simulator internals or an
evaluator oracle. The corrected-source runs below are separately declared
development experiments; they are not a controlled one-factor comparison.

## Corrected-source follow-ups and final diagnostic accounting

The corrected runtime is frozen at
`e8bc914c00365a3ea553ea28c0445f479b8f20b3`, capsule SHA-256
`93993839ec40eb64b6255f79eec803a7dd7e0553657c4b47c9e331ed553e5253`.
It retains three bounded timestamped public observation views and uses policy
1.2: a model-requested 300-second no-stop provisioning wait is allowed only
with conservative typed SLO headroom, exact public clocks and consistent
horizon arithmetic. The original public startup text and response schema are
unchanged. Each follow-up was declared before its first external call.

| Attempt | Model / effort | Policy | Completed decisions | Outcome | Horizon covered at last public clock | Last measured uptime |
| --- | --- | --- | ---: | --- | ---: | ---: |
| dev-01 | gpt-5.6-sol / low | 1.1 | 158 | completed, SLO failed | 100.0000% | 28.668858% (final) |
| dev-02 | gpt-5.6-sol / low | 1.2 | 156 | timed_out | 20.4205% | 95.643724% |
| dev-03 | gpt-6-astra / low | 1.2 | 0 | failed | 0.0000% | unavailable |
| dev-04 | gpt-5.6-sol / high | 1.2 | 132 | timed_out | 20.0137% | 97.229881% |

**Zero of these four attempts demonstrated SLO success.** Dev-01 completed the
horizon and failed SLO. Dev-02 and dev-04 hit their 2400-second wall limits;
their percentages are intermediate public measurements, not final SLO values.
Dev-04's last metric is from iteration 131; its last tool response is iteration
132. They must not be compared as successful final-cost outcomes.

Dev-03 failed on its first provider request. Captured terminal stderr said that
Astra requires a newer Codex client; the supplemental
`dev-03/provider-rejection-note.json` records this observed message. Its original
outcome retains the generic provider exception and physical run ID. Dev-04 is a
separately declared Sol/high fallback with the same corrected source and budgets.
It is not an invisible retry of dev-03.

The runtime defects were exercised after correction: preserved 300-second
no-stop waits occurred at the following retained iterations:

- dev-02: 24, 34, 41, 49, 67, 75, 79, 82, 88, 95, 102, 110, 117, 122, 127, 131, 137, 143, 147, 154.
- dev-04: 26, 33, 41, 66, 71, 76, 81, 84, 89, 96, 100, 107, 111, 121, 129.

The requests also contain the adapter's `last_observed` public views. This proves
that the implemented fixes were exercised; it does not attribute an SLO gain
to them. Both models still spent many decisions on sequential server additions,
operation checks and repeated observations under persistent capacity errors.
The corrected low-effort run ended after 156 completed decisions; high effort
ended after 132. Increasing reasoning effort alone did not establish successful
incident handling under the declared budget.

Each timed-out process exited 1 and retained the interrupted provider request.
Both emitted a `TransportClosedError exception in shielded future` diagnostic
while the owned SDK transport closed on cancellation. These stderr diagnostics
are preserved as supplemental observations; absence of a structured cleanup-error
row must not be described as a completely quiet shutdown.
Read-only inspection traced that message to Python 3.14's cancelled-shield
exception callback. The adapter explicitly drains the inner SDK task after
closing its transport; this diagnostic alone is not an unconsumed-task defect.

## Final independent verification

The reusable verifier checked all four retained artifact sets, including both
timeouts and the first-request failure. It verifies source/startup/schema/prompt
pins, contiguous trace steps, provider request IDs, terminal-horizon conditions
and available telemetry. Verification success describes evidence consistency;
it is independent of SLO success. The synthetic budget-stop verifier fixture
makes no external calls and is not a fifth simulator attempt.

| Attempt | Logical requests / results / errors | Reported adapter request count | Verification SHA-256 |
| --- | --- | ---: | --- |
| dev-01 | 158 / 158 / 0 | 159 | `3a866cbd430fa735e8e0ed53ec764c452e5c63df189ccf1918c35b1bacaef676` |
| dev-02 | 157 / 156 / 1 | 156 (partial) | `18aa08d632828f39375d497ee12d2b559cbbe7644ebbf8193c786e8402d30e76` |
| dev-03 | 1 / 0 / 1 | unavailable | `c3550b222682e0a5b9df021389aff0fa51fb6389ff509c8aead8e1eb4606e217` |
| dev-04 | 133 / 132 / 1 | 132 (partial) | `fa5808c8da71d7d38264594ef2eafa5feb13775bfa42e0dbf6a423f5ee70bc1d` |

Input/output usage in timed-out attempts is partial: the interrupted request
may have unreported usage. Monetary cost and provider-internal transport retry
details remain unavailable. Exact per-field coverage is retained by the verifier.

| Attempt | Plan SHA-256 | Outcome SHA-256 |
| --- | --- | --- |
| dev-01 | `9606f53255dd1b5b73b07238951247cc479b4e3724f54f5a709b60600ce04e8b` | `9c9ee930cda942117c9f689ebd0118e1536194fcb487fb331f2dfaf92f28ed3f` |
| dev-02 | `708e8725f1baf562394aeaf683106d6f20d77d94fc33177ff008b1f20f0bccae` | `13e01685828a47bacff1a3707c046f7b7318e5f6d84aad8d1c1ca583f41e1b8a` |
| dev-03 | `20901f6966b41f9e588746a4a16894c91af84bd38e177193c7ac7ca9e645f999` | `c78ddef9bd0d3de0d17e21e07146873437c6b76e419649cb251007251fd08a99` |
| dev-04 | `33db09ebd7932d904a97d0be896c26ccfa03ffe4e410a8eb0d8e5e086ab2e9ee` | `83a23b68e9273cabe9fcfb5c30001f55bbb812b1b8f36c0c6a95ac5a043e652a` |

Reusable verifier SHA-256: `132e182534cce243181487586009631439c7b8f07f1e2eef020c982fc1f355f8`.
Closeout summary SHA-256: `e84dace679f15b3056c7f3f0d29a66f10564984920a3dc9095e1c37ddf2619ef`.

The summary's `last_metrics` denotes metrics in a step response; dev-01's
authoritative final metrics are instead in `outcome.runner_result` and are
reported above. Its last intermediate uptime must not be paired with the
completed horizon. Follow-up diagnosis is recorded in
[decision-efficiency diagnosis](SRE_STRATEGY_DIAGNOSIS.md).
