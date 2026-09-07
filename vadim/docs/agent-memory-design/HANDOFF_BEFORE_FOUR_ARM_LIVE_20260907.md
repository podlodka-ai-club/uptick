# Handoff — both paired runs closed; final goal closeout

Work only `vadim/`; preserve dirty worktree and siblings. No commit/push. Goal marked COMPLETE via update_goal after root verification and independent final integration review. This closes the declared experiment, not broad reliability or proven SRE memory utility.

## Current state

12 attempts CLOSED, no active processes, attempt 13 RESERVED and unstarted. Independent Sol review finds it unnecessary for literal goal completion. Do not start it without a new preregistered distinguishing question. No additional API/model calls are needed for closeout.

Latest simulator is supported: start sends participant_token, seed, agent_id, agent_version, request_id; async advance uses operation polling and RUN_BUSY handling. Both evaluation runs observed the public 172800-second horizon. User news about at most one DDoS was not injected as privileged agent knowledge.

| Attempt | Memory | Full SLO | Score | Environment RUB | Tokens | Wall minutes |
|---|---|---|---|---:|---:|---:|
| 11 | frozen historical corpus + same-run | yes, 99.379707% | 100 | 17,733,485.85 | 6,662,971 | 43.677 |
| 12 | same-run only | yes, 99.386574% | 100 | 21,588,527.72 | 5,058,190 | 33.587 |

Both main processes and watchdogs exited 0 without signals or intervention. Same seed 44, Terra medium, same source capsule, policy 1.6, batches 1–4, 480 decisions / 1920 actions / 120 seconds per decision / 7200 seconds wall. Full public startup matched byte for byte before model calls. Historical corpus unchanged in both arms; historical occurrences 198 vs 0. Fresh same-run memory retained in both.

## Findings and selection

Memory availability works; SRE causal utility remains unestablished. One ordered pair cannot explain observed cost differences. Fixed sample 20/198 historical-bearing requests found no demonstrable historical use. Exhaustive 229-action identifier audit found zero confirmed historical-ID errors; the single RESOURCE_NOT_FOUND was not present in prior recalled history. Root monitoring exposure is disclosed, so do not call the whole experiment blind.

Preserve frozen_history attempt 11 as the best observed contest candidate because the official score ties and environment cost is lower. This does not establish memory caused savings. Attempt 11 used 31.7% more tokens than attempt 12 (exact differences in comparison.json). Model money remains unknown. Training08 cost is separate, with one unknown-usage cancelled call. Older weekly failures remain failures and cannot be pooled with this new two-day setting.

Other-task current episodic1.2 evidence: 4/4 raw recoveries vs 1/4 empty, eight real calls. Designed one-step development family, not broad transfer or held-out SRE proof. Recency-complete remains OFF.

## Authoritative next action and evidence

Final report, comparison, all-attempt ledger, immutable source hashes, and criterion matrix:
`artifacts/paired-memory-preparation/paired-sre-results/`.
Independent review: `artifacts/paired-memory-preparation/goal-closeout-review/initial-review.md`.
Accepted GC-001: referenced historical contract documents still label themselves draft. Preserve originals; additive CONTRACT_STATUS_ERRATUM.md explains this without retroactive freezing.

Final review/index verification passed; actual Goal is COMPLETE. No further live runs needed. Previous detailed evidence and exact launch identities: `docs/agent-memory-design/HANDOFF_BEFORE_FINAL_CLOSEOUT_20260906.md`; final outcomes under `artifacts/simulator-refresh-080-2026-09-06/full-sre/terminal-adjudication-11.json` and `terminal-adjudication-12.json`.

Secret stays ignored in `.simulator-participant-token`; never print, inspect, hash, or copy. No new credential use needed. Do not rerun unchanged tests or audits; existing auth 22 tests and harness 7 tests/Ruff passed. Frozen source/plan/corpus artifacts remain untouched.
