# Agent Memory implementation handoff

Updated: 2026-09-05 (Asia/Yekaterinburg)

## Resume point

- Branch: `codex/vadim-agent-memory`.
- Stages 1–5 are complete. Stage 5 closes the unfinished implementation from
  checkpoint `dbdbb42bf453af7d7ce6bd115562f67d3a069aa9`.
- Full verification: **292 passed, 1 skipped** with the locked optional Codex
  dependency; Ruff and whitespace checks clean. Only the live simulator test
  is skipped. Independent Terra High correctness/security review is clean.
- The commit containing this updated handoff is the Stage 5 implementation
  checkpoint; use Git history and the remote branch to resolve its SHA.
- Work only below `vadim/`. The modified root `README.md` and untracked root
  `docs/` belong to the user and must not be touched or staged.
- Commits and pushes for `vadim/` on this branch remain explicitly authorized.

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
- Stage 6 lessons and Stage 7 evaluation remain unimplemented. Their documented
  implementation/evidence gates still apply; do not infer activation or
  measured improvement from Stage 5 traces.
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
