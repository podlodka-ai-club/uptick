# Agent Memory implementation handoff

Updated: 2026-09-05 (Asia/Yekaterinburg)

## Resume point

- Branch: `codex/vadim-agent-memory`.
- Stages 1–5 are complete; the Stage 5 checkpoint is
  `0586b0842d8c9790a7dfdf0b52fa722286751951`.
- Stage 6 has a verified experimental implementation. Its held-out effectiveness
  gate remains open; no live training or memory improvement is claimed.
- Stage 6 checkpoint: `cbc1a9d1c8b396106dafb5ed018931338142d170`.
- The simulator v2 prerequisite is implemented and exercised live. CLI defaults
  to v2; explicit `--simulator-api-version v1` preserves the historical adapter.
- Full offline verification: **361 passed, 2 skipped** with the locked optional
  Codex dependency; the v2 live integration test separately passed. Ruff and
  whitespace checks clean. Independent Terra High review and the targeted
  premature-finish follow-up are clean.
- All 18 changed Python files pass formatting. Whole-project formatting still
  reports 17 unchanged files with pre-existing deviations; do not confuse that
  baseline with a new v2 regression.
- The commit containing this handoff is the simulator v2 checkpoint; resolve
  its SHA through Git history and the remote branch.
- Work only below `vadim/`. The modified root `README.md` and untracked root
  `docs/` belong to the user and must not be touched or staged.
- Commits and pushes for `vadim/` on this branch remain explicitly authorized.

## Delivered Stage 6

Read `docs/agent-memory-design/STAGE_6_IMPLEMENTATION.md` for the full record.

- Deterministic extraction groups exact observation conditions/action and a
  configured metric delta; positive and negative lessons share one contract.
- The separate validator requires two completed eligible first-attempt learning
  logical runs, two distinct immutable context-content fingerprints, complete
  assembler-leaf provenance and zero unresolved contradictions. Exact matching
  preserves JSON type distinctions; renamed identical contexts do not add support.
- Source capture freezes immutable run declarations before the episodic snapshot;
  retries use authoritative stored records and preserve snapshot/metadata identity.
- Every declared learning finalization, including failures/retries, revalidates
  existing knowledge. Frozen evaluation never supplies support/counters or triggers
  learning capture. Failed/retried/ineligible runs cannot supply eligible support.
- One immutable batch stores the evidence, candidates and validation manifests.
  Retrieval checks authoritative snapshots and fully regenerates the batch before
  exposing active `derived_untrusted` lessons. Later contradictions remove them
  from decision context. There is no incremental index or LLM reflection yet.
- `lessons_memory_runtime` and `MemoryConfiguration.episodic_with_lessons` are
  programmatic experimental entry points. Settings and run declarations are
  explicit; disabling lessons avoids constructing its source/module.

## Delivered simulator v2 and live results

- The owner authorized trial runs at `http://81.176.229.58:8080`.
- Read `docs/SIMULATOR_V2_ADAPTER.md` for contract identity, architecture and all
  five exploratory LLM attempt outcomes. The v1 endpoint on this server returned
  404 in the preceding probe; that historical failure is not an offline skip.
- The v2 client owns private panel/server credentials, sanitizes before model
  exposure and retries rejected target authentication once with the same request
  ID. The schema exposes 18 typed commands, no auth fields, no v1 mutations.
- The environment preserves async operation links and paginated logs/inbox,
  reports uptime/cost objective metrics and rejects premature `finish` while the
  server reports `running`. Step-budget exhaustion remains incomplete.
- Structured-output schemas are shared across providers. Live schema constraints
  were corrected without dropping local action validation. Generic memory ports
  remain simulator-independent; v1 compatibility facades stay explicit.
- Client smoke passed **14 checks**, including authenticated disk access,
  asynchronous server creation/deletion, polling and same-ID replay. It observed
  603.22 seconds with zero downtime but did not complete the run. Safe evidence:
  `artifacts/v2-client-smoke-2026-09-05.json` (ignored).
- LLM pilot: seed 42, Codex `gpt-5.4-mini`, no memory, 40-step budget. Attempts 1
  and 2 failed before the first decision (schema, then inherited effort `max`).
  Attempt 3 used effort `low` but ended early; this led to the finish guard.
- Attempt 4 completed the **7-day horizon** in 7 decisions with **SLO false**,
  uptime **0.2603356286** and cost **4317712903 minor RUB units**. Run ID:
  `ShAdlcABhkj2OkMEuOjmWvpo`. The model skipped almost the entire horizon with
  `stop_when: null` after seeing clean early logs. The transport worked; a
  successful policy has not been demonstrated.
- Attempt 5 tightened that prompt rule and retained error stopping, but exhausted
  40 decisions on short waits. It observed **10266.57 seconds (1.70% of the
  horizon)** with uptime **0.9998837226**, cost **77101916 minor RUB units**,
  status `running` and SLO null. Run ID: `9WLppE0zehmBsmqKZWQG9yEs`.
  The policy needs to budget monitoring intervals against the remaining horizon;
  `iteration` and `max_steps` are already supplied to the model.
- All retained pilot records/traces are ignored under
  `artifacts/v2-codex-pilot-2026-09-05/`. Attempts 2–5 use a diagnostic wrapper
  that captures startup/run ID; attempts 3–5 explicitly select reasoning effort
  `low`. There is no new effort CLI option. Attempt 1's run ID was not retained.

## Next work

- Diagnose and improve v2 monitoring/time-advance policy before freezing a
  baseline. Do not call the exploratory retries first-attempt evaluation evidence.
  Seed 42 has been used for debugging and policy tuning, so it is not an unseen
  holdout candidate.
- Stage 7 needs a separately versioned uptime/cost profile; preserve the original
  Stage 0 balance profile and historical memory-design documents. Implement the
  preregistered paired harness, bind every attempt (including startup failures)
  to immutable manifests and compare lessons against episodic-only on held-out
  seeds. Current tests, smoke checks and pilot traces are not promotion evidence.

## Delivered Stage 5

Read `docs/agent-memory-design/STAGE_5_IMPLEMENTATION.md` for the complete record.

- Structured audit supports natural idempotent replay, append races, one
  transient retry, mandatory sanitization/quarantine and validated reads.
- Separate request, decision, transition and outcome correlations join context
  selection, input, selection before execution, completion and terminal status.
- Created-item evidence comes from typed module receipts; episodic receipts
  are reconstructed from the authoritative stored record after append.
- Raw-body flags affect audit captures only. Primary memory semantics remain
  intact; sanitized selection/action/result/provenance/outcome facts remain
  in audit metadata when raw captures are disabled.
- `run.outcome` is the runner-observed outcome, recorded before module
  finalizers. It does not claim atomic finalization across stores. Legacy
  evidence failures do not prevent typed finalization attempts or replace an
  original run failure/cancellation.
- Provider-neutral prompt capture uses each supported facade's request builder.
  Custom models without it are explicitly labelled as context surrogates.
- Redaction covers quoted and nested JSON in prompt strings, including normal
  JSON escaping and quote-only escaped fragments. It remains pattern-based,
  not an arbitrary-secret detector.

## Standing scope and next gates

- Ponytail remains active: keep the smallest implementation satisfying the
  frozen design. Root owns planning, architecture, review and verification;
  delegate bounded implementation work to subagents.
- Structured audit is programmatic. Legacy CLI/observer JSONL and summaries
  remain smoke output, not promotion/evaluation evidence.
- `simulator-audit-retention-v1@1.0` declares retention; expiry, holds enforcement,
  compaction and deletion engines are not implemented.
- Stage 6 is implemented experimentally; Stage 7 evaluation remains unimplemented.
  Their evidence gates still apply; do not infer measured improvement from tests
  or traces. The v2 adapter prerequisite is complete; live pilots either failed
  SLO or remained incomplete.
- Stage 0 has an offline scaffold, not collected live baseline evidence.

## Verification commands

Run from `vadim/`. The child environment excludes inherited API keys because
Codex subscription-guard tests intentionally reject them; all SDK tests are fake.

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  uv run --extra codex --locked pytest -q -ra
UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --extra codex --locked ruff check .
git diff --check -- .
```
