# Agent memory: active handoff

Updated: 2026-09-05. Worktree: `codex/vadim-agent-memory`, base HEAD `ae9cb69`.

## Goal and boundaries

**Immediate continuation:** user explicitly approved the exact pair target, budgets
and historical-context transmission. Goal is **active**, blocker resolved.
Final plan/source/config/runner hashes revalidated; pair exec session **34113**
is still live. First arm empty_history **finished**, physical run
**nlJuWjJ6PanT0A0678G6wb9z**:160steps/2059.5s,37.03% horizon,uptime97.3279%,
slo_passed=null,cost16679341848minorRUB; stop maxsteps, not60-minute wall.
Second frozen_history arm is now LIVE, run **16vqjJlBHhAvEW6TLNWH1dZP**.
Poll the same session; do not launch/restart another copy or change frozen inputs.
Output `artifacts/persistent-recall-2026-09-05/pair-dev-01/`; approval recorded in
`launch-authorized-01.json`. Prior approval rejections remain historical evidence.
Read-only `inspect_pair.py` reports accounting but is not a complete integrity or
stale-evidence verifier; it was checked on dev06 negative outputs.
Reporting-only config-key mismatch was fixed; prior inspector is preserved as
inspect_pair_pre_live.py. Pair inputs were not changed. Independent prefix audits
are in pair-dev-01/prefix-integrity-empty*.json and qualitative-prefix-105.md.
I34 repeat is justified: actual context truncated I31code/values, unlike fulltrace.
Offline cap sensitivity and spare-budget expansion prototypes are under the
same artifact root (replay_history_*.py, history-*.json); production unchanged.
Expansion restores complete fields without dropping baseline IDs within8KB,
but no model utility is demonstrated. Non-SRE rawrecall harness is being prepared
offline by file_handoff_research; do not dispatch it until the SRE pair finishes,
to avoid confounding latency. Context-prefix-analysis.md reports measuredbytes
and provider tokens separately. See PERSISTENT_RECALL_RESULTS.md for details.

The persistent Codex goal is **active**, with no global token budget. Read
[ACTIVE_GOAL.md](ACTIVE_GOAL.md) for the completion contract.
Owner update (2026-09-06): peer agents need90–100 minutes for a full simulated
week. Treat60-minute cap as possibly undersized, not evidence of abnormal speed.
Do not change the livepair. Plan the next full run with120-minute allowance and
a larger preregistered decision cap; judge horizon and SLO separately.

Priority: memory, successful full public SRE behavior, and a controlled comparison
of persistent memory under identical within-run context. Neither green tests nor
successful retrieval microprobes close the goal. Current continuation made progress:
completed a frozen reuse experiment, followed the full-task check, and fixed an
opt-in episodic evidence-visibility defect. The explicit user approval resolved the external launch blocker.

Change only `vadim/`; preserve root/sibling work and unfinished existing changes.
Keep oracle/evaluator answers, hidden simulator state and future observations out
of policy/memory inputs. Do not relax learning/evaluation gates for positive results.
Public simulator and OpenAI Codex subscription experiments are already authorized.
User explicitly switched the internal decision model to **gpt-5.6-terra / medium**.
Use that for new experiments; preserve historical Sol/low source capsules and results.
Use `env -u OPENAI_API_KEY -u CODEX_API_KEY` for subscription runs. Locally use
`UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --locked --offline ...` from `vadim/`.

## Latest full-task result: dev06 finished locally

- Full SRE **dev-06 local process exited0**, session28040 is closed. Physical run
  `Vp0ETbGWqA6FTmmZUWNg8aww` remains incomplete:160decisions,30.6% horizon,
  uptime0.9722873,slo_passed=null,2159.37seconds,4,456,121 inputtokens.
  Do not poll/restart the closed process or present simulated status running as
  an active local process. That historical process is closed; the new paired process is live as described above.
- Plan: `artifacts/sre-completion-2026-09-05/sre-plan-06.json`; output: `dev-06/`
  below that directory. Same Sol/low, seed42,160decisions,2400-second wall budget,
  no persistent memory and public tool surface as dev05. Current history/guidance
  are the changed active mechanisms. Archive tools are **not integrated**.
- Source capsule hash: `680cd22cef29e3346683f3a9c5b8267d0bf5867d74b8e323939e02443f3916e9`.
  Independent preflight passed. Incidental existing EpisodicRecallSettings remains
  inert; source_dirty=true is declared. First launcher failed before plan load
  due to a relative path; zero remote requests, separately retained.
- Independent `dev-06-verification.json` passed, errors=[]; all160requests,
  results and actions paired. Four failed actions retained. Full task not solved.
  [SRE_DEV06_RESULTS.md](SRE_DEV06_RESULTS.md) has costs,
  negative outcome, and the next stale-operation-memory hypothesis.

## Implemented mechanisms and latest evidence

- Generic observation history is in the runner: latest result per exact action,
  1KB record / 8KB serialized history / 24entries, redacted, immutable, run-local,
  with metadata/truncation markers. Same mechanism in all persistent-memory modes.
  Full latest result remains unbounded.
- Experimental ObservationArchive supports exact UTF8 read and literal find only
  within opaque issued run refs. Storage limits:1MiB/record,8MiB total; read/find
  payload<=8KiB,query<=256bytes. It is not wired to CLI/runner.
- Current checks: **695 passed,2 opt-in live skips**, Ruff and scoped reviews clean.
  Rawrecall review fixed missing public final metrics; pass2clean. Historical56
  schemafixture preserved through exact additive-field projection, defaultcanonical
  unchanged. Review markers validated under persistent-recall/raw-review/.
  No new commit/push made in this continuation; keep unrelated dirty files intact.
- [MECHANISM_RECOVERY.md](MECHANISM_RECOVERY.md): diagnosis
  and 16 public-prefix probes. History/guidance helped targeted diagnosis but also
  substituted an unverified action for a repeated read; no SRE success follows.
- [OBSERVATION_HANDOFF_RESULTS.md](OBSERVATION_HANDOFF_RESULTS.md):
  nine episodes; directory-based exact reads recovered facts but increased total
  input tokens34.3% and generation time2.65x versus fullinline.
- [OBSERVATION_REUSE_RESULTS.md](OBSERVATION_REUSE_RESULTS.md):
  18questions/28generations,all18correct. Carry saved14.5% inputtokens versus repeated
  find and20.7% versus fullinline; time remained1.85x fullinline. Two real reuse and
  two actual stale-exposure opportunities passed. Independent719checks passed.
  All pilot processes are finished.
- Optional episodic version1.1 exposes one exact query-match window hidden by
  ordinary field prefixes, <=600 serialized bytes. Default1.0 unchanged; tested
  actual runtime composition, eligibility, trust and provenance. Mechanical
  evidence only; not in dev06 capsule and no model utility experiment yet.
  [EPISODIC_EXCERPT_RESULTS.md](EPISODIC_EXCERPT_RESULTS.md).

## Next decisions and supporting history

Dev06 exhausted decisions without completing the task. Next isolate last-observed
operation statuses being presented as current pending work; see result document.
Four decision-only requests are frozen in `artifacts/operation-freshness-2026-09-05/`:
plan SHA `3a03e01c00ff7f7dcff5c363672384826e2a0b910e0f5da021cd8183cffbd191`.
All4calls finished, session60545 closed. Independentpreflight195passed. Exact arms
both300wait; changed arms create/querylogs; prereg result **inconclusive**.
See [OPERATION_FRESHNESS_RESULTS.md](OPERATION_FRESHNESS_RESULTS.md).
Policy1.3 semantic correction is complete/reviewed: unresolved evidence vs latest
reported pending distinguished; headroom/floor and accepted->metrics->wait preserved.
Its behavior differs from the earlier probeeligibilityintervention.
Corrected offline9case shadow-v2 independently replayed: bothqueryarms selectlatest
transition in all9; prioriteration2bug preserved inerratum. Reconstructionomitspre_state;
do not claim exactoriginal/crossrunranking or wirequeryenrichment fromthisresult.
Terra/medium actual-policy probe completed all4calls,session38770 closed. Policy1.2
both300wait; actual1.3 bothserver.create. Independent51checks passed;131790input,
2879output,72.65generationseconds,zeroactions. This is compatibility/action-selection
evidence only, not SRE utility. Plan `artifacts/terra-policy-2026-09-05/plan.json`
SHAfe51cbef52a0504076216f4843cba7e5130cc70afc46056ae71afe8467baed39.
Next: paired development full-task comparison onTerra/medium; identical current-run
episodic runtime/history/config/budgets, only empty vs frozen historical corpus differs.
The paired harness is frozen and live; see the immediate continuation above.
Actual corpus export276episodes+36outcomes/24namespaces is frozen under
`artifacts/persistent-recall-2026-09-05/`; root verified all312rows againstSQLite.
Existing completed-only eligibility already allows271episodes. 8k advanced retrieval
selected3historicalruns vs1under4k in exact prefix136query; availability only,
no approximatecurrentrun seeded. See PERSISTENT_RECALL_RESULTS.md.
Optional rawrecall(schema1.4) admits finalizedcompleted/failed/interrupted, excludes
crossrunexcluded/missingoutcome; defaultunchanged. Metadata includes outcome status,
executionterminal,finishedat,boundedstopreason. RunOutcome.terminal is execution
finalization, NOTworldgoalcompletion. Queryexcerpt1.1 composes withrawrecall.
Source capsule forTerra4probe predatesrawfeature. New source is frozen at
`artifacts/persistent-recall-2026-09-05/source-raw-recall`, hash
e25da72bee3ffe1427d061c435da723e601a694a2cd705ec44bce3cf20665f94.
Outcomeview nowincludes exact orderedpublicmetricprefix<=1024bytes andomittedcount,
status_scope=execution,stop_reason_truncated marker; noderivedsuccessclassification.
All35completedcorpus outcomes have604800observedseconds butuptime22.21–27.68%.
Live pair budgets: 160 decisions / 3600 wall seconds per arm, serial empty then frozen.
Artifactrun_pair.py reviewed/fixed: exactrequest/dataclasstelemetry (alsofailedcalls),
cancellationpropagation,namespaces,readonlyhistory,realHTTPstartupdict runID.
Root actualwrapperfake smoke passed; earlierfake tuple evidence preservedseparately.
FinalrunnerSHA08d8d44388a04b0f258c701500e6390f506df188213d99f8c24b2d8133e68a1d.
Finalplan `artifacts/persistent-recall-2026-09-05/pair-plan-01.json` SHA
a9b3d621433f0848b304d075a0b6e1eaa8c9d534d39500e139291fa604be8715.
Finalofflinepreflightpassed; pair-dev-01 nowliveinsession34113; seeimmediatecontinuation.
Rawconfiguration artifactSHA d55757c67e1f46a5ef818409dc3b645e0742ddaa9d75e12d9aeee992453be270,
fingerprint02c5956fcf9e2e591dea35cae415a4b7a99d41d999a5405087b2faa77aa9a1db.
Finalrawreplay(rootindependent) selects2episodes(1completed,1interrupted),5970/8000
estimatorbytes. OriginalSolharness-contract wronglyvariedobservation_history;
preservederratumcorrects tohistoricalcorpusONLY. Finalplanbindserratum.
User asked what memory actually learned: answered from SQLite,276 SRE matrix episodes
but zero active SRE rules; six validated rules belong only to the controlled fixture.
See [MEMORY_CONTENTS_2026-09-05.md](MEMORY_CONTENTS_2026-09-05.md).
Do not rerun expensive diagnostics unchanged. Any archive integration must preserve environment
schema ownership, trace visibility, and charge retrieval decisions to the budget.
Persistent-memory learning/frozen-evaluation separation remains required. Seed42
is development data, and the public API lacks authoritative immutable world/family
identities; this is an open final-generalization gate, not permission to invent one.

General-agent controls/Sol handoff research:
[WORKING_MEMORY_RESEARCH.md](WORKING_MEMORY_RESEARCH.md).
Historical stages, prior authorizations, run details and original reading list are
preserved in [the previous handoff](HANDOFF_ARCHIVE_2026-09-05.md).
For remaining contracts use REMAINING_EXECUTION.md and EXPERIMENTAL_MEMORY_GUIDE.md
under `docs/agent-memory-design/`; successful SRE and persistent utility remain open.


## Owner priority clarification — 2026-09-06

Before another large SRE/budget/economics experiment, simplify and establish the
memory chain: exact event persistence with provenance/time; relevant factual
retrieval for matching situations and explicit non-applicability/uncertainty for
negative controls; then model use of those facts, including failed outcomes and
stale evidence. Keep evaluator expectations outside model/retrieval inputs.
Finish the already-running frozen pair unchanged. The next experiment should
be small and diagnose memory directly; do not launch the proposed120-minute SRE
run immediately afterward. The larger budget remains a later calibration option.
Mechanical storage checks and isolated successful retrievals do not establish
systematic relevance, model utility, or economic benefit.
