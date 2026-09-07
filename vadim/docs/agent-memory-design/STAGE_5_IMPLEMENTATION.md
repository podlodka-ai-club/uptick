# Stage 5 Implementation Record

Decision status: **accepted**
Implementation status: **complete**

## Scope and composition

Stage 5 adds versioned structured audit events to the existing runner and
memory boundaries. It introduces no learned memory module or evaluation
promotion path. Compose `AuditConfiguration.simulator_default()`, an explicit
`StructuredAuditTraceSink`, and a memory runtime with matching resolved
configuration fingerprints. Both structured stores are supported. Use separate
episode and audit namespaces, fresh for each independent experiment; an audit
namespace is bound to one resolved configuration.

The legacy CLI and JSONL observer remain compatibility/smoke outputs. Enabling
structured audit is programmatic. A successful trace does not turn those
legacy summaries into validation or promotion evidence.

## Evidence and correlations

The event sequence for an executed decision is:

1. `memory.context_selected`: candidates, selected IDs, module/version, scores,
   selection rationale, effective budgets, consumption and truncation.
2. `decision.input`: observation and the provider-neutral structured request.
3. `decision.selected`: the model's selected decision, before environment
   execution, so execution failure does not erase its action.
4. `memory.item_created`: actual item ID, type and provenance returned by a
   memory module's typed `CreatedMemoryItem` receipt.
5. `decision.completed`: observed result, selected memory inclusion, transition
   ID and the run-outcome correlation.
6. `run.outcome`: the runner-observed terminal status.

`request_id` joins context selection to decision input; `decision_id` joins
input, selection and completion. The transition ID joins completion to the
persisted episode. The deterministic outcome correlation joins decisions to
their run outcome. Event IDs are stable and distinct from these correlations.
`sequence` orders the event phases; `event_id` breaks ties deterministically
within an item-created phase. It does not claim total append chronology.

`ExperienceSink.record()` returns a list of created-item receipts or `None`
when it cannot report created artefacts. The orchestrator never infers that a
generic sink created an episode. Episodic receipts identify the persisted
episode and return equivalent data on replay. The module reads the authoritative
record after append instead of trusting a cached operation receipt's payload.

The OpenAI, Codex and registry decision facades use the same request builder
for `prompt_trace()` and `decide()`. The captured representation includes
ordered messages, model selection, settings and response schema before provider
conversion. Provider SDK wire payloads and internal retries are outside this
trace. Custom decision models without this capability are explicitly labelled
`decision-context-surrogate`; their context is not proof of the exact prompt.

## Raw-content and integrity policy

The three `audit.raw_content` switches govern structured audit bodies only.
They independently disable prompts, observations and decision traces without
rewriting primary episodic/legacy records or their semantic outcome fields.
Mandatory secret handling still applies at every primary persistence boundary.
A global primary-record suppression policy would be a separate contract.

Core structured evidence remains in sanitized event metadata with raw captures
disabled: candidate/selected IDs, scores and rationale, module versions,
budgets and truncation, typed selected action, result facts, created-item
provenance, and outcome status/metrics. Full model decision narratives and
prompt/observation capture bodies remain conditional. A switch controls its
declared capture class: observation content already included in an enabled
prompt capture is not removed by disabling the separate observations capture.

Enabled bodies are sanitized before hashing and storage. Each attempted capture
records its policy, redactor, retention reference, capture state and redaction
outcome. Failed body redaction yields a body-less quarantine entry; failed
metadata sanitization rejects the event. Disabled captures retain no body or
body hash. Credential fixtures include quoted JSON assignments embedded inside
prompt strings. The pattern-based redactor covers the repository's credential
forms; it is not an arbitrary-secret detector.

Replaying an event compares its persisted semantic fields, excluding only the
generated event timestamp. An equivalent replay returns the original event;
different retained data conflicts. The same rule resolves an append race. A
transient store failure receives at most one retry. Reads validate record,
capture and redaction hashes and reject inconsistent event/policy metadata.
Structured record and snapshot reads also verify their content hashes.

## Lifecycle and partial writes

`run.outcome` describes what the runner observed, not successful finalization of
all memory modules. It is written before module finalizers. If a finalizer
fails, the recorded world outcome remains factual and the error is surfaced.
An audit failure can stop finalization; these operations are not an atomic
transaction across modules and stores.

Failure and cancellation after session creation attempt failed/interrupted
outcome recording without replacing the original exception. A failure before
session creation has no stable run ID. A selected event may exist without a
completed event when execution or subsequent persistence fails. A primary
episode may exist without its audit event when the audit write fails.

## Retention and stage gates

`simulator-audit-retention-v1@1.0` is a validated declaration in the resolved
configuration fingerprint: raw bodies and snapshots have a minimum 90-day
retention; summaries and validation/promotion/approval/rollback records have
project-lifetime retention. This stage implements no expiry, compaction,
deletion, hold scheduler, lesson generation, semantic retrieval or activation.
Later evaluation manifests must still bind their complete evidence and policy
references before any promotion claim.

## Verification

Verified on 2026-09-05 (Asia/Yekaterinburg):

- Full offline suite including the optional Codex SDK: **292 passed, 1 skipped**.
  The only skipped test requires `UPTICK_INTEGRATION_SIMULATOR_URL`; no live
  simulator or model run is claimed.
- `ruff check .` and scoped `git diff --check`: clean.
- The README's structured-audit composition example executes against temporary
  SQLite. A scripted two-decision runner produces 11 joined audit events, two
  episodes and one outcome with both structured store backends.
- Independent Terra High correctness/security review: clean after fixing
  escaped JSON secret handling, authoritative created-item receipts and
  always-retained structured audit metadata. Same-phase event ordering is
  intentionally deterministic rather than a total append chronology.

Reproduce the full suite without inheriting API keys into Codex subscription
guard tests (those tests use fake clients):

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  uv run --extra codex --locked pytest -q -ra
UV_CACHE_DIR=/private/tmp/uptick-uv-cache uv run --extra codex --locked ruff check .
git diff --check -- .
```
