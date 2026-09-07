# Active memory work — compact handoff

## Goal and latest owner priorities

Persistent Goal is ACTIVE, no global token cap. Completion contract:
[ACTIVE_GOAL.md](ACTIVE_GOAL.md). It is not complete.
Only edit `vadim/`; preserve other dirty work. No oracle/evaluator mappings,
future observations or answer-selected data may enter agent/memory requests.
Internal decision model: **gpt-5.6-terra / medium**, explicitly chosen by owner.

Owner's latest correction: first prove exact event persistence → relevant fact
retrieval → correct model use/abstention on a small task. Tune SRE economics
later. The frozen pair has finished unchanged; its full-success criterion was not met. Do not immediately launch
another large SRE run. Other teams reportedly take90–100 wall minutes for the
week; a later full-task budget may be120 minutes with more decisions. This is
budget calibration, not a verified matched baseline or a cure for prior downtime.
Keep virtual simulator RUB separate from actual provider/subscription charges.

## Live process — do not duplicate

**Exec session34113 CLOSED, exit0. Do not poll or restart.**
**No live root experiment. Session40054 CLOSED exit0: four I34decisions complete.** Session37410 CLOSED exit0; eightcell raw4/4vs empty2/4.
Full SRE frozen-arm independentreview passed. Fivequestion independentreview passed.
Five-question sessions67524/87117 are CLOSED, exit1/exit0 respectively.
Serial pair `artifacts/persistent-recall-2026-09-05/pair-dev-01/`:
- `empty_history` finished: run `nlJuWjJ6PanT0A0678G6wb9z`,160steps,
  2059.497s (34.3min),223961.276/604800simseconds (37.03%),uptime.9732792083,
  SLO=null,worldstatusrunning. Stop=maxsteps, notwall. Cost16679341848minorRUB.
  4503545input/3134720cached/64311outputtokens,160calls,0retries/providererrors.
  One refused prematurefinish atI160. Independent final verification passed.
- `frozen_history` completed: run `16vqjJlBHhAvEW6TLNWH1dZP`,139steps,1861.873s,
  full604800simseconds,uptime.2960334102,SLO=false,cost62451112903minorRUB.
  Full frozen-arm/pair integrity review passed; canonical artifacts retained.
  Different observed horizons prevent a matched whole-week cost comparison.

Root owns the probe process. Do not restart either SRE arm, resume the capped
first run or change frozen inputs. Pair utility is not established.

## Frozen pair bindings

Root artifact directory: `artifacts/persistent-recall-2026-09-05/`.
- `pair-plan-01.json`:a9b3d621433f0848b304d075a0b6e1eaa8c9d534d39500e139291fa604be8715
- `run_pair.py`:08d8d44388a04b0f258c701500e6390f506df188213d99f8c24b2d8133e68a1d
- `source-raw-recall/` tree:e25da72bee3ffe1427d061c435da723e601a694a2cd705ec44bce3cf20665f94
- Configuration artifact `raw-config-preflight-with-outcomes/raw-memory-configuration.json`:
  d55757c67e1f46a5ef818409dc3b645e0742ddaa9d75e12d9aeee992453be270
  resolved fingerprint02c5956fcf9e2e591dea35cae415a4b7a99d41d999a5405087b2faa77aa9a1db.
Load configuration verbatim. Corpus312records=276episodes+36outcomes, immutable.
Only intervention is historical corpus0 vs312; same-run episodic/history remain
identical. **Original harness-contract.json is wrong**; plan binds the preserved
`harness-contract-erratum.json`, which correctly varies past-run corpus only.
Both arms160decisions/3600seconds. Full success requires public completed status,
604800observedseconds,uptime>=.99,SLOtrue; execution completion alone is insufficient.
Both fail → utility not established. Seed42/corpus are development, not holdout.

User explicitly approved this exact external run; earlier auto-review refusals
are resolved. Do not ask again. Launch evidence: `launch-authorized-01.json`.
Use `env -u OPENAI_API_KEY -u CODEX_API_KEY` for subscription calls. Local commands:
`UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --locked --offline ...` in vadim.

## Small memory-chain work — next priority

Under `non-sre-raw/`:
- Exact20trainingrows (12transitions+8outcomes) independently read back against
  SQLite; root verification `root-exact-corpus-verification.json`.
- `memory-chain-relevance.json/md`:4knownsituations have relevant rank1; P@3=1/3.
  Novel codez6w returns3other-code candidates. They retain provenance/untrusted
  status, but no explicit unsupported flag. Storage and known top1 pass;
  overall relevance/abstention unresolved. Analogy is not exact-case support;
  do not hardcode incident-code equality into generic retrieval.
- **5-question guided model-readback probe completed5/5 under plan-02**.
  Original plan01/run-dev-01 failed subscription verification before generation
  (5logicalcalls/0adapterrequests); retained as unavailable. Network-enabled
  plan02/run-dev-02 used identical inputs and passed. Independent question review passed.
  Folder `non-sre-raw/relevance-questions/`; `plan-01.json` SHA
  b6cd01be5dd0ab9183ef377c947a59ce3d9947e44a144f167da95407a310d3e4.
  `run_questions.py` SHA e6e51c719e3b46e2e4cc0f56207db71982366ff12fe57703e6c9d3052f5b4509.
  Freeze observed at2026-09-05T20:01:59.535359Z. Root final preflight passed5/5
  exact regenerated requests and schema. Outputrun-dev-01 is the retained infrastructure failure.
  Launched after session34113 completed: same subscription/UV command pattern,
  script `.../relevance-questions/run_questions.py .../relevance-questions/plan-01.json`.
  Max5Terra/mediumcalls,60s each/360total, no harness retries/replacements.
  Provider inputonly latest_result+actualmemory_context. Guided question asks
  first direct same-code event: citedID, action.message, that episode's
  result.data.recovered; novelcase must return no evidence. Expectations stay
  evaluator-only. This is guided readback, not normal-agent utility. Earlier
  undispatched negative-control label leakage was fixed; invalid inputs preserved.
- Later-stage8cell full-task harness already prepared/reviewed:
  `run_incident_pair.py` SHA f0f8b2031bd3c8c6d0d56ed1a904e419b1fbb197b88ce39c4019bcbcfc0b9f5d.
  `plan-01.json` completed; SHA
  b36f73b6caaad4bf6543be6621d3d4177f0659a2212b161d2d2524c07da98f6b.
  Offline preflight passed; source/config/inputs unchanged. Draft retained.
  Root fixed arm-label confound: same neutral run ID/namespace per seed, isolated
  files. Independent proof: requests differ only memory_context.items. Final fake
  smoke in `smoke-output-arm-label-fix`; earlier smoke attempts are retained.

## Mechanism findings and current code

Production: raw episodic recall opt-in schema1.4 admits finalized failed/interrupted
as well as completed runs, with exact bounded public outcome metrics. Execution
terminal/completed is **not** world-goal success. Defaults/canonical fixtures
preserved. Generic ObservationHistory bounded1000bytes/record,8000total,24records;
latest full result remains unbounded. File archive is experimental, not CLI-wired.
Latest production checks695passed/2opt-in skips,Ruff clean; no new production
changes in this live-pair phase. Branchcodex/vadim-agent-memory,HEADae9cb695.

The I34 repeated log query was justified: its actual context truncated the error
code/values present in full trace. Audit erratum retained. Offline cap increases
recover fields but evict old records. Separate `replay_history_expansion.py`
prototype preserves every baseline selected ID and uses spare8KB budget to
restore complete small records; I34 6617→7559bytes,meancomplete2.839→4.5. No model
utility or production implementation claimed. Verification/report artifacts retained.
`context-prefix-analysis.md` separates UTF8bytes from provider tokens.
`strategy-budget-diagnosis.md` records persistent deficit vs sequential100-unit
changes; this is a planning-feasibility hypothesis, not a learned world law.

Detailed evidence: [PERSISTENT_RECALL_RESULTS.md](PERSISTENT_RECALL_RESULTS.md).
All earlier context/pins/results: [archived handoff](HANDOFF_DURING_PAIR_2026-09-06.md).
Historical experiments are closed; do not poll their sessions. No new commit/push.

Next: actual8cell independentpairreview passed; next
finish working-memory completeview review and frozenI34decisionprobe.
file_handoff_research independently investigates candidate selection: known r4x/v9n
rank1 historical action failed; distinguish event outcome from eventual run outcome.

## Current working-memory increment

artifacts/observation-completion-2026-09-06/ contains before snapshots, incremental
change.patch/scope.json, implementation-replay.json. Optional
ObservationHistory(max_complete_record_bytes=2000) expands complete redacted
records only with spare8KB, same baselineIDs/order. Default disabled. No runner/CLI
wiring. Proposed AgentConfig field removed after schema guard failure; config and
execute restored byteexactbefore. Current patchSHA009ee82004a24bc3256723fcf4135c3b8229e1a473bfe4952d732da73f136b1a.
Focusednew+continuity tests16; fullsuite beforeflagremoval702passed1schemafail2skips;
postremovalfocused+architecture30passed; finalfullsuite702passed2optinliveskips.
All test sessions closed. Observation_history agent
finished boundedquickreview CLEAN; review.json SHA110c5dcbb29d7d605aa0826f038364fd7bdd75291d0d2f5c203ef24900fdf540; file_handoff_research prepares draft4-call I34
ordinarypolicydecisionprobe, exacthistoricalrequest with ONLYhistory changed.
Root must review/freeze before dispatch. No large SRE/economics experiments.

I34decisionprobe frozen at artifacts/observation-completion-2026-09-06/decision-probe/plan-01.json
SHA553ffc56b6cde369c4a8ae025e51978f1b6a57c6299be051d05073ed64899f5b.
Runnerb8fe3167392c7dc4f554e6d91b84238c08d234230296fdd04466f4be12da78cc,
source treef9cbf8d62f5204a178380d65ac5df7440f5577e4c7e744aef27d5d3845aea4dd.
ABBA exactsameoriginalI34requestexceptobservation_history; publicprefixI1..33,
nooriginalI34result ininputs. BothI20/I31becomecomplete; baselinealreadymentions
code/available6inpreviousmodelreason. 4logicalmax8adaptercalls(includesadapter
schemaretryallowance),60seach300total,noenvironmentactions. No retryreplacement
afterfailure. Rootpreflight exactbyte restoration+bothhistoryregenerationpassed.
Next: finishsession40054, independentactualdecision/accountingreview; noautomatic
claimquery_logs wrong because I34mayinspectnewtimewindow. Defaultremainsdisabled.

I34probe final rootassessment: baseline2/2identicaltargetedquery_logs,
expanded2/2identicaladvance8626withsixerrorstops. Narrowdescriptivecriterionmet,
noexecutedaction/fulltaskbenefitclaim. Input56006vs56402/output1294vs1000;
4adaptercalls0retries. mechanism_audit finalindependentreview pending.
Next useful step after review: make opt-in history construction available to
AgentRunner without changing persistedAgentConfig schema (e.g. explicit constructor
option with freshperrunhistory), prove defaultidentity/freshrunintegration on generic
toy environment. Then preregister a bounded actualbehavior comparison if justified.
Do not launch another expensive SRE merely to repeat oldfailure; ownerpriority
memorychain was demonstrated on smallfamily, largeeconomicsstilldeferred.
Goal remains ACTIVE/incomplete: repeatedfullsuccessfulSRE stillunmet.
