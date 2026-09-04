# Agent Memory implementation handoff

Updated: 2026-09-05 (Asia/Yekaterinburg)

## Resume point

- Branch: `codex/vadim-agent-memory`.
- Stages 1–5 are complete; the Stage 5 checkpoint is
  `0586b0842d8c9790a7dfdf0b52fa722286751951`.
- Stage 6 has a verified experimental implementation. Its held-out effectiveness
  gate remains open; no live training or memory improvement is claimed.
- Full offline verification: **333 passed, 1 skipped** with the locked optional
  Codex dependency; Ruff and whitespace checks clean. Independent Terra High
  review is clean after the accepted manifest provenance fix.
- The commit containing this handoff is the Stage 6 implementation checkpoint;
  use Git history and the remote branch to resolve its SHA.
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

## Live simulator probe and next work

- The owner authorized trial runs at `http://81.176.229.58:8080`.
- Direct API v2 smoke with seed 42 succeeded: all 18 commands were listed,
  `server.types.list` and `site.config.get` returned HTTP 200, time advanced by
  300 seconds. The observed interval had zero downtime; the run remained running.
- The existing v1 adapter was tested against that address and failed with HTTP
  404 at `/v1/start`. The live failure is separate from the offline skipped test.
  A full LLM-agent run did not happen.
- Next prerequisite: adapt the simulator boundary to v2, including control/server
  credentials, 18 commands, asynchronous operations and the uptime/cost objective.
  A prefix-only URL change cannot work. Preserve the generic memory boundaries.
- Then implement Stage 7's preregistered paired harness, bind run declarations to
  immutable manifests and compare lessons against episodic-only on held-out seeds.
  Current synthetic tests and the v2 smoke are not promotion evidence.
- Sanitized local smoke evidence is under ignored
  `artifacts/live-v2-smoke-2026-09-05.json`; credentials were not saved in it.

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
  or traces. Live simulator-v2 adaptation is the immediate prerequisite.
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
