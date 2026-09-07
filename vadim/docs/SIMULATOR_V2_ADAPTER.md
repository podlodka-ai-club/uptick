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

Attempts 1–5 used seed 42, Codex `gpt-5.4-mini`, no memory and a 40-step budget.
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
At that checkpoint this was a pilot-only setting. The normal CLI observer does not
record a run ID when model startup fails before the first decision. Stage 7
must capture every declared attempt, including these failures, in its immutable
manifest before claiming evaluation coverage.

### Follow-up policy and observability work

The historical pilots below used `simulator-v2-time-budget@1.0`: a deterministic,
observable-only wrapper computes a bounded wait floor from the remaining
horizon and decision budget. It preserves the stop condition, exempts pending
operations, and records proposed/effective duration when it adjusts a decision.
Raw adapter action semantics remain unchanged.

`--reasoning-effort` is now an explicit portable CLI setting. Provider-neutral
telemetry retains adapter-visible call/validation-retry counts, measured time,
known usage and usage completeness on success, failure and cancellation.
SDK-internal transport retries are not observable. Unknown monetary cost remains
null. Partial log pages now say that unread logs remain; their error counts do
not describe the full unread journal. The prompt requires investigating the
observed error stop rather than treating a partial clean page as recovery.

| Attempt | Frozen settings | Result |
| --- | --- | --- |
| 6 | Seed 42; no memory; `gpt-5.4-mini`, low, 40 decisions; time policy | Incomplete: 32640.388456408 seconds observed, uptime 0.9975761867223897, SLO null, cost 257006093 minor RUB. Run `oHYrv6cMFNipb78wrJXDRm3Y`. |
| 7 | Same source capsule as 6; seed 42; no memory; `gpt-5.4`, medium, 160 decisions | Completed 604800 seconds in 159 decisions: SLO false, uptime 0.2657543556929663, cost 8404012903 minor RUB. Run `HnO73c9kpqjlz1VK91K9OzKP`. |
| 8 | Seed 42; no memory; `gpt-5.4-mini`, low, 40 decisions; explicit CLI effort and corrected log-page visibility | Incomplete: 32592.52497722 seconds observed, uptime 0.99769220832456, SLO null, cost 257006084 minor RUB. Run `LPmofpKzcX0p17ZsLw2US9Yd`. |
| 9 | Same source, seed, effort and budget as 8; model `gpt-5.6-sol` | Completed 604800 seconds in 27 decisions: SLO false, uptime 0.2659386936335913, cost 8455412903 minor RUB. Run `XQqybzunrxvpO8ul2VvQYZ7w`. |

Attempt 6 confirmed that wait durations were raised, but first-error stops and
repeated diagnosis still exhausted the budget. Attempt 8 read filtered capacity
errors and successfully called `server.types.list`, but made no corrective
resource change and exhausted its budget. The observability fix did not establish
a task-performance improvement. Attempt 7 changes both model and step budget;
it is not a one-factor ablation. It successfully created a server, polled the
asynchronous operation to completion and observed two active resources. It
then continued reading old logs and finally skipped the remaining 517000.60
seconds. The full seven-day run had 444071.77 seconds of downtime. This proves
the authenticated mutation path works; it does not establish a successful SRE
policy or a memory benefit. Attempt 9 created a server at decision 11 and
finished in 268.86 wall seconds, but also stopped corrective work and skipped
the remaining horizon. Faster decisions did not recover the SLO. Its model was
verified against the local Codex subscription catalog before execution.

Each follow-up pilot imports a frozen source capsule in its ignored attempt
directory so concurrent development cannot change the code mid-run. Scripts,
source hashes, physical run IDs, traces and outcomes remain alongside the earlier
attempts. The Stage 6 v2 learning obstacles are documented in
`agent-memory-design/STAGE6_V2_DIAGNOSIS.md`.


### No-stop wait correction (policy 1.1)

The current CLI composes `simulator-v2-time-budget@1.1`. An explicit no-stop
wait now needs current public evidence that the full-horizon downtime allowance
is already exceeded; unknown or recoverable SLO evidence restores the default
first-error stop. Pending operations retain their proposed duration. Proposed
and effective stop settings, durations and eligibility evidence are observable.
Resource summaries distinguish active backend and database counts.

See [`V2_POLICY_GUARD_RESULTS.md`](agent-memory-design/V2_POLICY_GUARD_RESULTS.md)
for the exact pilot-9 failure, regression checks, frozen diagnostic identity
and limits on interpreting the results. Historical pilot outcomes above remain
attached to their original source capsules.
