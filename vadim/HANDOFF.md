# Agent Memory implementation handoff

Updated: 2026-09-04

## Resume point

- Branch: `codex/vadim-agent-memory`
- Last completed and pushed commit: `54f467a99e1febc6ad214c4bb6b26fb92b9c863f`
- That commit completes Stage 4. Its last full verification was `181 passed, 2 skipped`, Ruff clean, and `git diff --check` clean.
- Stage 5 is **in progress, uncommitted, and not green**. Do not claim it complete or commit it wholesale before the blockers below are resolved.
- Work only below `vadim/`. The modified repository-root `README.md` and untracked repository-root `docs/` belong to the user and must not be touched or staged.
- The user has explicitly authorized commits and pushes for `vadim/` on this branch.

## Standing decisions

- Keep the implementation private to `vadim/` so it cannot become a repository-wide rule.
- Use the smallest implementation that meets the frozen design. Ponytail mode is active: no Stage 6/7 work, semantic retrieval, lesson generation, consolidation scheduler, or retention/deletion engine yet.
- Raw prompt, observation, and decision-trace bodies are enabled in the simulator audit profile, independently switchable by versioned configuration, and must pass mandatory secret handling before persistence.
- Retention is declared now by `simulator-audit-retention-v1@1.0`; execution remains a later stage.
- The canonical Stage 5 evidence path should use the structured store. Legacy JSONL CLI/observer output is compatibility/smoke output, not promotion or evaluation evidence.
- External document reviews were already obtained from Claude CLI, Pi DeepSeek Pro, and Pi Kimi K3 before implementation began.
- Continue orchestrating with Terra High for complex reviews and Luna XHigh for medium/mechanical work.

## Current uncommitted work

The working tree contains these Stage 5 changes under `vadim/`:

- `memory/audit.py`: versioned structured audit events, three independent raw-body captures, redaction/quarantine metadata, hashes, one transient retry, and read-time validation.
- `memory/config.py`: audit, raw-content, redactor, and retention declarations included in the resolved configuration fingerprint.
- `memory/orchestrator.py`: audit-sink composition checks plus context-selection, transition/item, and run-outcome event emission.
- `runner.py`, `models.py`, and `ports.py`: deterministic decision/transition correlations and decision input/completion trace calls.
- `llm/contracts.py`, `llm/openai.py`, `llm/codex.py`, `cli.py`, and their tests: deterministic serialization of the exact provider-neutral `StructuredGenerationRequest`; all three supported decision facades expose `prompt_trace(context)` using the same request builder as `decide()`.
- Structured stores now verify record and snapshot hashes on reads; the store-focused subagent reported `81 passed` and Ruff clean for its bounded scope.
- `tests/test_memory_audit.py` was being authored by a Luna XHigh subagent when work was stopped. It has 378 lines and was not reviewed or run after interruption; treat it as a draft.

The prompt-capture subagent reported `19 passed, 1 skipped` and Ruff clean for `test_llm_boundary.py`, `test_codex.py`, and `test_cli.py`. The runner was then updated to consume `prompt_trace()` with an explicitly labelled `decision-context-surrogate` fallback, but that integration has not been tested.

## Blocking findings to resolve first

1. **Audit replay is not naturally idempotent.** `event_id` and the store idempotency key are stable, but a newly constructed replay gets a new default `occurred_at`, so the store sees different input and raises `MemoryConflictError`. A Terra reviewer reproduced this. The smallest robust fix is to read an existing event by `event_id`, validate it, compare all semantic fields except the generated timestamp, and return it when identical; on an append race, perform the same comparison after the conflict. Different bodies/metadata under one event ID must still conflict. Update the draft retry/replay test and any wrapper store with `get()`.

2. **Raw policy scope is currently inconsistent.** The in-progress code applies `audit.raw_content` by mutating primary episodic records and legacy entries, including replacing `RunOutcome.stop_reason`. A Terra reviewer correctly flagged that this destroys structured-memory semantics and that the policy is nested under `audit`. Recommended minimal resolution: remove the new raw-policy mutation from `EpisodicMemory` and `LegacyMemoryAdapter`, keep mandatory sanitization at every primary-store boundary, and make the three switches govern only structured audit bodies. Document that exact scope. If a future design intends global raw suppression, introduce a separate explicit primary-record policy rather than silently changing semantic records.

3. **`memory.item_created` currently overclaims a generic `ExperienceSink`.** The orchestrator assumes every sink creates one episode with `item_id == transition_id`, although the protocol returns no created artefact. Do not leave this lie in a generic boundary. Smallest honest choices: have the episodic sink return a typed created-item receipt that the orchestrator can audit, or rename the event to `memory.transition_recorded` and state that item-created evidence is not yet complete. Stage 5's stated outcome favors the small typed receipt.

4. **Correlations need one final pass.** Add an explicit `request_id` to audit writes/events rather than only aliasing it to `decision_id`. Add the deterministic run `outcome_correlation_id` to each completed decision so the per-decision trace can join to `run.outcome`. The transition ID already joins the decision and episodic record.

5. **Failure-path evidence is incomplete.** A selected decision is currently recorded only after environment execution succeeds. Decide whether to add the minimal `decision.selected` event immediately after `decide()` so an execution failure still retains the final action. Failed/interrupted run outcomes are attempted through `finalize_run`; add tests before claiming coverage.

6. **Run-outcome ordering needs explicit semantics.** `MemoryOrchestrator.finalize_run()` currently emits the audit outcome before module finalizers. Either define that event as the runner-observed outcome (not proof that every module finalized), or move it after successful primary persistence and document the partial-write behavior if audit then fails. Do not imply an atomic transaction across stores.

7. **Existing runner fakes do not implement the new port.** Add `TrackingMemory.record_trace()` in `tests/test_runner.py` (delegating to its runtime is sufficient), then update ordering assertions only where the new events are intentionally observed. The last full-suite run from the prompt subagent had five failures for this missing method; newer changes may introduce more.

8. **The current edits are unverified.** `config.py` was just aligned to the normative `validation_promotion_approval_rollback_records` retention field, empty raw-body objects were allowed, `list_events()` was sorted by `(sequence, event_id)`, and runner prompt capture was integrated. None of those final edits has had Ruff or pytest run.

## Suggested completion order

1. Read this file, the Stage 5 section of `docs/agent-memory-design/03_IMPLEMENTATION_PLAN.md`, and the raw/retention sections of the RFC and architecture tests.
2. Inspect the complete diff and resolve blockers 1–6 without expanding beyond Stage 5.
3. Review and finish `tests/test_memory_audit.py`; add focused runner correlation/failure tests and configuration/composition tests.
4. Run focused tests, then full pytest, Ruff, and `git diff --check` from `vadim/`.
5. Run an independent Terra High correctness/security review. Fix only concrete Stage 5 findings.
6. Run the `autoreview` closeout workflow. If clean, update Stage 5 implementation/status documentation.
7. Stage only `vadim/`, create a contextual commit, push `codex/vadim-agent-memory`, and verify local and remote SHAs match.

Useful commands after source review:

```bash
cd /Users/mingazhev/Repos/podlodka/uptick/vadim
UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run pytest
UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run ruff check src tests
git -C /Users/mingazhev/Repos/podlodka/uptick diff --check -- vadim
```

## Stop condition for this handoff

All subagents were stopped or had already completed. No Stage 5 implementation commit or push was made in this session. Preserve the working tree and resume from it; do not restart Stage 5 from scratch.
