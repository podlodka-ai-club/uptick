# Active memory goal — current handoff

## Scope and authorization

Goal ACTIVE, not complete or blocked. Edit only vadim/; preserve unrelated dirty
work. Internal model gpt-5.6-terra / medium through existing Codex subscription;
unset OPENAI_API_KEY and CODEX_API_KEY. No oracle, future events, hidden-world
state, evaluator-only labels or answer-selected corpus enters the agent.

Owner now permits up to8full attempts including the current one (1of8;7left),
with480decisions/7200wallseconds/120seconds perlogicaldecision each. No repeat
permission needed. Each next attempt requires a distinguishing hypothesis,
preregistered replication or memory-comparison role; no blind repeats. See
artifacts/simulator-refresh-2026-09-06/attempt-budget-authorization.json.

## LIVE: first refreshed full attempt

Root exec session **66664**, confirmed live via write_stdin. Poll this handle;
never restart solely on a wait timeout. Run **cFT5xFNHnrc7tc9kYK4xVH0M**, started
2026-09-05T21:25:29Z. Do not mutate its frozen inputs or inject root analysis.
Output: artifacts/simulator-refresh-2026-09-06/full-sre/run-dev-01/frozen_history/.
Plan: same full-sre/plan-01.json, SHA
6012d9f830d47780381370bd8e267adb5c8138701d15f1579b274c29a34c2816.
This plan pins source, actual new startup/schema, corpus and publicAPI identity.

Single development run: seed42,Terra/medium,same312old public raw episodes,
separate writable same-run memory, complete history2000/base1000/global8000/max24.
Do not alter source-capsule/,plan or oldcorpus while live. Prior old full-sre plan
under observation-completion-2026-09-06/ was superseded with0calls; neverlaunchit.
Actual fresh startup matched before firstmodelcall. No live model errors/retries
through 340 decisions. HTTPerrors on environmentsteps41 and54 are retained; model
recovered by reading operation status. Neither error stopped/restarted the run.

## Current evidence

All refresh artifacts: artifacts/simulator-refresh-2026-09-06/.
API0.7.1 public v13 description, actual new commands_markdown and logs/summary
retrieved. Same old run confirmed for idempotent startupfetch:0newruns0modelcalls.
Typed query_logs_summary integrated and live-smoked (9logs2useragentgroups).
Full suite710passed2existinglive skips; focused49+28. Toolreview and rootincrement
review clean. Actual current-capsule wrapper smoke passed, including120sdeadline,
startup tuple retention, final score boundaries, failure/cancellation and isolation.
The harness retains latest public overview/evaluation without extra HTTP calls.

checkpoint-340.json freezes340 logical calls:10,927,363 input+138,961 output;
7,804,160 cached INCLUDED in input;4003.564s modeltime, zero provider errors/retries.
Latest clock I340:21.2007% of week,70.8333% of480decision budget. Newest measurement
I336 overview:uptime99.437004%,cost16,448,238.71 RUB,onebackend. Constant current-rate
total50,496,266.35 RUB; notfinalforecast/lowerbound. Actualfinal SLO/scoreunknown.
New public behavior: I304 begins combined sourceCIDR/UA firewall rules;I307/310/313
singleIP GenericBot rules;I314 generalizes US+GenericBot. Do not call these clients
attack/legitimate from hidden labels. Capacity add/delete resumes317/324/328/333/339.
The agent changes hypotheses, but successful policy learning remains unproven.

snapshot_progress.py now separately tracks latest_public_measurement from overview
OR get_metrics.current, preserving original fields. I240 replay parity saved as
checkpoint-240-measurement.json. checkpoint-300.json was early and actually covers
I294, not300. All earlier snapshots retained; no active runner changes.

## Memory investigations alongside live run

mechanism_audit completed prefix-audit-01/audit.json/md (fixedI1–16):I1 two
cross-run inboxepisodes explicitlyrejected as irrelevant;I2–16each one exact
same-runpriortransition+oneoldhistoricalepisode. All32external_untrusted, incomplete
querymatches. Nooracle fields found. Earlytwo-codestopwatchlist was unsupported,
but no harm/copying causality established; laterI31/36 widened to6codes.
Pending300snullstops are allowed by policy1.3,notblindwholehorizonwaits.

Root prefix-audit-40/root-observation.md:catalogread26used27, then absent from
actualrequest38(history31–36,latest37;recalledcurrent37+old7). Catalogreread38
and52. Specificcontextavailabilitygap,notyetcausalproof. Complete-recordhistory
expandsselectedIDs only andcannotresurrectevictedfacts. No sourcefix made yet.

Completed retrieval-redundancy-38/audit.md/json: exact I38 baseline reproduced
using frozen 312 old rows + current transitions <=37. Remove runtime_policy only
from reconstructed retrieval query (the runner adds it to provider context later).
Baseline selects current I37 + historical I7. Excluding current I37 instead
selects I25 + historical I7; catalog I26 moves from rank3 to rank2 but still loses
the one-item-per-run diversity slot. Excluding all represented I31–37 is the same.
I14 negative control reproduces baseline too. Immediate redundancy alone does
NOT explain/fix the catalog omission. The byte-like token estimator is deliberate
UTF-8 upper-bound counting, not an established bug. No production fix yet.

Factorial completed: retrieval-redundancy-factorial-38/factorial.json and
short-assessment.md. I38 final mixes: cap1/include => I37+old7 (6157 bytes);
cap1/exclude => I25+old7 (5871); cap2/include => I37+I25 (5460);
cap2/exclude => I25+I26 catalog (5250). Both factors required in this replay.
I14 control changes current-item mix with no future I26. This is availability,
not model/task utility. Joint change also removes historical items from these
contexts, so it is not evidence of better cross-run learning. No config adopted.
Root review: exclusion is pre-retrieval row removal, which also changes recency
normalization; it is not yet a test of post-score candidate suppression.

mechanism_audit completed frozen-rows-verification.json: all8 cells reproduce
using saved JSONL, without live SQLite. Its catalog26-check.json inspects the FULL
STORED result, not the selected model context. Root corrected this distinction in
retrieval-redundancy-factorial-38/root-assessment.md.
Root replay_candidate_filter.py keeps all stored rows/scores and suppresses the
current predecessor only before advanced selection: same selected IDs as the
row-removal variants, cap2 gives I25+I26 (5250 bytes). candidate-filter.json saves
actual resulting contexts. I26 capacity/price are visible in result prefix;
type/provisioning in overlapping query_match tail. The record is STILL fragmented,
not complete JSON. Model usefulness is unproven. Suppression may also remove the
transition's preceding observation, so it is a heuristic, not exact deduplication.
Next: preregister a bounded baseline/filter decision probe plus non-SRE negative
control, execute only after live run ends. No production retrieval changes yet.

file_handoff_research completed loop-prefix-155/causal-loop-audit.md/json:
three capacity cycles (create117/delete126/error131; create132/delete138/error143;
create144/delete150/error155). At I138 exact I131 counterevidence is absent from
useful context. At I150 the model explicitly remembers expansion fixed deficit,
yet deletes due budget pressure. Memory loss cannot explain the whole cycle;
restoring evidence is not proven to change its policy. I155 correctly rejects an
old incremental backlog as evidence of current failure and queries the exact
window. All fixed inputs <=155; zero model/network/simulator calls.

observation_history remains interrupted after harness draft; root finalized it.

Completed design-note.md proposes model-selected/source-verified evidence cards
or opaque archive bookmarks, with a targeted I27→I38 + non-SRE test. DESIGN ONLY:
no cards, bookmarks or probe implemented/launched. Test simpler existing retrieval
settings first. Do not run extra model probes alongside current full attempt.

## Next action and completion limits

Monitor live handle and take next checkpoint around 400 decisions. The next
probe is prepared under memory-selection-probe/: plan-DRAFT.json, eight exact
requests (SRE I38 ABBA plus non-SRE v2 ABBA), 60s/call,600s total. Actual CLI
preflight passed. Lifecycle fake-client success/error/timeout/cancellation4cases
passed; actual dispatch smoke checked live-attempt refusal,8successful fakecases,
firstfailure stopping after1, and refusing output overwrite. Zero external calls.
SRE baseline exactly reproduces actual model request; treatment changes only
memory_context. Non-SRE v1 retained but NOT dispatchable; v2 fixes both-steps
history construction and explicit task objective. Its history is empty because
identical second action replaced the first; both recall arms retain the route,
so the hypothesized evidence-loss risk is NOT triggered by this control.

Probe review completed: pass1 accepted one low evaluation-contract overclaim;
pass2 targeted verification clean. Markers emitted for review
ar-memory-selection-20260905T222404Z, start22:24:04.560500Z,
pass1 end22:29:21Z, pass2 start22:29:45.703943Z/end22:30:08Z.
review.json and review-pass2.json retain dispositions; old contract/draft saved
under review-before/. Final frozen memory-selection-probe/plan-01.json SHA
10912ab67e91d57ece3f9496fec181f5bda339a3e2e8337c7c04ecd76881c60b.
Frozen-mode load validated all8 requests, schemas and source pins. No model call.

NEXT DISPATCH (after session66664 terminal, not a polling timeout):
env -u OPENAI_API_KEY -u CODEX_API_KEY UV_CACHE_DIR=/private/tmp/uptick-uv-cache
uv run --locked --offline python artifacts/simulator-refresh-2026-09-06/memory-selection-probe/run_probe.py
artifacts/simulator-refresh-2026-09-06/memory-selection-probe/plan-01.json
(single shell command, positional plan; no --preflight for frozenplan.)
Requires network escalation for the already authorized Codex subscription.
Harness also requires retained terminal fullrun outcome/ended_at. It will create
memory-selection-probe/run-dev-01 exclusively; never replace failed outputs.
All subagents now completed/idle, observation_history still interrupted.

Keep current attempt intact. Afterterminalstate, verify fullhorizon,score,uptime,
cost,allcalls/retries,corpusunchanged andpublicprovenance; retainfailureasdata.
Success: wholeweek,uptime>=.99,RUBcost<5billionminor,publicscore100. Lowercostbands
[50,150)millionRUB=75,[150,500)=50,>=500=0;incomplete/uptime<.99=0.
Runningmax_achievable_score ignoresfuturecost/failures andisnotfinalscore.
Oldcorpus retains0.5-era objective asuntrustedhistory; no backfill/newworldclaims.
Same seed acrossversions is notmatchedworld; publicAPI lacksimmutableworldhash.

Earlier smallmemorychain is proven in a designedfamily:exact20rows,knownrank1,
5/5guidedanswersinclfailure/abstention,ordinaryagent4/4withhistoryvs2/4empty.
This is notholdoutorfullSREutility. Prior fullSREpairfailed; one arm incomplete,
otherfullweekuptime29.6%. Repeatedfullsuccesses andhonestmatchedmemoryevaluation
remain unmetGoal criteria. No newcommit/push;branchcodex/vadim-agent-memory,
HEADae9cb6952df54d4a47a30c2d9e09695dc13767b6 pluspreexistingdirtychanges.

Detailed earlier state: docs/agent-memory-design/HANDOFF_DURING_SIM071_2026-09-06.md;
goal: docs/agent-memory-design/ACTIVE_GOAL.md; results: PERSISTENT_RECALL_RESULTS.md.
This goal turn: retained I200/I240 checkpoints; prepared, corrected, tested and
reviewed the frozen8call decision probe; repeatedly verified live session66664.
Progress plus verified wait, not blocked. No new production source changes.

Completed cost-learning-prefix-280/audit.md/json: agent DOES use accrued cost plus
future hourly rate; repeated observed cost changes around short cycles are ~25.7m
MINOR units =257k RUB. No proven rounding/minimum/delete fee. Selected rawmemory
only3 same-run cost deltas (+4/+3/+2minor) among279same-run recalls; nocycle-spanning
comparison. root-interpretation.md records units, confounds, and a possible future
bounded objective-measurement history with iteration/provenance, not single-action
causality. NOT implemented/preregistered; finish existingrun+probe beforechangingfocus.
reasoning-memory-boundary.json verifies active contracts/assembler/runner match
working files: modelhypotheses/plans live in audit and1-step previousdecision, not
raw episodic transitions. Channel separation, not automatically a bug.

verify_terminal.py is a read-only first-attempt closeout helper. It currently
returns not_terminal; after handle terminates, it writes terminal-verification-01.json
without environment/providercalls, checking finaloverview, horizon,score,uptime,cost,
callpairing/telemetry, recalledsourceIDs/current-episode timing/trust, corpus hash.
Structural checks are not an exhaustive semantic oracle audit. Never restart an
unfinished run due a waittimeout. Current goal turn: progress + verified livewait.
