# Time advancement contract and diagnostics

`POST /v2/runs/{run_id}/time/advance` is public. The adapter sends one JSON body containing the outer `request_id`, the selected `duration_seconds`, and optional `stop_when`. It forwards `stop_when` unchanged. The endpoint returns HTTP 202 with an `operation_id`; that operation must be polled until `succeeded` or `failed`, and its terminal `result` contains the advancement result. The existing local validation requires an integer duration of at least 300 seconds and an object for `stop_when`; it imposes no maximum, splits no interval, and performs no automatic POST retry.

## Authoritative schema

Use `{"kind":"advance_time.schema"}` to fetch the current public `/openapi.yaml` with one bounded GET. This action does not call the time endpoint. Its response includes the complete endpoint YAML, JSON request body and request schema, duration and nested stop-condition schemas, all response definitions, and recursively referenced local schemas and parameters. Constraints, enums, descriptions and response examples remain in their original YAML.

`transport_check` compares the adapter's wire field names and required fields with the fetched schema. `wire_fields_match` covers that comparison only; it is not full JSON Schema validation or proof that the server successfully executes every valid duration. Duration constraints are reported verbatim alongside the adapter's local checks. The model must inspect any mismatch or additional constraints. The API remains responsible for full value and combination validation.

The documented start response describes `stop_when.new_log_errors` and optional exact `error_codes`; matching codes combine with OR. The requested interval is compared with real elapsed time. Stopping reasons are `log_error`, `duration_elapsed`, and `run_completed`. Consult the fetched contract for exact types, bounds, required fields and response schemas rather than inferring them from examples or execution failures.

## Cache and freshness

The schema action defaults to `mode: network`. `mode: cache` reads only a previously validated extracted contract; `mode: auto` uses that cache or performs one GET when absent. Observation uses `auto`; outer `options.time_contract.mode` can select another mode. The cache is scoped to the current origin and stored privately in the run directory.

`contract_fetch` reports requested/effective mode, source URL, live/cache origin, retrieval timestamp, age, document SHA-256 and network attempt. A failed refresh preserves an available cached contract together with the new error. Missing documentation never appears as a successful empty schema. Legacy log and probe schema interfaces remain supported.

## Refusals and transport uncertainty

For newly sent POST actions, HTTP refusal diagnostics retain `error.http_status`, the complete decoded and redacted JSON response in `error.response`, and `error.http_call`. The latter records method, path, actual redacted JSON request body, available diagnostic response headers, body completeness and elapsed HTTP time. Diagnostic headers include request/correlation/trace identifiers, retry timing and content metadata. Authorization and cookie headers are excluded. These diagnostics persist with the action's journal response and retain their original timing on replay.

Transport exceptions retain available HTTP metadata and report `delivery: unknown` when submission may have occurred. Invalid or incomplete JSON is a protocol error. A decoded HTTP refusal has `delivery: known`, emits no successful advancement evidence, and is not automatically retried. A refusal without a clock establishes neither advancement nor absence of advancement. Only subsequent world observations can establish current simulation time.

Successful advancement evidence requires a successful response containing a simulation clock and a nonnegative numeric `applied_advance_seconds`. A malformed success response produces an explicit diagnostic without success evidence. No error triggers automatic interval changes or infrastructure actions.

Older saved responses may lack HTTP headers and timing because earlier transport versions did not collect them. Such metadata cannot be reconstructed by replaying a mutation and must not be invented.
