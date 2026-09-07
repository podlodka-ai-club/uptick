# Handoff — active memory-tower Goal

Current as of 2026-09-07: user approved and activated the Goal in
`docs/agent-memory-design/MEMORY_TOWER_GOAL_DRAFT.md`. No token budget specified;
existing live-attempt limits remain. Previous Goal completion below is historical.

Latest Stage04: artifacts/memory-tower-preparation/STAGE04.md. Public runtime
ingestion now uses explicit allow_observed_learning=True and record_observed_learning;
default composition rejects observed learning. Writer and reader share one world
module. Known frozen declarations survive omission, duplicates and caller mutation.
31 focused tests and local review pass. Fresh real corpus preparation through
this API and separate-process read pass off0/on1/same0, all readers learning=false,
no warnings,5834/8000 on-context tokens. Artifacts: world-learning-runtime-01 and
world-learning-runtime-read-01. No simulator/model calls; all processes terminal.
Next: durable handoff, explicit launcher integration, remaining mechanisms,
actual model utility and independent transfer. Full Goal remains active.

First offline stage: `artifacts/memory-tower-preparation/STAGE01.md`.
Real corpus08: all620 raw records passed integrity/provenance; a new honest
observed-byte snapshot was created. Strict lesson evidence rejects missing run
declarations; all619 transition contexts are null. 27 transitions have delta
arrays, but all216 delta values are zero. Successive observed metrics do change,
with 3–16 intervening decision iterations; see `corpus08-metric-signal.json`.
Do not equate those associations with single-action causal credit.

Implemented: candidate proposal now permits failed/interrupted/ineligible
**declared learning** observations; promotion rules remain strict. Lesson batch
schema1.1 stores new semantics, schema1.0 replays old extraction semantics.
48 focused lesson/evidence/runner tests +5 consolidation tests passed; Ruff and
local review passed, including a legacy replay compatibility fix. New offline
tool: `scripts/probe_memory_evidence.py`. No new live/model calls this stage.

Stage02 completed as an intermediate implementation:
`artifacts/memory-tower-preparation/STAGE02.md`. New `memory/associations.py`
uses `validate_observed_evidence` (integrity/provenance only); strict
`validate_evidence` still requires run declarations for existing promotion paths.
Same real corpus08 gives538 unaccepted temporal metric associations,167 with a
13:00UTC cutoff,0 with no explicit learning selection. Prefix output exactly
matches the cutoff subset of full output. Input hash unchanged. Complete
interval refs retained; no single-action causal credit, no world identity claim,
no decision retrieval.53 existing tests and8 new delegated tests passed; Ruff
and local quick review passed. No new simulator/model calls.
Feature inventory is in `artifacts/memory-tower-preparation/feature-inventory.md`;
root corrected the initial conflation of class defaults with actual C/D config.
Actual C/D episodic1.2/raw recall and advanced lexical retrieval are on, legacy
is off; higher derived modules remain off. Do not repeat corpus construction as
if real raw episodes had never been used in decisions.

Stage03: `artifacts/memory-tower-preparation/STAGE03.md` records five verified
descriptive immediate-response patterns from real corpus08. Existing
WorldModelMemory now has explicit schema1.5 experimental opt-in persistence and
retrieval. SQLite new-process probes world-context-01 and02 pass world off/on/
current-source-run exclusion. First on arm: one fact,5834 estimated context tokens
inside8000,19.89s cold retrieval. Same-run arm has an episodic materialization
warning, retained verbatim; do not call that arm entirely clean.
`decision-boundary-01/` captures actual StructuredDecisionModel.decide client
requests with off0/on1/same0 facts, without a provider call or fake decision.
This is a mechanical fixture, not utility or full runner integration. Source
run is interrupted; `running` means immediate response, not completion.
Follow-up world-context-04 supplies the missing same-run query iteration480:
world off0/on1/same0, all warnings empty. The original warning was correct rejection
of an incomplete query fixture; production freshness checks are unchanged.
Source DB had an unknown sqlite3 reader lock; root used a consistent SQLite backup
world-context-source-02 instead of killing it. Capture decision-boundary-02 passes.
Historical schema compatibility test reverified passing; old raw fingerprint
unchanged. Bounded production closeout review is now complete (details below).

Batch provenance references now resolve to actual SQLite records (root verified
world-context-05/provenance-check.json). Per-member gets replaced by one canonical
namespace list,17 focused tests pass. Latest world-context-05 on18.89s/same19.12s
does not demonstrate a clear speedup; repeated whole-corpus validation remains.
decision-boundary-03 passes with corrected provenance and no provider calls.

Shared batch verifier implemented: evidence/selection once per equal settings/
cutoffs group; each summary independently recomputes its counts.20 focused tests
pass. world-context-06 on11.16s/same11.28s, off0/on1/same0 facts, no warnings.
runner-boundary-01 now verifies the actual AgentRunner->composed memory->decision
client path on a recorded public response, off0/on1. Capture-only: no model output
or environment action; intentionally failed local fixture outcome is labelled.
Production quick review scope is frozen in stage03-review-scope.json.
Review found and fixed P2 AR-OBS-1: membership inferred from summaries became
empty for unprojectable selected records, and raw snapshot size outranked selected
coverage. Membership now derives once from validated records/cutoffs and ranking
prioritizes selected coverage. Two regressions added;41 focused tests pass.
Post-fix scope: stage03-review-scope-pass2.json. Root targeted review is clean;
fresh three-batch real-corpus read preserves the complete final context exactly.
Evidence: stage03-review-result.json and revision-01/post-review-retrieval.json.

revision-01 completed: real probe_page counts1/0 ->1/1 ->4/1; superseded summaries
excluded. Independent raw JSONL check covers all9/10/13 visible summaries and
cutoff-safe refs; early terminal outcomes hidden, source SHA unchanged. Broad
projection groups different pages, so no causal/reliability inference. No model
or simulator calls. All root probe handles are terminal.

Next: actual provider use and all remaining
mechanisms/independent comparisons. Do not mistake correct descriptive counts
and capture-only transport for useful model decisions.
Stage03 lacked an observed-learning facade; Stage04 resolves that API gap.
Normal finalize still follows the original strict source. A retrieval flag alone
does not automatically learn after a new run; launchers must invoke the explicit
learning step. Frozen evaluation must never silently become training.
Do not invent hidden-world hashes or mistake candidate persistence for the
Goal's required accepted knowledge and decision-use chain. Continue all
mechanisms, progressive profiles, actual utility and transfer checks.
Agent audit `candidate-gates.md` distinguishes raw capture, proposals and promotion;
its diagnostic-only suggestion is an intermediate step, not the Goal finish line.

## Historical experiment handoff (status below must be reverified)

Work only `vadim/`; preserve dirty worktree/siblings; no commit/push. The prior Goal remains COMPLETE. User subsequently explicitly authorized four new parallel memory experiments; these are attempts13–16, A LIVE; B/C/D CLOSED, no automatic replacements or new Goal inferred.

## Current task

All four independent seed45 runs started around 2026-09-06 19:05:39–42 UTC (September7 00:05 Asia/Yekaterinburg). Terra medium, same frozen auth source, policy1.6, max480 decisions/1920 actions, combined120s fresh-read+provider deadline,7200s original wall per run. Expected public horizon172800s; SLO>=.99 plus positive native score and full completion. Parallel run creation is allowed; server queues one advance perparticipant/globaltwo. Do not bypass queue or reset deadlines.

A: fresh public overview + immediate action feedback; no previous plan/recent/history/episodic memory.
B: same + bounded ordinary previous decision/recent/action-result history; no episodic memory.
C: B + fresh same-run episodic1.2 memory.
D: C + immutable training08 corpus620records.

ALL arms replace environment cached model-visible run_state with fresh public overview (or public busy error). Extra reads and combined elapsed time are recorded. Transport/session safety remains intact. Exact startup SHA f7c4977d8ff862dff4ccdb1469c3f2051bc1444ef7a4819f785d404cd8ff2337 verified before model calls in all four. Fresh OpenAPI unchanged. One seed perarm is exploratory; do not pool with prior11/12 because current perception changed.

## Process handles — verify before doing anything

| Arm | Main exec | Watchdog exec | Run ID |
|---|---:|---:|---|
| A |67273|79457|DoN8G1nzG3fKpmaPxsVvMvGD|
| B |58153|45564|78gBmERHeoLfCFREpcAwU8TK|
| C |61314|67170|YzT8u5uLAtxfiILK7qFB2uPo|
| D |30804|71283|itpZoup6hyvJLca7w6sPrfge|

Exact original deadlines/plan hashes/start receipts in `artifacts/four-arm-memory-20260906/launch-receipt.json`. All watchdogs confirmed guard_started. Never start duplicates based on an observation timeout. Actual main exit must be checked separately even if watchdog reports terminal; preserve cleanup errors and use only inherited exact-identity signal procedure if genuinely needed.

## Evidence and next action

Directory `artifacts/four-arm-memory-20260906/` holds frozen PROTOCOL.md, plansA–D, contracts, freeze.json, preflights, root-review.json, reviewed copied runner, ablation.py, launch helper, tests, public spec, launch logs and watchdog logs. Independent subagents four_arm_isolation and four_arm_harness_review finished. Root verified their actual fixtures and ran9 tests plus1 combined-deadline negative test; Ruff clean. Source scope hash a53fb4db749e2fc24413386862ff72c5928461c5c39f147fc274f7451dced75e. No production/source-capsule mutation.

Initial provider-boundary check saw3 successful answers in EACH arm, no provider errors. A requests havezero memory/history/previousplan; Bzero episodic memory buthistory; Ccurrent-run memory only; Dhistorical+current memory. This proves treatment application in the checked live prefix, not utility or full success. Exact metadata in initial-provider-boundary-check.json. Do not mutate live code/plans/corpus based on answers.

Next: monitor existing processes, then after ACTUAL exit capture immutable final artifacts/accounting for seed45. A/B/C use no-historical flag; D allows exact training run bdnX2wsmrAtl2ykhHdEovjvS. Report all failures, truncated horizons, extra overview reads/time, tokens/calls/retries/unknown usage, SLO/score/envcost, repeat diagnostics and bounded stale-evidence findings. Model monetary cost unavailable unless observed. No extra model/API calls during cleanup. Do not copy/read live SQLite. Exact provider requests are authoritative; prompt_trace is deliberately a pre-read placeholder.

Previous12 closed attempts and completed Goal comparison remain intact at artifacts/paired-memory-preparation/paired-sre-results/; prior handoff archived at docs/agent-memory-design/HANDOFF_BEFORE_FOUR_ARM_LIVE_20260907.md. Secret `.simulator-participant-token` stays ignored0600: never inspect/print/hash/copy; only private authorized launch load. New four-arm series does not reopen or relabel old results.

## Status check 2026-09-06 20:20:40 UTC

D completed full48h with score100, uptime99.8970209%, RUB21,845,623.93,267decisions in60.56min. Main30804 and watchdog71283 exit0 verified; guard terminal/not_signaled. Corpus unchanged. Receipt four-arm-memory-20260906/terminal-process-D.json; final snapshot/accounting still pending. A/B/C and their guards verified live. Latest public progress A42125s (24.38%), B129425.352s (74.90%), C152325s (88.15%). A made little simulated progress since previous check despite many answers; do not change a live arm or reset its budget. Original deadlines remain21:05:39–42 UTC.

## Status check 2026-09-06 20:49:47 UTC

C completed48h,100points,uptime99.9132246%,RUB22,873,623.44,405decisions,88.14min; main61314/watchdog67170 exit0 verified. B consumed480decisions, ended failed with HTTP409 RUN_BUSY; final public observed135334.184779453s=78.32%,uptime99.790656%,native score/SLO null. Main58153 exit1/watchdog45564 exit0 terminal/not_signaled verified. No provider errors in either; corpora unchanged. Terminal receipts saved; full accounting/causal diagnosis pending. A main67273/watchdog79457 still live,459answers,126132s=72.99%,original deadline21:05:39Z; no restart/intervention. D remains previously closed success.

## B diagnosis and corrected C/D comparison

User corrected expense comparison from B/C to C/D. Read-only reports in artifacts/four-arm-memory-20260906/b-analysis/{failure.md,failure.json,C-D-comparison.md,C-D-cost-details.json,accounting-C.json,accounting-D.json,source-index.json}. Fresh public OpenAPI identical; active V2 objective full SLO+cost bands, no revenues/profit. OldV1 maximized final balance. B480dec/515actions;72of141completedadvances error-stopped, applying36,899 of185,919requestedseconds. Last480 accepted pendingadvance then finish() GET overview hit409; explicit budget-exhaustion result masked. Final server state after pendingop unknown; no extra API continuation. DB remained unavailable and migration only began460. No code changes or new runs. C/D bothfull100; env22,873,623.44 vs21,845,623.93 (D4.494%less); tokens12,680,462 vs8,645,459 (D31.821%less), complete incl2providerretrieseach, zero accounting violations. One development pair does not establish causal memory effect.
