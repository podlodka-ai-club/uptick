# Time advancement and diagnostics

`POST /v2/runs/{run_id}/time/advance` requires no Basic Auth. The JSON body contains the outer `request_id`, integer `duration_seconds` at least 300, and optional `stop_when`. The adapter sends UTF-8 JSON with Content-Type application/json; it does not stringify the nested object or put fields in the query.

The live OpenAPI defines stop_when as an object with additionalProperties=false and required new_log_errors, an integer with const=1. Optional error_codes is an array of 1–6 unique strings from SERVER_CAPACITY_EXCEEDED, DB_CONNECTION_LIMIT_EXCEEDED, DISK_FULL, SITE_UNAVAILABLE, DB_UNAVAILABLE, FIREWALL_DENIED. Codes combine with OR; existing logs do not trigger stopping. Unknown or duplicate codes and an empty list are rejected. Both omission of error_codes and an explicit valid list are documented. The response stop reasons are log_error, duration_elapsed, and run_completed. The time boundary is the maximum of requested and real elapsed time, capped by the world end; matching new errors can stop it earlier. Events sharing a timestamp are processed atomically.

The adapter's body construction matches this schema: request_id and integer duration_seconds are top-level fields; stop_when remains a nested JSON object. No extra wrapper is sent. A successful response requires clock, previous_simulation_time, requested_duration_seconds, processed_events, new_logs, and stop_reason; logs_cursor is optional. ErrorResponse requires error and message, permits optional details, and does not require a clock. The adapter preserves any returned details; missing server diagnostics cannot be reconstructed.

Use `time.schema` or `advance_time.schema` for a freshly fetched OpenAPI endpoint and all referenced definitions, including exact constraints and error-code enums. Observe uses the existing `auto` contract cache, with explicit source and age; set `options.time_contract.mode` to `network` for refresh. Failed refreshes retain explicit errors alongside any cached contract. A document hash identifies the version inspected. This exposes the authoritative schema without claiming that a documented server feature has passed a live mutation test.

`{"kind":"advance_time","duration_seconds":300}` is the documented unconditional variant. Omitting stop_when is different from an empty object or null. Its current server behavior must be assessed from its actual response. The adapter never selects this variant automatically following a refusal.

## Diagnosing application of time

New advance_time submissions preserve the exact serialized body, the overview read immediately before submission, the original HTTP status/body, and an overview read after an error. `time.diagnose` with `target_request_id` returns that journal entry, reads a fresh overview, and fetches the current OpenAPI time contract without replaying the POST. Contract retrieval failures remain explicit. It reports replay guarantees and whether a response is cached locally. Application is marked confirmed only when the saved original HTTP 200 response contains all required AdvanceTimeResponse fields; its exact result is returned. An error response or a later overview alone leaves application unknown. Earlier journal entries can lack request bodies and before snapshots; these cannot be reconstructed from a fingerprint.

Compare simulation_time and remaining_seconds across the saved observations. Every run GET can itself advance time. In particular, applied_advance_seconds in a follow-up overview describes that GET, not the failed POST. A changed clock proves that world time changed, but does not identify which request applied it, prove that the whole requested interval ran, or establish rollback. Without a request-correlated successful response or explicit server details, application of an errored advance remains unknown.

A complete HTTP error is delivery=known, with no interval-success evidence. This says the response was received, not that all possible time effects were rolled back. A transport failure is delivery=unknown.

## Replay

The live time endpoint explicitly states that a repeat with the same request_id does not advance time a second time and that normalized stop_when belongs to the idempotent payload. Consequently, an advance whose response was lost can be resubmitted with the identical action and original outer request ID. This guarantee comes from time/advance itself.

A repeated advance_time action with a cached response returns that response locally without another POST, including cached HTTP 500 responses. The adapter retains compatibility with existing journal fingerprints. Do not change stop_when or duration under the original ID or use a new ID to reconcile the earlier request.

The contract does not describe INTERNAL_ERROR rollback or reservation details. Idempotent replay prevents duplicate advancement according to the endpoint contract, but does not itself prove what happened during a failed request. Only a request-correlated successful result or explicit server diagnostics can establish its applied interval. time.diagnose exposes available saved evidence without replay; if the server supplies neither clock nor details and current overview also fails, application remains unknown. No documented variant is thereby proven operational.
