# Simulator v2 boundary

Implementation record, 2026-09-05. This is the live-simulator prerequisite for
Stage 7, not evidence that a memory configuration improves performance.

## Contract and compatibility

The deployed simulator at `http://81.176.229.58:8080` exposes v2. Its published
`/openapi.yaml` identifies API version `0.5.0`; v1 `/start` returned HTTP 404 in
the preceding compatibility probe. V2 changes both the action space and the
objective, so changing a URL prefix is insufficient.

The inspected OpenAPI content has SHA-256
`452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0`.

The implementation keeps the v1 client and environment and selects the version
at the CLI boundary. Model response schemas and system prompts are selected
together with the environment. V2 has 18 typed control commands, read-only page
probes, logs, metrics, resources, inbox, operation polling and time advancement.
V1 fixes, deployments, scaling and purchase probes do not enter the v2 schema.

## Ownership and sensitive data

The decision model chooses a command and typed parameters containing resource
IDs. The HTTP client resolves panel and target-server authentication privately.
Neither action schemas nor environment sessions need usernames or passwords.
Six commands require server authentication: `database.create`,
`database.inspect`, `database.backup`, `database.restore`, `disk.usage` and
`disk.cleanup`. A database ID is resolved to its hosting server before fetching
that server's current credential reference.

The client must sanitize responses before they cross the `ToolResult` boundary;
observer and audit sanitization alone would be too late for model prompts.
Known secret values require exact replacement as well as ordinary pattern
redaction. Credential-delivery inbox messages require conservative projection:
historical messages can contain old passwords which current resource lookups
cannot recover. Sanitized evidence must not contain raw credentials, including
exception messages and asynchronous operation results.

Only an authentication rejection can trigger a bounded target-credential
refresh. The retry keeps the same command, parameters and request ID. The API
excludes `target_auth` from its idempotency comparison, rechecks authentication
on replay, and returns the original operation for an accepted asynchronous
command. The adapter does not automatically retry arbitrary mutations with a
fresh request ID.

## Lifecycle and outcome

`server.create`, `server.delete`, `database.backup`, `database.restore` and
`site.stop` return asynchronous operations. Acceptance and later observation
produce generic operation links for the runner and memory. A successful
operation does not complete the run. A failed page probe or failed operation
must remain distinguishable from a successful HTTP exchange.

The v2 time action has a separate `advance_time_v2` discriminator so its
`stop_when` condition survives generic action and trace serialization. An
explicit null condition omits the wire `stop_when` field and requests the whole
interval; a condition can stop at
the first matching new log error. Expected firewall rejections need different
handling from infrastructure failures. The model may choose a filter or a
bounded interval without early stopping.

V2's objective is to complete the simulation with `uptime_ratio >= 0.99`, then
minimize `total_cost_minor` among SLO-passing runs. Uptime measures elapsed
availability for legitimate reference traffic, including firewall and site
state. It is not the HTTP success ratio. Cost includes servers and backup
storage; there is no revenue or balance.

Final results come from the authoritative overview. Reaching an agent step
budget does not turn a still-running simulation into a completed one. An early
`finish` action is rejected with the current overview and remaining horizon;
the runner continues until an authoritative terminal state or its step budget.
Partial and failed runs cannot contribute to successful-run
cost aggregates. Generic objective metrics carry the observed availability
and cost evidence without adding simulator types to memory.

## Evaluation boundary

The existing Stage 0 and frozen memory-design documents describe the original
balance-based simulator profile. Preserve that historical profile. Stage 7
needs a separately versioned v2 profile with uptime/cost endpoints, explicit
failure rules, immutable environment identity, train/holdout manifests and
paired first-attempt runs. An adapter smoke test or a single live LLM pilot
does not satisfy that gate. Seed 42 has already been used for debugging and
policy adjustment; it must not be declared an unseen holdout for this work.

## Verification

The live client smoke passed 14 checks using seed 42. It exercised start,
resources, sanitized inbox, authenticated `disk.usage`, the server type catalog,
asynchronous backend creation, same-request-ID replay, operation polling,
asynchronous deletion and final overview. Both operations succeeded and the
replay returned the original operation ID. At the final observation the run
was still running, with 603.22 seconds observed and no downtime. This is a
transport/lifecycle smoke, not a completed agent run.

Sanitized local evidence is in ignored
`artifacts/v2-client-smoke-2026-09-05.json`. A check against the client's known
secret registry ran before saving it. The separate v2 environment integration
test passed on the same server. Offline verification: **361 passed, 2 skipped**;
the skipped tests require explicit live simulator URLs. Ruff and whitespace
checks passed; all 18 changed Python files pass formatting. The whole-project
formatter still reports 17 unchanged, pre-existing files; those were left alone.
Independent Terra High review and a targeted follow-up on the
premature-finish guard found no remaining actionable defects.

### Exploratory LLM attempts

All attempts used seed 42, Codex `gpt-5.4-mini`, no memory and a 40-step budget.
They are sequential debugging attempts with changing code/settings, not paired
evaluation runs. None is evidence of a successful policy or memory improvement.

| Attempt | Result | Evidence and consequence |
| --- | --- | --- |
| 1 | Failed before the first decision | The provider rejected unsupported object-property-count schema constraints. Removed those wire constraints while preserving local validation. The run ID was not captured. |
| 2 | Failed before the first decision | The local Codex configuration supplied reasoning effort `max`, unsupported by this model. Subsequent pilot wrappers explicitly selected `low`. |
| 3 | Incomplete, 6 decisions | The model chose `finish` with the run still active. Final status remained `running`, uptime 0.9996758663 and SLO null. Added an environment guard that rejects early finish. |
| 4 | Completed, **SLO failed**, 7 decisions | Full 604800-second horizon; uptime **0.2603356286**, downtime 447349.01 seconds, cost **4317712903 minor RUB units**. |
| 5 | Incomplete, 40-decision budget exhausted | Retained error stopping but used only 300-second advances. Observed 10266.57 seconds, **1.70% of the horizon**; uptime **0.9998837226**, SLO null, cost **77101916 minor RUB units**. |

Attempt 4 run ID: `ShAdlcABhkj2OkMEuOjmWvpo`. The model observed clean early
logs, then requested 604458 seconds with `stop_when: null`. This disabled error
stopping and consumed almost the entire horizon without intervention. The
adapter executed that request and reported the failed SLO correctly. A short
healthy interval was insufficient evidence that a full-horizon jump was safe.
For attempt 5, the prompt explicitly required retaining first-error stopping
over unobserved future intervals and warned against extrapolating from a short
healthy observation. The model retained that condition but chose 34
time-advance actions of only 300 seconds, interspersed with six reads. Its
run ID is `9WLppE0zehmBsmqKZWQG9yEs`, status `running`, stop reason `maximum
step limit reached`. This is incomplete, not a passed SLO. The model receives
both `iteration` and `max_steps` in its decision context. The next policy
experiment must budget monitoring intervals against the remaining horizon and
decision limit before freezing a v2 baseline. A passing policy has not been
demonstrated.

Ignored local records are under `artifacts/v2-codex-pilot-2026-09-05/`.
Attempts 2–5 have a start/failure/result record with run ID and source-content
hash; attempts 3–5 also have step traces. The pilot wrapper invokes the CLI
entry point but records startup; attempts 3–5 explicitly set reasoning effort `low`.
This is a pilot setting, not a new CLI option. The normal CLI observer does not
record a run ID when model startup fails before the first decision. Stage 7
must capture every declared attempt, including these failures, in its immutable
manifest before claiming evaluation coverage.
