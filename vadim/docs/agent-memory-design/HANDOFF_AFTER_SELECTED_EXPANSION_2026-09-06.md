# Active memory goal — attempt 2 and ledger probes closed

Goal ACTIVE. Full repeated success and matched memory evaluation remain unmet.
User allows up to 8 full attempts: 2 started, 6 remain. Each 480 decisions,
7200 wall seconds, 120 seconds/logical call, Terra/medium through existing Codex
subscription. No API keys, repeat permission, hidden oracle, curated correct
answers, or changing live inputs. Work only under vadim/. Preserve dirty work.
Branch codex/vadim-agent-memory, HEAD ae9cb6952df54d4a47a30c2d9e09695dc13767b6.
No commit/push. Prior detail is archived in HANDOFF_ATTEMPT02_PREFIX320_2026-09-06.md
and HANDOFF_BEFORE_ATTEMPT02_2026-09-06.md under docs/agent-memory-design/.
All paths below are relative to artifacts/simulator-refresh-2026-09-06/.

## TERMINAL: do not restart attempt 2

Root exec session **78574**, run **0Kpi0djTswM4HDotyW7aYw8h**.
Started 2026-09-05T23:46:27.625275Z; deadline ~2026-09-06T01:46:27Z.
Exec78574 confirmed exit1. Outcome timed_out at 2026-09-06T01:46:29.568654Z.
398 completed steps,399 logical requests. terminal-verification-02.json retained.
Wall7201.943s including cleanup exceeds declared7200s; not budget success.
Latest public overview I394 observed149978.068s (24.797%week), uptime99.461423%,
cost20,046,279.97RUB, still running/degraded. No full success.
798 memory envelopes checked structurally; no violations, corpus unchanged.
Known telemetry398 successful calls:12,875,921input+169,374output=13,045,295total;
9,108,736cached INCLUDEDinput. Final cancelled call lacks token usage.
Ledger exec15785 EXIT1 after first acquisition: bound state exceeds2048B.
Raw model output and summary preserved, no actor calls, no retries/replacements.
Candidate gate unmet; semantic-assessment.md also notes incomplete source refs.
Output full-sre/run-dev-02/frozen_history/{outcome.json,provider-events.jsonl,
current-memory.sqlite3,historical-corpus.sqlite3,seed-42/trace.jsonl}.

Pinned full-sre/plan-02.json SHA
c63d0209e6cce61d46d14fcdea832ee1906679dde509d1850ec04b64bc9a22f4.
full-sre/run_full_sre.py SHA
 eb235921b0cb4ac4ef6a820d3d20a2adc3a91eae1e07b3662c65ace2f3262c44.
Source full-sre/source-capsule-02 tree
990d1d86029e16c2a2b1c4d7a92bbd763af256a9281a5d317777fc417c2c836a.
API0.7.1/publicv13/cost-bands.v1, seed42. Actual startup/schema/model verified in
full-sre/dispatch-verification-02.json. No model/prompt/selection/history changes.

Attempt2 changes: append ALL481 finalized attempt1 raw records to old312 corpus;
separate policy1.4 terminal cached-SLO-evidence correction. Two changed factors,
same-seed development, NOT heldout or causal memory comparison. Raw config
fingerprint02c5956fcf9e2e591dea35cae415a4b7a99d41d999a5405087b2faa77aa9a1db;
diversity cap1; history1000base/2000complete/8000global/24records.
Corpus793=756transitions+37outcomes,25namespaces, SHA
3fcbeb4c6f4d28adb776b77acf07c2a68c9c8544c53d40b710ba63d104ba9c93.
Exact union validated; attempt1 outcome stays interrupted, never relabeled success.

## Checkpoints and next action

Retained checkpoint-02-{040,080,120,160,200,240,280,320}.json.
I320 latest measurement: observed126566.304s (20.926968%week), uptime
.9940598695396711, downtime751.820360587s, cost1619123627minor=16,191,236.27RUB,
2backends, hourly51400695minor. 320results,0providererrors. Current-rate projection
84.473304mRUB is conditional, not a forecast/bound. I280 had1backend and projection
49.555781m; that earlier projection does not describe current configuration.
I313 public advance300s returned49092events/12025logs; cause not inferred from count.
I280 accounting:9,001,489input111,906output=9,113,395total;6,429,696cached INCLUDED
input;3151.489modelseconds,median10.769s. No provider billing inferred.

Checkpoint360 retained. Attempt ended at398: NO checkpoint400.
Terminal verification finished offline,0external calls. No public completion.

## CLOSED ledger probe: exec15785 exited1, do not retry

strategy-ledger-probe/plan-01.json SHA
f1985b5563536abca96da0b496b30404f485ac94c8fe6d7542b5c893c6310d23.
Runner SHA78369915081cd32d7eae95f7e6abf9cecab23bb37cb1f328de5f9547894f818b.
Loaded frozen plan successfully;19offline tests passed. Probe dispatched after attempt2 terminal.
Failure after1/12 calls: ValueError Working state is over budget.
39,108tokens (38,703input+405output),12.56s,0environmentactions.
A separately declared protocol adjustment requires a fresh plan; no live integration.
Do not rerun command below; it is retained for provenance.
Exact command from vadim, with network escalation if needed (already authorized):
`env -u OPENAI_API_KEY -u CODEX_API_KEY UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --locked --offline python artifacts/simulator-refresh-2026-09-06/strategy-ledger-probe/run_probe.py artifacts/simulator-refresh-2026-09-06/strategy-ledger-probe/plan-01.json`

12logical calls/60seconds each/900total, max24adapter generations, no harness
retries/replacement/actions. Each block:2independent acquisitions then actorABBA
current/ledgerA/ledgerB/current. SRE actual attempt1I132 acquisition/I138 use,
only visible recent/latest refs126–131. NonSREv2 toy actual public trial failure
then25distinct observations; I1 gone from actual actorI27. Designed development,
NOT heldout/generalization. V1 ControlledIncident diagnostic retained as infeasible.
Model writes <=4entries/2048UTF8bytes; bind same-run past refs, sanitize, untrusted.
Actor treatment only inserts working_memory field; preserves prior bytes otherwise.
No authored ledger/correct action. Count acquisition cost. Invalid/unsupported
notes fail gate; tied/mixed toy outcomes inconclusive. Gate and limits are frozen
in experiment-contract.md. One-off acquisition/use, NOT per-turn lifecycle or files.

Autoreview ar-strategy-ledger-20260906T003252Z pass1 scope
sha256:db1d9097e8da8276967f0b5ccb44be3a63e9002c315656750cd1948a3c175604.
START00:32:52.265953Z, END00:42:16.872842Z: findings, sole AR-STRAT-1 rejected by
root as future-only mutation/reload concern; no reachable path in pinned probe.
review-adjudication.json binds original reviewer report hash (reviewer used own id).
0validated blockers, no semantic changes/rerun. Do not re-review unchanged scope.

## Retained results and caveats

Attempt1 closed:480steps/111.410min,33.323970%week,uptime91.172965%,28.013376mRUB,
15.822815mtokens,481provider generations incl1repair. Public running, score null,
maxachievable0. Failure preserved; terminal-verification-01.json structural clean.
I46144831s wait stopped only for selected capacity/DB codes, applied full interval;
downtime jumped. Cause unknown. Later cached metrics lost behind get_operation,
policy1.4 fixes only terminal inability to finish already-SLO-failed run; not I461.
Production policy/tests already reviewed (one pending-state regression fixed),
60focused/722fullpassed+2existinglive-skips; no production edits in current cycle.

Prior memory-selection-probe8calls failed adoption gate: filtered catalog choice
sometimes made false budget claim (~84.574mRUB>50m), no latency/token improvement.
Do not adopt predecessor suppression/cap2. NonSREv1 invalid, v2 correct, both kept.
Nested evaluator hash caveat retained; actual direct frozen pins matched.

New audits: recall-exposure-attempt2-prefix160.json — attempt1 shown120/160requests,
not proof of use/utility. policy-action-parity-attempt2-prefix280-v2.json — exactly
1normalized host rewriteI51 nullstop→firsterrorstop,2655requested/15.337applied.
Initial artifact's34raw differences include33wire aliases; NEVER claim34rewrites.
plan-02-metadata-erratum.json — nested descriptive counts/version stale312/0.5.0;
operative top-level pinned actual corpus793 verified. Runner ignores nested block.
Do not edit frozen live plan. Future plans derive counts and mixed source versions.

Summary-use audit under full-sre/run-dev-02/frozen_history/summary-use-attempt2-prefix160:
I42/144/145/160 error-conditioned samples; I144→145 useful IP/UA comparison but not
traffic frequencies/attacker labels. I146 avoids automatic deny. /root/observation_history
now auditing only new2summary calls in suffix281..320, no future data/calls.

General-loop-memory-followup/ has report and integration-sketch.md (403words,
SHAa106976153920beab06bc47dff665ebb4532d289538f0fb1561aad5bd3ec183d).
Opt-in generic same-call envelope, inner environment-owned decision, default exact
path; NO implementation unless probe positive. Existing architecture has no such envelope.
context-footprint-attempt2-prefix280/report.md+details.json: fixed developer38827B,
schema20874B; late runtime23–27KB. Ledger adds2KB (only~2% measured total), does not
prove safe removal of any field. Bytes are not tokens. Duplicate projections exist,
not a license to remove raw evidence. Footprint reportSHA720e080635aae1aa4384e083923938147743c6902b1c825d3d3d7318146e061d.
remaining-attempt-sequencing.json is conditional, not dispatch: up to2dev+4paired
fresh-seed cold/warm attempts if ready; no eval seeds declared, no blind repeats.

Latest checkpoint360:24.490184%week,360results0providererrors; I359metrics uptime
.994794274548994,downtime771.054531122s,observed148116.634s,cost1953227650minor
(19,532,276.50RUB),1backend,conditionalcurrentrateprojection52.135276mRUB.
common-horizon-prefix-02-336.json: new18.247mRUB at139662s lies betweenold17.733m
at136700s and19.018m at144266s; no interpolation/causal savingclaim.
advance-progress-attempt2-prefix320.json:85advances,35earlystops,22<10%requested,
2<1second. Raw counts do not prove safety guards unnecessary.
Summary suffix281..320 audit complete:2newsummarycallsI285/I286 beforeI313volume
jump, each1error sample; no summary afterI313 withinprefix320. Power+firewall changed
together, subsequent improvement cannot isolatefirewallcausality. Artifacts inold
summary-use-attempt2-prefix160/*suffix281-320* andanalysis-suffix281-320.md.
If frozenledgerprobe fails, a possible next diagnosis (NOTacceptedplan/feature):
raw recall exposure is proven, but512-byte resultprefixes can omit actualfacts.
Prior cap2/filter negativeprobe did not supplycompletecatalogevidence. Consider
separately measuring fixed-selection complete-evidence formatting under declared
budget, with generic rules and no answer-orientedfieldselection. Do notblindly
repeat old rejected selection package or implement before evidence.

## Current next action after closed ledger v1

First acquisition raw1674B+binding642B=2316B; hard2048 rejection.
Budget diagnosis and semantic assessment retained in strategy-ledger-probe/run-dev-01/.
Separate candidate strategy-ledger-probe-v2: rawupdate1280B localvalidator with
existing one adapterrepair, exact numeric prompt; final2048cap unchanged. All
oldpins/actorinputs/evidence/schema/model byte-identical. Eighttests/preflightpass.
Review running /root/mechanism_audit; IDar-ledger-budget-20260906T015800Z,
scope064baa41bea2cdff3d8be630d90d48933ecff414b5e296363f5ea8a2da2b2e3f.
No calls yet. Aftercleanreview freeze draft then dispatch12calls once. No further
budget/wording rerun if gate fails/ambiguous. No productionintegrationyet.
Observation_history independently auditing recall payload at fixed9checkpoints.
Transport closeout confirms13genericHTTP_ERROR+4CONCURRENT+4stale404.
Terminal rootTimeoutError/CancelledError; no retained proof of TransportClosedcause.

V2 review CLEAN, frozenplanSHA
0f74feb2929848ba2873ea80f57817e0b6daebc96e8018dcc6c374f274e22dd1.
DISPATCHED exec51365 around2026-09-06T02:05Z; do not duplicate.
Outputs strategy-ledger-probe-v2/run-dev-01/. Poll withboundedwaits, then assess
bothwriteroutputsandallactorarms peroriginalsemanticgate.12logicalmax/24gens,
60sec/call/900total. ReviewmarkerEND02:04:59.815829Z.
Reviewer report material_semantic_delta=true describes candidate vs predecessor;
rootEND=false correctly records no changes CAUSED BY review. No findings.

V2 exec51365 EXIT0,12/12calls completed,13adaptergenerations incl1repair.
340,014input+3,801output=343,815tokens;131,584cached INCLUDEDinput;
124.961modelseconds,125.340wallseconds,0actions. Rawinvalidrepairnotavailable.
Toy:bothwritersgrounded; currentAobserve(unresolved),ledgerAviolet(unrefuted),
ledgerBobserve(unresolved),currentBamber(refuted). Mixed1/2ledger effect;
PREREGISTERED GATE NOT MET. No productionintegration or furtherwording/budget
reruns. SREsemanticreview stillrunning /root/file_handoff_research, complete
before finalcloseout; no outcome can override failedtoy gate.
Next mechanismhypothesis: fixedselectedrawmemory partialpayload at9checkpoints.
recall-payload-checkpoints/report.md:18items,15partialdata,2data-completebuttail
omitted,1untruncated; noentiredatahidden; allquery_matchpresent. Formatter
result512chars +querymatch; same2items1current1historical/checkpoint.
Consider generic bounded continuation of EXACTsame selected result funded only
fromsparebudget. Must compare priorrejectedcap2/predecessorprobe; no selection
changes/domainfields/correctanswer. No new experimentplan or productionedit yet.

Final SREreview alsoFAIL: bothwriterrefsomit131(actualcapacitynumberssource);
writerAunsupportedlegitimacy. ledgerAapplicationuncertain,ledgerBpass.
sre-semantic-review.mdSHA924f94ef5594c7846dc2858752076cef38283f7cfc2ffab9e78b9ea2d5d02476.
Rawcontinuationprototypev1offline:11751extraASCIIcanonicalchars,3/18complete;
all9within8000globalestimatedbytes. ROOT FOUND VALIDATION GAPS: hardcoded
source_identity_verified and no explicit module/pertypebudget assertions.
Agentobservation_historyproducingseparatecorrection-v2 withhardchecks; do not
acceptv1asprovenall-boundary-safe. Oldartifacts retained. Corememory config
episodic.max_context_tokens=8000, pertype{} confirmed byrootoperativeconfig.
mechanism_auditdesigningone minimalopt-in integrationpath, no implementation.
No model/SREprocess live. Fullattempt3 NOT prepared/dispatched yet.
