# Active memory work

## Goal and boundaries

Persistent Goal is ACTIVE, with no global token cap. It is not complete. See
[ACTIVE_GOAL](ACTIVE_GOAL.md). Edit only vadim/ and
preserve unrelated dirty work. Internal model: gpt-5.6-terra / medium.
Never expose hidden state, future events, evaluator answers or answer-selected
corpora to memory/model requests. Existing Codex subscription experiments are
authorized; unset OPENAI_API_KEY and CODEX_API_KEY. Do not request approval again.

Owner priority: first prove persistence → relevant retrieval → model use and
abstention on a small task. Tune economics later. This chain now has evidence
on a small designed family; full SRE success and memory utility remain unproved.
Do not launch another expensive SRE run without a new distinguishing hypothesis.

## Expanded owner authorization — 2026-09-06

User now allows up to8full attempts if useful, counting current run as1 of8;
7remain after it. Per-attempt480decisions/7200s/120s, existingCodex subscription.
Record each attempt and preregister hypothesis/comparison before dispatch. This
is permission, not a requirement to spend all8; no blind repeats or replacements.
See artifacts/simulator-refresh-2026-09-06/attempt-budget-authorization.json.
Current frozen one-attempt plan remains immutable.

## Live attempt — 2026-09-06

ONE authorized fresh run is LIVE under
artifacts/simulator-refresh-2026-09-06/full-sre/run-dev-01/.
Root exec session66664 is active. Poll with write_stdin; do not restart or replace.
Actual run_id cFT5xFNHnrc7tc9kYK4xVH0M; started2026-09-05T21:25:29Z.
Output files are in run-dev-01/frozen_history/ (not directlyrun-dev-01/).
Fresh startup_spec hash verified before firstmodelcall. First5decisions completed,
6threquest underway at checkpoint; no observedmodelerrors.
Plan01 SHA6012d9f830d47780381370bd8e267adb5c8138701d15f1579b274c29a34c2816.
Terra/medium,480decisions,7200wallseconds,120seconds/logicaldecision,seed42,
same312historical raw records,complete history2000/base1000/global8000/max24.
User explicitly approved budget then requested simulatorrefresh; approvalresolved.
Actual API0.7.1/startup/source/schema are newly pinned. Old full-sre plan is
SUPERSEDED with0calls and must never launch. No automatic retry/replacement.

Refresh evidence: artifacts/simulator-refresh-2026-09-06/.
OpenAPI SHA297574eddcb3f227cd8f3155ed4204b8f9a713af7e37f4fef6e34b2815f580d4;
startup SHA9aaec4adb7b36cbaf8bb29d8ca24ad1ee4ea064003599ac81b3d622450773c2e;
source SHA7aa4b1f51314a22a93aafcbaa059ff3e20e5637e296ea4495f69ac35bc67baad;
prompt SHA616f71f2e5409d68b85a8a3fa244ffce4216e55c8e828dc5160909541236f368;
schema SHA59b0bd59fe7d2610b0d05002f37adc1ccf668ecd248e3fa77b28a6bb92e8fafd.
Typed query_logs_summary works; full710passed2live skips,focused49+28,toolreview
clean. Live smoke on existing dev06:9logs2useragent groups,1GET0newruns0modelcalls.
Fresh briefing fetched via evidenced idempotent oldstart,SAMErun confirmed.
Neutral CORE reminder revalidates cross-version experience; old corpus unchanged.
New harness retains latest public overview/evaluation and actualpublic starttuple.
Current-capsule wrapper smoke passed including boundary scoring/failure/cancellation.
Root frozen incremental review clean db6d7ede584a80217853b358cbd09d5b04ec70b5da8f7cec48b3c54bcf139ff0.
All subagents idle/stopped; observation_history interrupted after completed draft
so root finalized sourcefreeze. No commit/push. Preserve allpreexistingdirtywork.

## Current public checkpoints and next hypothesis

This goal turn made progress (new retained accounting and evidence), with a
verified live wait on session66664. No live inputs or frozen source were changed.
checkpoint-40.json:40successful logicalcalls,0modelerrors; latestoverview at35
observed29292.402913059s (4.843%week),downtime13.113323016s,uptime.99955233,
virtualcost257005468minor (2.570mRUB),upperboundscore100 (notfinal).
Currentrate projection43.656mRUB ignoresfutureactions/load/billinggranularity.
First26calls:805413input+9035output;489216cachedincludedininput;282.905s
modeltime; providermoneyunavailable. Do not mistakevirtualspendforbilling.

Fixedpublicprefix1–16 audit at prefix-audit-01 is assigned to mechanism_audit.
Root fixedprefix1–40 at prefix-audit-40/ and root-observation.md:catalogread26
was used27,then absent fromactualrequest38(history31–36,latest37,recalledcurrent37
plusolder7). Agentreadcatalogagain38. This is a specific contextavailabilitygap,
notyetcausalprooforjustificationfornewproductioncode. Complete-recordoption only
expandsalreadyselectedIDs; itcannotrecoverevictedcatalog. Revisit afterfullrun.
Earlytwo-codestopfilterwidens toall6codes by31/36;300snullstopswhileoperation
pendingareallowedbycurrentpolicy andarenotblindfullhorizonwaits.

## Verified memory chain

Artifact root: artifacts/persistent-recall-2026-09-05/.

- Exact 20 training records (12 transitions, 8 outcomes) match SQLite byte for byte.
- Four known situations retrieve the relevant episode at rank 1. Three items are
  selected, with exact-code P@3 = 1/3. Novel situations return analogies, not direct
  evidence. No generic incident-code equality filter was added.
- relevance-questions/run-dev-02: 5/5 guided answers correct, including two failed
  actions despite eventual successful run outcomes, and novel-case abstention.
  Independent review passed. Original run-dev-01 retains five authentication
  failures and zero generation requests. Plan 02 preregistered network recovery.
- non-sre-raw/pair-dev-01: ordinary agent recovered 4/4 with experience, 2/4 without.
  Eight calls, no errors or retries. Actual paired requests differ ONLY in
  memory_context.items. Negative same-code evidence helped cases 103/104 choose
  ivory. Independent review passed. This is a designed training family with
  one-decision tasks, not a holdout or proof of SRE utility.
- Successful continuation episodes exist at initial ranks 9–11. After a public
  failed observation, the unchanged retriever promotes them to rank 1. Since
  negative evidence already helps decisions, no ranking change is justified.
- Small-pair input tokens: 58,283 vs 66,942; output: 729 vs 765; adapter seconds:
  34.780 vs 36.884. Provider money is null. Cached tokens are included in input.

## Closed SRE pair

pair-dev-01/empty_history, run nlJuWjJ6PanT0A0678G6wb9z: 160 steps in 2059.497s,
37.03% of the week, uptime 0.9732792083, SLO null, virtual cost 166,793,418.48 RUB.
The decision cap bound first. frozen_history, run 16vqjJlBHhAvEW6TLNWH1dZP:
139 steps in 1861.873s, full week, uptime 0.2960334102, SLO false, virtual cost
624,511,129.03 RUB. Both independent integrity reviews passed. Different horizons
prevent a matched whole-week cost comparison. Memory utility was not established.

I136 used a narrow error filter without evidence excluding other error types.
No causal memory attribution or hidden-world diagnosis is justified. The owner's
90–100 minute reference concerns other teams' wall time; a later 120-minute budget
is an option, currently deferred by the memory-first priority.

## Current code increment

artifacts/observation-completion-2026-09-06/ retains before snapshots and review.
ObservationHistory(max_complete_record_bytes=2000) optionally stores bounded,
complete redacted strings beside baseline 1000-byte views. Snapshots use spare
8000-byte budget, newest first, preserving baseline IDs and order. Limits remain
configurable. No raw ToolResult cache is retained. Defaults are unchanged.

Now wired into AgentRunner via optional constructor argument
observation_complete_record_bytes=None (default) or2000 (experiment). No CLI flag.
An initial AgentConfig flag violated the
historical schema contract and was removed; config/execute were restored exactly.
Validators remain unchanged. Incremental patch SHA:
009ee82004a24bc3256723fcf4135c3b8229e1a473bfe4952d732da73f136b1a
Only observations.py and tests/test_observation_completion.py are in this increment.
Class increment quick review: CLEAN. Runner integration incremental patch
SHA ed550f80c2fed84db49ae191a1c1f21256179634b505f166464ee56c778a56d7
(execute.py + test_decision_continuity.py) also reviewed CLEAN.
Latest full suite:705 passed,2 existing opt-in live skips; focused+architecture33.
Fresh history is created per run; invalid options fail before environment.start.

The 62-step public-prefix replay matches the earlier prototype exactly. Mean
extra context is 822.74 bytes. At I34 the same six records grow from 6617 to 7559
bytes, restoring complete I20 and I31 evidence.

## Completed I34 decision probe

Folder: artifacts/observation-completion-2026-09-06/decision-probe/.
Frozen plan-01 SHA: 553ffc56b6cde369c4a8ae025e51978f1b6a57c6299be051d05073ed64899f5b
Runner: b8fe3167392c7dc4f554e6d91b84238c08d234230296fdd04466f4be12da78cc
Source tree: f9cbf8d62f5204a178380d65ac5df7440f5577e4c7e744aef27d5d3845aea4dd

The original I34 request is preserved; only observation_history changes. History
uses public I1–I33, never the original I34 result. ABBA order, four completed
logical/adapter calls, zero retries, zero executed environment actions.
Baseline repeats the same targeted query_logs 2/2; expanded repeats the same
8626-second advance with six error stops 2/2. The narrow descriptive consistency
criterion is met. Full-task benefit is unknown.

Baseline already mentions the code/available=6 in previous model reasoning. The
treatment restores direct provenance/detail in TWO records. The baseline query
extends the old window by 33.5s, so it is not inherently wrong or redundant.
Input tokens: 56,006 vs 56,402; output: 1,294 vs 1,000. Provider cost is null.
root-assessment.json and independent-decision-review are saved; review passed.
Canonical cached-token total is 40,448 (agent final message had a typo). Do not claim an SLO or economic improvement.

## Next concrete action

Monitor live session66664 and retained public traces, without injecting root
analysis into the model. Check approximately every40decisions. Save actualtoken/
retry/time totals and public final score/uptime/cost/horizon. Provider money stays
null if unavailable; virtualRUB is separate. Do not mutate capsule,plan orcorpus.
Review all results/provenance after completion and retain failures.
New public northstar: wholehorizon,uptime>=.99,cost<50millionRUB (5billionminor),
score100. Bands[50,150)million75,[150,500)million50,>=500million0;incompleteor
uptime<.99scores0. Runningmaxscore ignoresfuturecost; notafinalscore.
Oldrecords are earlier-version untrustedexperience,notcurrentworldlaws. Same seed
acrossversions is notmatchedworld. This is one development run,notmemoryablation.
Repeatedfullsuccesses andmatchedmemoryutility remain unmetGoal criteria.

## Earlier state and detailed evidence

Branch codex/vadim-agent-memory, HEAD ae9cb6952df54d4a47a30c2d9e09695dc13767b6,
ahead 5. Earlier raw recall schema 1.4 opt-in admits finalized failed/interrupted
runs with bounded public outcome metrics. Run finalization is not world success.
Default canonical fixtures remain intact. Policy 1.3 handles operation freshness;
ObservationArchive remains experimental. Previous suite: 695 passed, 2 skipped.

[Results](PERSISTENT_RECALL_RESULTS.md) and
[archived handoff](HANDOFF_AFTER_MEMORY_CHAIN_2026-09-06.md)
retain all prior pins, attempts, costs and rejected hypotheses.
