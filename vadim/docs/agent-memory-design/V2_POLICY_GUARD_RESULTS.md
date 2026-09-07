# V2 no-stop wait guard — development evidence

Continuation starts at `dbfe6c8bb4db0aa09adfb5fa3af2d853b7a0f74b`.
This work diagnoses an observed decision failure and checks a versioned policy
correction. It does not close a memory-learning, holdout or default-promotion gate.

## Observed failure

Historical pilot 9, run `XQqybzunrxvpO8ul2VvQYZ7w`, used seed 42,
`gpt-5.6-sol`, low reasoning effort, no memory and 40 decisions.

- Decision 24 read two new `SERVER_CAPACITY_EXCEEDED` errors on a complete
  status-500 log page. No evidence established that future failures were harmless.
- Decision 25 requested `advance_time_v2(duration_seconds=67927,
  stop_when=None)`. The model called this a bounded diagnostic interval and
  wanted to avoid spending its remaining decisions on repeated investigation.
- Policy 1.0 preserved every explicit no-stop wait, so almost 19 simulated
  hours passed without stopping for errors.
- Decision 26 measured `downtime_seconds=11248.954219974` after
  `observed_seconds=129326.753146672`, with `475473.246853328` seconds left.
  Total horizon: 604800 seconds; its 1% downtime allowance: 6048 seconds.
- Decision 27 correctly recognized irrecoverability and advanced to the end.
  The earlier unmonitored interval had already exceeded the total allowance.

The retained trace is
`artifacts/v2-codex-pilot-2026-09-05/attempt-9/seed-42/trace.jsonl`.
Its original source and outcome are unchanged.

## Architectural decision

Keep HTTP action execution exact. The opt-in v2 decision-policy wrapper owns
any adjustment to a model-proposed wait and records it in the effective decision.
Unknown future health is not evidence that stopping can be disabled.

An unrecoverable full-horizon SLO requires finite, consistent, current public
evidence: `downtime_seconds > 0.01 * (observed_seconds + remaining_seconds)`.
Current uptime below 0.99 alone is insufficient: early downtime can still be
diluted by future availability. Missing observations do not prove failure.

The fix uses this public objective and observed counters; it introduces no
hidden world rules, oracle actions, learned claims or simulator transport changes.

A second context ambiguity is visible in pilot 9: decision 19 describes two
servers as a backend-plus-database minimum, while the actual resource inventory
contains two backend servers and a separate database server. The terse
`active_instances=2` summary does not communicate this distinction. Resource
summaries should preserve the returned active counts by role, without creating
a new cache or treating current utilization as proof that earlier capacity
errors are resolved.

## Verification

Policy `simulator-v2-time-budget@1.1` restores the default first-error stop for
unknown or recoverable SLO evidence. It permits explicit no-stop waits only
when the latest response contains consistent objective counters and clock
proving the full-horizon allowance is already exceeded. Duplicate counters,
wrong units, nonfinite values, inconsistent counters and overflow fail closed.
The prompt trace reports the same eligibility evidence used by the decision.
Pending operations retain their requested duration; the environment still
executes the effective action exactly.

- Focused policy/environment checks: **43 passed**.
- Full locked suite: **536 passed, 2 skipped in 6.68s**; the skips are opt-in
  live adapter tests, not failures.
- Ruff, formatting for the five changed Python files and diff whitespace passed.
- Replay of the retained pilot-9 decisions through the new wrapper restores
  `new_log_errors=1` at decision 25 (duration 67927 unchanged), while preserving
  the justified no-stop wait at decision 27 (duration 475474 unchanged).
- All four previously sealed memory experiments still pass independent
  artifact/lifecycle/binding verification after the policy version change.
- Local quick review of the code/test diff completed clean. No transport or
  memory-core dependency was introduced.

## Frozen live diagnostic

- Development seed 42; Codex `gpt-5.6-sol`, low reasoning, no memory.
- Budget: 80 decisions and 900 wall seconds, fixed before the first API call.
- Run: `TaQUWekTn2Vq3YM3ch0MUTc8`.
- Base revision: `dbfe6c8bb4db0aa09adfb5fa3af2d853b7a0f74b`; dirty source
  capsule SHA-256:
  `81030ce99da93ae7f832531cd7817a4f63f0e9886cff842169756fbd7b399e81`.
- Lock SHA-256:
  `02e9796facefb5f44da68fbd115a4db6958d1a72785e5ead2cc100f26a0c2191`.
- Started: `2026-09-05T05:00:17.710871+00:00`.
- Ignored manifest, frozen source and trace:
  `artifacts/v2-policy-guard-2026-09-05/policy11-seed42-sol-low-80/`.

The CLI exited with code 0 after **80 decisions in 871.06 seconds**. The
simulator remained `running`; stop reason was `maximum step limit reached`.
Observed time was **87918.157230829 / 604800 seconds (14.54%)**, downtime
**403.330497405 seconds**, uptime **0.9954124323108131**, SLO **null**, total cost
**1490616412 minor RUB**. This is an incomplete unsuccessful attempt, regardless
of the clean process exit or current uptime. The runner made 34 operation reads,
31 returning `running`, and three backend-create commands completed. Seed 42
remains a development seed. Changes to policy, prompt and resource summary together
with a doubled decision budget are not a controlled one-factor comparison
against the historical 40-decision pilot. This is not a memory ablation.


## Operation-polling follow-up

The first policy-1.1 diagnostic exposed a separate prompt problem: the instruction
to poll an operation until terminal spent decisions 18–23 and 33–39 on consecutive
status reads. Public progress increased slowly with real elapsed time. A bounded
advance at decision 40 was followed by a successful operation result at decision
41. This does not establish a universal operation duration.

The follow-up changes only the v2 prompt: poll once, then use public progress and
clock to choose a bounded monitored advance before polling again. Investigate
errors that stop that advance, and use an operation result only after its public
status is terminal. No poll-count state or command rewriting was added to the
policy wrapper. Existing applicable policy, provider-boundary and CLI tests:
**53 passed**. Ruff passed; a focused review of this semantic delta was clean.

A second frozen diagnostic uses the same model, seed and 80-decision/900-second
limits. Capsule comparison verified that only `llm/prompts.py` differs:

- Run: `baRQeQU3gPUvlcfkkh7XzOT0`.
- Source SHA-256:
  `67c3ebc1707c8c65327e43ef073e873559d88f7decf7be9afae47a7cbce7ee28`.
- Base revision and lock hash are the same as the first diagnostic.
- Ignored manifest, source and trace:
  `artifacts/v2-policy-guard-2026-09-05/policy11-polling-seed42-sol-low-80/`.

The wall timer interrupted this attempt after **900 seconds and 78 executed
steps**, before a `run_finished` result; process exit was 1 (`TimeoutError`).
The interruption was retained at `2026-09-05T05:26:35.059890+00:00`. The last
completed action's public clock had **516780.274289547 seconds remaining**
(**88019.725710453 / 604800 seconds, 14.55%, covered**).

The latest measured objective snapshot is from **decision 74**, not the end:
`uptime_ratio=0.9957498183080795`, `downtime_seconds=373.520664107`,
`observed_seconds=87883.457974762`, `total_cost_minor=1464916405`. No terminal
SLO result or final cost was collected. These values must not be represented as
full-horizon performance.

Operation reads fell from 34 to 15; reads returning `running` fell from 31 to
12. The replacement cycle still consumed decisions when first-error stops
interrupted waits: this attempt made 18 log reads and 18 time advances, versus
11 and 12 in the first attempt. Thus polling behavior changed, but neither
attempt completed the horizon: **0/2 demonstrated SLO success**. A single
stochastic follow-up on a development seed does not establish a causal gain.

## Remaining diagnosis and next gate

Review of both traces and request serialization found no evidence of lost
resource/catalog payloads, incorrectly truncated log pages or pending-operation
state corruption. The main remaining gap is incident diagnosis and a justified
capacity target:

- First attempt, decision 50: returned aggregate load 633 against capacity 300,
  with each backend reporting load 211 against capacity 100. The next change
  added one backend; that did not demonstrate enough capacity for the observed
  load.
- Follow-up, decision 74: load 632 against capacity 400. Another single-backend
  create followed at decision 76, without establishing target sufficiency.
- Errors have required/available capacity but no event-level server attribution.
  Source, IP, region or user-agent labels alone do not establish legitimacy or
  hostile traffic. Routing and traffic-causality hypotheses remain unresolved.

The next development experiment should require an evidence-backed remediation
and capacity-sufficiency argument, then measure full-horizon behavior with a
budget fixed before execution. Do not compensate for failed reasoning by adding
hidden simulator rules or by claiming the optional memory modules have learned
anything. The successful-baseline, immutable-world, held-out learning-utility
and default-promotion gates remain open.
