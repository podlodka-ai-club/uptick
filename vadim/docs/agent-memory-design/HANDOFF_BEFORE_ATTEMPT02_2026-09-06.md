# Active memory goal — handoff

Goal ACTIVE. Previous turn and this turn made progress and verified a live wait;
no genuine blocker. Edit only vadim/. Preserve all existing dirty work. No commit
or push. Branch codex/vadim-agent-memory; HEAD ae9cb6952df54d4a47a30c2d9e09695dc13767b6.
Goal criteria remain full successful SRE replications, honest matched memory
comparison with frozen train/eval boundaries, and non-SRE validation. Microprobes
alone do not complete it. User permits up to8 full attempts, current=1, leaves7;
per attempt480 decisions/7200 wallseconds/120s logicalcall. Terra/medium via Codex
subscription, unset OPENAI_API_KEY/CODEX_API_KEY. No repeated approval needed.

## Full attempt 1 TERMINAL — do not restart

Session66664 confirmed exit0; ended2026-09-05T23:16:53.807067Z. Run
cFT5xFNHnrc7tc9kYK4xVH0M stopped at480steps, simulator stillrunning,33.323970%week,
uptime91.172965%,spend28,013,376.23RUB; no finalscore, maxachievable0.
Terminal-verification-01.json structuralchecks clean,960memoryitems,corpussame.
481providerrequests incl1repair,480logicalcalls;15,620,612input202,203output,
11,088,896cached INCLUDED;6684.597wallseconds. Full-attempt-01-closeout.md.
All current artifacts below relative to artifacts/simulator-refresh-2026-09-06/.
Run files: full-sre/run-dev-01/frozen_history/{provider-events.jsonl,outcome.json,
current-memory.sqlite3,historical-corpus.sqlite3,seed-42/trace.jsonl}.
Frozen full plan full-sre/plan-01.json SHA
6012d9f830d47780381370bd8e267adb5c8138701d15f1579b274c29a34c2816.
Source capsule hash7aa4b1f51314a22a93aafcbaa059ff3e20e5637e296ea4495f69ac35bc67baad.
Do not mutate capsule, plan, current inputs or old312-record historical corpus.
Seed42, API0.7.1 publicv13, actual new startup verified, complete history2000 bytes,
base1000/global8000/max24; current-run memory writable separately. No oracle,
future rows, hidden simulator state, evaluator-only labels or curated answers.

## Immediate next actions

1. Probe session94388 confirmedexit0. All8complete,0retry. outcome-accounting.json
   and results.md plus independent-outcome-review/. SREbaseline2catalogreads,
   filtered2creates; nonSREbaseline2route,filtered1route1boundedstatusreread.
   SREtokens62857vs62925,modelseconds27.823vs37.819. Filteredfirstanswer wrongly
   claims keepingextra backendfitsbudget: publicprojection8.457bminor>5b.
   Gate NOTcleared; do not implement predecessorfilter/cap2. Top-levelpinsallmatch;
   nonSREnestedmanifest has stale evaluatorhash, directfrozenplanhashcorrect.
2. mechanism_audit implementing only v2_policy.py/tests policyfix: valid cached
   same-run public irrecoverableSLOproof survives interveningget_operation.
   Strictclock/horizon/units/newercontradictionsfailclosed. This fixes terminal
   spin, not I461uptimeloss; oldfrozencapsule unchanged. Waitreport/test/review.
3. observation_history preparing corpus-dev-02/: exactwhole union old312rawrows
   +finalizedattempt1currentstore, no curatorfilter/payloadchanges. Validate
   originalrunner corpuschecks, sourcehashes, types/outcomes, no leaks.
4. Root prepared full-sre/experiment-02-draft.md, notfrozen/notdispatched:
   newrawhistory +separateterminalguardcorrection, otherwise sameconfig/model/
   tools/budgets. Notcausalmemorycomparison/holdout. Need corpusvisibilityprobe,
   finishedcodetests/fullsuite,boundedreview,newsourcecapsule/newcontract/plan,
   actualpreflight before fullattempt2. No fullrunorproviderprocess currentlylive.

Probe prepared/reviewed: byte-exact SRE baseline, only memory_context differs;
actual8request/schema/source preflight passed. Fake-client lifecycle and actual
loader/dispatch/liveguard/failure-stop/output-exclusion smokes passed,0externalcalls.
Non-SRE v1 was invalid and retained; v2 recordsboth identicalactions, givingempty
history. Baseline recalls predecessor.observation, filtered older.result; BOTH
contain the route. It checks source-placement/actionstability, not general no-loss.
Autoreview ar-memory-selection-20260905T222404Z: pass1 low contractoverclaim fixed,
pass2 targeted clean at22:30:08Z. review.json/review-pass2.json and review-before/.
No production retrieval change. Tests elsewhere unchanged:710passed,2live skips.

## Evidence that determines later choices

- retrieval-redundancy-factorial-38/: baseline cap1 selects I37+old7. Filtering
  immediate predecessor AND cap2 selects I25+I26catalog (5250bytes vs6157 baseline).
  Either factor alone missescatalog. Root post-score filtering retains allstored
  rows/scores and reproduces result. Exact contexts in candidate-filter.json.
  I26 is STILL fragmented between resultprefix and query_match, not completeJSON.
  Both tested cap2 contexts displace historical items. Availability != utility.
- loop-prefix-155/: some exact counterevidence lost at I138; I150 remembers that
  expansion helped and deletes anyway forbudget. Wholecycle is not onlyforgetting.
  Laterpublic I304+ introduces IP/UA firewallrules;I314 generalizes US+GenericBot;
  add/delete resumes. Clientlegitimacy/attackcausality not established bylabels.
- cost-learning-prefix-280/: agent includes accruedcost inforwardprojection, but
  recalled adjacent-cost deltas only+4/+3/+2minor, not wholecycles. Repeatedobserved
  ~25.7mMINOR changes=257kRUB; no provenrounding/minimum/deletefee. Root interpretation
  suggests possible generic source-linked objective-measurement history, not
  causal attribution to lastaction. NOT implemented/preregistered.
- objective-history-footprint-340.json:69 measurements,8genericnormalizedmetrics;
  last3snapshots median1697/max1711UTF8bytes. Feasibility only, no modeluseproof.
- log-sampling-prefix-340.json/md: all3 summarycalls filteredcapacityerrors and
  returned1log each; all35targetedqueries error-filtered. Threeincrementallogreads
  wereunfiltered, so don'tclaim allsuccesslogsabsent. Failure-conditioned samples
  don't establish wholetrafficfrequencies or whichrequestcausedcontention.
- advance-progress-prefix-340.json:102successfuladvances,38earlystops,5<1s.
  policy-rewrites-prefix-340.json:103proposedadvances,36nullstops,0rewrites.
  OfficialOpenAPI new_log_errors const1; no arbitrarythresholdcapbug. Promptpolicy
  influence untested; don't weaken guards just to finish a run.
- reasoning-memory-boundary.json: rawepisodes omit modelhypotheses/plans; those
  remain auditdata and one-step previous_decision. Channel separation, not provenbug.

Consolidation-readiness/audit.md: currentvalidator cannot promote failed/interrupted
runs to active lessons; rawschema1.4episodicrecall is available. Derivedconsolidation
requires explicitimmutable snapshot/dryrun/exactapply, notautomaticlearningcycle.

snapshot_progress.py is offline, tracks latest_public_measurement fromoverview or
get_metrics; oldfields unchanged inI240 replay. checkpoint-300.json actuallyI294.
Detailed prior handoff archived in docs/agent-memory-design/
HANDOFF_BEFORE_TERMINAL_SIM071_2026-09-06.md and HANDOFF_DURING_SIM071_2026-09-06.md.
ACTIVE_GOAL.md and00_NORTH_STAR.md remain normative. Old smallmemory tests prove
only designed-family utility, not full SRE benefit; old paired fullruns failed.
