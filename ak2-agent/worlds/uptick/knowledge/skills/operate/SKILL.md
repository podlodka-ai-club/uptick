---
# Resilient read interface: use act {"kind":"catalog.get"} to read the complete
# stable command schema from bootstrap when the network command_catalog source times
# out. For full inventory and historical metrics, follow references/resilient-reads.md;
# exhaust resource/range pagination and never treat partial data as complete.
name: operate
description: Observe and operate Uptick API v2 using documented log filters, credentials, control commands and asynchronous operations.
---

# Operate Uptick

Complete the run with time-based uptime at least 99%, minimizing infrastructure cost. Choose actions from observations. The adapter executes the selected action without choosing infrastructure changes or diagnostic intervals.

Invoke `scripts/adapter.py` with one JSON object on stdin: `operation`, `action`, `request_id`, and optional `options`, `seed`, `task`. Examples below are the object placed directly in `action`; there is no `payload_json` wrapper. For `observe`, use an empty action.

## Explicit time-cycle execution

Use `time_cycle.run` to execute a finite plan selected before invocation: an explicit list of intervals, common `stop_when`, observation options, baseline measurements, required predicates and stopping predicates. The adapter performs a fresh preflight, then each selected advance followed by compact observation and condition checks. Before every subsequent interval within the invocation, it also evaluates `before_advance` predicates against the checked postflight data. A failed predicate stops progression and preserves the confirmed earlier effects. It chooses no interval or infrastructure action.

Read [references/time-cycle.md](../../references/time-cycle.md) for the complete plan schema, predicate syntax, bounds and examples. All six plan fields are required. Existing standalone actions and ordinary observe remain available.

`observe_options.required_sources` may explicitly select from `overview`, `metrics`, `resources`, `inbox`, and `server_inspect`. `overview` is always required. Membership of `metrics` must match `include_metrics`. Supply distinct actual server IDs in `inspect_server_ids` and include `server_inspect` when addressable fresh inspections are required. Omitting `resources` is explicit: no resources GET or cached inventory is used, `resource_inventory.complete=false` remains visible, and selected inspections do not claim to be a complete server list. Every selected inspection is a fresh idempotent `server.inspect` read with a checkpointed child request ID; any refusal, malformed response, or uncertain delivery stops the plan before another time interval. Entries in the predicate context use the stable shape `/servers/{server_id}/result/server/...` for both inventory and explicit inspections. For example, select `/servers/{server_id}/result/server/status` or `/servers/{server_id}/result/server/disk/free_bytes`. An explicit inspection is complete only when the documented result contains the matching server ID, a nonempty status, and a nonnegative integer free-byte count.

Each child advance has a distinct stable request ID saved before submission. Every received result and inbox page is journaled before progression. Overview/resources must be fresh; inbox is paginated until the selected range is exhausted. Missing data, source errors, violated requirements, log-error stopping and world terminal state prevent another interval. Explicit `observe_options.include_metrics=false` omits metrics; a failure of attempted metrics stops execution.

For transient read failures, the model may select `observe_options.read_retry` with `max_attempts` from 1 to 3, `total_budget_seconds` from 1 to 75, and fixed `delay_seconds` from 0 to 30 (default 0). The budget is shared across all read attempts and retry delays in one preflight or postflight. A nonzero delay occurs only after a retryable failed attempt and before the next attempt. Its planned and completed states, elapsed time, charged budget and adjacent attempt numbers are journaled. If the full delay and persistence margin do not fit the retry budget or the invocation's `budget_seconds`, the cycle stops without another read or advance. No delay occurs with one attempt, after success, after the final attempt, or after a non-retryable refusal.

Attempts are sequential and individually journaled. Only transport timeouts/read failures, malformed or incomplete read responses, and HTTP 409 with exact error `CONCURRENT_RUN_REQUEST` are retried; other refusals stop immediately. The adapter never sends advance until all mandatory sources are complete and fresh. Defaults retain one attempt and no delay.

Set `observe_options.include_inbox=false` to send no inbox GET when inbox is not selected in `required_sources` and no predicate uses `/inbox`. Such plans expose inbox as omitted rather than empty. The adapter rejects incompatible plans before any request. The default remains true for existing plans.

Budget exhaustion returns an accepted local continuation with `pending=true`; explicitly select `{"kind":"time_cycle.continue","continuation_id":"RETURNED_ID"}` under a new outer request ID. Replaying an invocation with a definite saved response returns that response without extending execution. For `delivery=unknown`, replay the identical action with the original outer ID; the unresolved child retains its original payload and request ID. When its definite acceptance and `operation_id` were saved, recovery polls that operation without resending `time/advance`. Recovery yields before another interval. A refusal or condition stop cannot be bypassed with continue.

Use `{"kind":"time_cycle.get","continuation_id":"RETURNED_ID"}` for a historical checkpoint and add `record_index` to read one full journal record, following `next_record_index`. These reads never execute the plan. Ordinary observe exposes compact plan status and retained advance evidence with original identities. Finishing a plan does not set world done unless the API explicitly reports terminal state.

Run `python3 scripts/test_time_cycle.py` together with the existing transport tests for isolated validation; the tests do not access the live simulator.

## Compact ordinary observation

Ordinary `observe` returns fresh overview, metrics, inbox pagination, source clocks, diagnostics, active work, completion and evidence, but does not repeat stable OpenAPI fragments, the full command catalog, complete credential metadata, or every persisted terminal operation payload. `data.separate_reads` links to schema and catalog actions. `data.operation_records.terminal_operation_ids` links each historical result to `operation.get`; terminal results retain their original identity and remain eligible for evidence output. Inbox messages are compact summaries with credential references, returned count and opaque continuation action; use `inbox.get` for the complete selected page. A missing or partial live source remains in `source_status`, diagnostics and the envelope error.

## Bounded resources observation

When ordinary observation reports an incomplete resources body, request a standalone bounded recovery read:

```json
{"kind":"resources.get"}
```

It performs up to three fresh read-only GET attempts and returns on the first fully decoded, structurally validated inventory. It never uses a cached snapshot, returns partial server data, executes a mutation, or treats partial JSON as complete. The successful response preserves the source clock, all required inventory/server fields, retrieval freshness, server count, attempts made, and diagnostics for every earlier incomplete attempt.

Optional sibling settings are `max_attempts` (1–3, default 3), `timeout_seconds` (1–20, default 15), `budget_seconds` (1–25, default 20), and `max_response_bytes` (1024–2097152, default 2097152). Timeout must not exceed the per-attempt budget, and their configured aggregate budget is capped at 75 seconds. Each attempt uses exactly one GET through the shared run gate. If none returns a complete valid response, delivery is `read_failed`, evidence is empty, and `data.resources_fetch.attempt_diagnostics` plus the error preserve the framing, received-byte count, parse stage, timeout and timing of every attempt. A decoded HTTP refusal is definite (`known`) and is not retried. See `references/resources.md`.

Outer `options.resources` independently configures resources transport:

```json
{"operation":"observe","action":{},"request_id":"observe-resources","options":{"resources":{"timeout_seconds":3,"budget_seconds":4,"max_response_bytes":1048576}}}
```

These are the defaults. Socket timeout accepts integers 1–30 seconds; total request budget accepts integers 1–45, with timeout no greater than budget. The byte cap accepts integers 1024–2097152. Resources has no documented query parameters or pagination. One GET is attempted, without retry or snapshot substitution, after the other selected sources and operation checks. Its failure therefore cannot delay overview, inbox, metrics or active-operation HTTP requests. Their age at final assembly still includes the resources wait.

Observation now executes HTTP jobs serially on the main thread through the existing run gate. A timer bounds each job's gate wait, headers, body and validation. All jobs share a 100-second collection allowance from entry to observe, leaving time for persistence and output; exhausted jobs report explicit errors without sending. Existing source options, metrics omission, filters, operation batch selection, terminal result caching and evidence pagination remain supported.

The shared reader uses one `httpx.Client` per adapter process and reuses its connections across requests. HTTPX validates Content-Length/chunked framing; the adapter checks the byte limit while streaming and decodes JSON once, after the complete HTTP body. A correctly framed keep-alive response does not require connection closure. `http_call.framing`, `json_complete`, `response_bytes`, `read_chunks`, parse-error metadata, stage, HTTP duration and gate wait distinguish observed transport facts. Partial or structurally invalid resources never populate `data.resources`. Current failures remain in `source_status` and `OBSERVATION_INCOMPLETE`; their metadata is also saved privately as `last_resources_read`. A timeout after HTTP 200 does not establish its cause.

Run `python3 scripts/test_resources_read.py` and `python3 scripts/test_logs_read.py` for local transport checks, then one observe with explicit resource limits for live validation. These checks require no infrastructure mutation, probe or selected time interval. Ordinary world GETs still account for real elapsed time. See `references/resources.md` for framing semantics, validation scope and diagnostics.

## Optional automatic metrics

Set outer `options.include_metrics=false` to exclude metrics from this observation:

```json
{"operation":"observe","action":{},"request_id":"observe-current","options":{"include_metrics":false}}
```

The default is `true`, preserving existing callers. The boolean setting is per invocation and does not disable `metrics.get`. Omission appears in `data.source_status.metrics` as `source=omitted`, `fresh=false`, `complete=false`, `network_attempted=false`. There is no cached snapshot substitution: `data.metrics` is absent and envelope `metrics` is empty. Explicit omission alone does not create a transport error; inspect source completeness. Overview, resources, inbox, selected active operations, terminal operation caching and evidence pagination continue normally.

Both metrics interfaces use the same public GET, query serialization, run gate and incremental JSON reader. Automatic metrics uses one attempt and observation source limits (default socket 10 s, transport budget 12 s); standalone `metrics.get` retains its existing two-attempt transport (socket 25 s, budget 30 s per attempt). A standalone success does not prove an automatic timeout is a client defect. Source `elapsed_seconds` includes gate waiting; `http_call.gate_wait_seconds` separates it from HTTP `elapsed_seconds`. `http_call` also reports limits, path, body completeness and failure stage. Standalone metrics exposes the same record at `data.metrics_fetch`, or in its structured error, plus actual transport retries. DNS, connection and header waiting remain a combined stage.

Metrics query `names` arrays are serialized as one comma-separated value, as required by form/explode=false; existing comma-separated strings remain supported. With neither `from` nor `to`, the endpoint returns a current snapshot; for a period provide both. No interval is selected automatically. Existing `options.metrics_params` remains supported. See the live OpenAPI for metric names and time-series constraints.

## Logs and other reads

For aggregated traffic/DDoS diagnosis over an explicitly selected completed interval, use:

```json
{"kind":"logs.summary","params":{"from":"RFC3339_START","to":"CURRENT_OR_EARLIER_SIMULATION_TIME","group_by":"source_cidr","limit":100,"offset":0,"ipv4_prefix_length":24,"ipv6_prefix_length":64}}
```

`from`, `to`, and `group_by` are required. `group_by` is one of `source_ip`, `source_cidr`, `user_agent`, `region_code`, `page`, or `status`. Optional filters are `source_ip`, `source_cidr`, `user_agent`, `region_code`, `page`, `status`, `has_error`, `error`, and `firewall_rule_id`. Prefix lengths are valid only with `group_by=source_cidr`. The action performs exactly one summary GET and never classifies traffic or chooses a firewall action.

A valid response preserves `clock`, `groups`, `total_requests`, `total_groups`, and `next_offset`. `page_complete=true` means the returned aggregation page is valid; `complete=true` only when `next_offset` is null. Otherwise invoke the returned `continuation_action`, preserving the same interval and filters. `limit` and `offset` paginate groups, not requests. Read failure returns `delivery=read_failed`, empty evidence, `fresh=false`, and transport diagnostics. See `references/logs.md`.

For a narrow, current error-attribution read, use the bounded projection interface:

```json
{"kind":"logs.recent","params":{"from":"SIMULATION_TIME","to":"SIMULATION_TIME","error":"SERVER_CAPACITY_EXCEEDED","limit":50}}
```

`from`, `to`, exact `error`, and integer `limit` 1–50 are mandatory server filters. Optional documented filters are `cursor`, `status`, `has_error`, `source_ip`, `source_cidr`, `user_agent`, `region_code`, `firewall_rule_id`, and `page`. The adapter sends one GET and returns only `timestamp`, `error`, `source_ip`, `source_cidr`, `user_agent`, `region_code`, `firewall_rule_id`, and `status` for each record. It never classifies an address as hostile or selects a firewall action.

A successful response reports `page_complete=true`, retrieval freshness, `cursor`, and `has_more`. `complete=true` means `next_cursor` is null and the selected filtered range is exhausted; otherwise follow `continuation_action`, preserving every filter and changing only the opaque cursor. A failed or malformed read returns `delivery=read_failed`, no evidence, `complete=false`, `page_complete=false`, and explicit transport diagnostics. Missing fields remain null and must not be inferred. See `references/logs.md`.

Read one page; the existing action format remains supported:

```json
{"kind":"logs.get","params":{}}
```

Optional transport limits belong beside `params`, not inside the query:

```json
{"kind":"logs.get","params":{"limit":200},"timeout_seconds":10,"budget_seconds":15,"max_response_bytes":1048576}
```

`timeout_seconds` is the socket timeout (default 25, integer 1–60). `budget_seconds` is the total wall-clock budget (default 30, integer 1–60); socket timeout must not exceed it. The main-thread Unix timer covers schema loading, any required OpenAPI fetch, query validation, waiting for the shared per-run gate, HTTP headers, body reading and response validation. Saving diagnostics and emitting JSON occur after this timer, within the runtime's overall invocation allowance. `max_response_bytes` bounds the logs HTTP body (default 1048576, integer 1024–2097152); this cap leaves room for diagnostics within the runtime output limit.

There is at most one logs GET and no automatic retry. The existing shared `Client.get` gate serializes it with other requests to this run. A missing local OpenAPI document may require one additional public GET, within the same total budget. Filters are forwarded unchanged, including `from`, `to`, `cursor` and `limit`. The adapter chooses no interval and follows no cursor automatically.

The response keeps `data.clock`, `data.logs` and `data.next_cursor`. `data.logs_fetch` adds requested filters, limits, source, freshness, HTTP status, duration, stage, returned count and `has_more`. `complete=true` describes a valid returned page, not exhaustion of the query or freshness of its historical records. The source clock remains authoritative.

Failures retain `error.stage`, exception type, elapsed time, requested filters and `error.http_call`. Transport stages distinguish `prepare_request`, `connect_or_headers` (DNS/connect/TLS and header wait are combined), `read_body`, `parse_json`, and `response_size`; gate wait and schema/validation failures are also identifiable. A complete decoded HTTP refusal retains its body in `data` and `error.response`. An incomplete or invalid body is diagnostic only: at most 8192 prefix bytes are returned with completeness/truncation metadata, never as an accepted logs page. Missing logs are not an empty result.

JSON is parsed once after HTTPX confirms completion of the HTTP body. Missing bytes or a missing final chunk are transport failures even if the received prefix is valid JSON. HTML, partial JSON, oversized responses and pages missing required fields produce errors. A valid page or complete decoded HTTP refusal has `delivery=known`; rejection before the logs HTTP attempt has `not_sent`. A lost or invalid page has `delivery=read_failed`: logs.get only reads historical records and starts no operation. This returns explicit diagnostics with `complete=false`, `fresh=false`, `evidence=[]`, `pending=false`, `done=false` and `success=null`. The runtime ends this failed read attempt and returns control to the model, which selects any next request and its parameters. Recovery does not change the original interval, filters, limit or transport settings, infer a cursor, or accept diagnostic bytes as a page. The latest read metadata, error and delivery are saved privately as `last_logs_read`; this does not reserve request IDs or cache a logs page.

These new limits apply to standalone `logs.get`. Optional logs collection during `observe` retains the existing observation source limits. For bounded verification, run `python3 scripts/test_logs_read.py`, then invoke a single `logs.get` with caller-selected documented filters and explicit limits. Report its actual diagnostics even if the external timeout persists. These checks do not establish infrastructure success.

Continue with the returned opaque cursor:

```json
{"kind":"logs.get","params":{"cursor":"RETURNED_NEXT_CURSOR"}}
```

Read infrastructure errors:

```json
{"kind":"logs.get","params":{"has_error":true,"limit":1000}}
```

Read a specific infrastructure error:

```json
{"kind":"logs.get","params":{"error":"SERVER_CAPACITY_EXCEEDED","limit":1000}}
```

The confirmed query names are `from`, `to`, `cursor`, `page`, `status`, `has_error`, `error`, `source_ip`, `source_cidr`, `user_agent`, `region_code`, `firewall_rule_id`, and `limit`. `from` and `to` are inclusive simulation date-time bounds. `limit` is 1–1000, default 100. Filters combine with AND. `error` is incompatible with `has_error=false`; `user_agent` matches a case-sensitive substring.

Read current schemas with:

```json
{"kind":"logs.schema"}
```

The same contract is available in `observe.data.logs_query_contract`. Each parameter retains its YAML definition. The `references` mappings expose referenced definitions, including allowed enum values and formats. The adapter validates parameter names; the API validates values and combinations. Do not invent aliases. See `references/logs.md` for the confirmed protocol.

To read an event and its preceding context, select `from` and `to` around its simulation timestamp without a cursor positioned after that event. Follow returned `next_cursor` values with filters as required by the API. Never decode or decrement cursors. Each call reads one page. A first page or empty filtered page does not establish complete current state. Remove error filters when successful requests are also needed.

Default `observe` omits historical unfiltered logs. Supply outer `options.logs_params` to include a chosen logs page. Each response retains its source clock; observations from different endpoints are not an atomic snapshot.

Additional read-only actions:

```json
{"kind":"inbox.get","params":{}}
```

```json
{"kind":"metrics.get","params":{}}
```

These queries also use their live OpenAPI parameter definitions. Follow inbox `next_cursor` when additional messages remain, including credential rotations. HTTP errors preserve the server body in `data` and `error.response`, with `error.http_status`. GET reads produce no success evidence.

## Control commands and credentials

```json
{"kind":"command","command":"server.inspect","params":{"server_id":"ACTUAL_SERVER_ID"}}
```

The live authenticated catalog in `observe.data.command_catalog` supplies all 18 commands, schemas, execution modes and authentication requirements. Always include `params`, even when empty.

Read the catalog independently with this action:

```json
{"kind":"command_catalog.get","timeout_seconds":45,"budget_seconds":55,"max_response_bytes":4194304}
```

This performs exactly one authenticated `GET /v2/runs/{run_id}/control/commands`, sequentially, without fetching other endpoints or retrying automatically. Basic Auth is resolved privately from bootstrap. The outer request ID remains required by the adapter envelope, but this GET does not reserve it or enter the mutation journal. Repeating this action requests a fresh catalog.

All settings are optional. `timeout_seconds` is the socket timeout, default 45; `budget_seconds` is a separate wall-clock budget covering the entire HTTP request, default 55. Both are integers from 1 to 60, and the timeout cannot exceed the budget. The Unix runtime enforces the total budget with a process-local alarm. `max_response_bytes` defaults to 4194304 and accepts integers from 1024 to 16777216. JSON is read incrementally and returned immediately when complete; incomplete JSON, HTML and oversized responses are errors.

The full decoded response, including command schemas, is returned at `data.command_catalog`. `data.catalog_fetch` reports source (`live`, `cache`, or `none`), `fresh`, UTC `fetched_at`, `age_seconds`, whether a network request was attempted, configured limits, and request timing. Fresh means obtained during this invocation, not an atomic snapshot with other sources. Catalog reads produce no evidence of successful infrastructure changes.

A successful catalog is persisted privately for this run and origin. To read only that saved response:

```json
{"kind":"command_catalog.get","mode":"cache"}
```

A cached response always has `fresh=false`. Cache-only mode never sends HTTP and returns `CATALOG_CACHE_MISS` when absent. On a failed network fetch, any previous catalog is returned explicitly as cache alongside the current structured error. An HTTP refusal preserves `error.http_status` and `error.response`; transport errors preserve the exception type and message. A cached catalog does not clear or hide a failed refresh.

Default `observe` uses catalog mode `auto`: a saved catalog matching the current run and origin is returned without a network request. When no matching cache exists, it attempts one bounded fetch using the observation source defaults (10-second socket timeout, 12-second catalog budget), unless overridden through outer `options.catalog`. The standalone `command_catalog.get` action retains its 45-second socket timeout and 55-second total budget defaults. Cached results retain their original clock and retrieval timestamp, with `fresh=false`, updated `age_seconds`, and `network_attempted=false`. `data.catalog_fetch.requested_mode` and `effective_mode` show the selection.

No outer options are needed for this default. The action `{"kind":"command_catalog.get"}` still explicitly refreshes from the network; its default mode remains `network`. Both the action and outer `options.catalog` accept `mode` values `auto`, `cache`, and `network`. Cache-only mode never fetches, even on a cache miss. Configure observation limits or force refresh with outer `options.catalog`, for example `{"catalog":{"mode":"network","timeout_seconds":50,"budget_seconds":60}}`. Failed fetches retain structured diagnostics and any available cached catalog.

Every observation attempts overview, inbox, enabled metrics, the optional selected logs page, command catalog and selected unconfirmed-operation checks before resources. Jobs execute serially on the main thread, retaining the shared per-run gate and bounded timers described above. Their HTTP exchanges do not overlap. Each live source gets at most one bounded HTTP attempt; collection-budget exhaustion remains explicit. Defaults are 10 and 12 seconds; set outer `options.source_timeout_seconds` (1–30) and `options.source_budget_seconds` (1–45), with timeout not exceeding budget. `options.metrics_params` and `options.inbox_params` select documented query ranges or cursors; `options.logs_params` selects the logs page.

To prevent a legacy backlog from delaying current state, observe polls at most eight nonterminal or unconfirmed operation IDs per invocation. Known active operations are prioritized, then the least recently checked IDs. Set outer `options.operation_poll_limit` to an integer from 1 to 32. Deferred IDs retain their last confirmed payload and age when available; otherwise their source is explicitly incomplete. Repeated observations progressively confirm the backlog.

`data.source_status` reports each source’s network/cache/omitted origin, completeness, freshness, HTTP status, elapsed time, simulation clock, returned cursor, requested parameters, and age at assembly where applicable. A missing or timed-out source remains an explicit diagnostic and is never interpreted as empty or healthy. Catalog cache metadata independently reports its retrieval age.

Only pending, nonterminal, or legacy-unconfirmed operation IDs are checked remotely. Confirmed terminal operations retain their cached results and stable evidence identities without another GET. `data.operation_tracking` distinguishes remotely checked IDs from cached terminal IDs and records per-operation source metadata. Active operations remain fresh; cached terminal results are historical.

Observe learns credential IDs and returns their metadata but does not automatically retrieve every secret mentioned by a long inbox. Use `credential.get`, or select a credential in a server-authenticated command; the adapter then obtains it privately on demand. This avoids turning paginated inbox observation into many sequential authenticated calls.

`data.execution_stats.last_observation_seconds` records elapsed wall-clock seconds for observation collection, measured with a monotonic clock through result assembly, before final state persistence and JSON output. The value is also saved in the private state under `execution_stats.last_observation_seconds`.

For server-authenticated commands:

```json
{"kind":"command","command":"disk.cleanup","params":{"server_id":"ACTUAL_SERVER_ID"},"credential_id":"ACTUAL_CREDENTIAL_ID"}
```

Use `credential_id` for `database.create`, `database.inspect`, `database.backup`, `database.restore`, `disk.usage`, and `disk.cleanup`; omit it for other commands. The adapter obtains and privately supplies target credentials. Control-panel Basic Auth comes separately from bootstrap.

```json
{"kind":"credential.get","credential_id":"ACTUAL_CREDENTIAL_ID"}
```

Only metadata and availability are exposed. Check server identity, version and expiry. Following `TARGET_UNAUTHORIZED` or `CREDENTIALS_EXPIRED`, obtain the current credential and retry the same command and parameters with its original outer request ID. The credential reference may change because authentication is excluded from command idempotency comparison.

## Simulator request diagnostics

Inspect documented read-only diagnostic sources:

```json
{"kind":"request_diagnostics.schema"}
```

Retrieve historical transport diagnostics and, when documented, server processing information for an original request:

```json
{"kind":"request_diagnostics.get","source_request_id":"ORIGINAL_REQUEST_ID"}
```

The original request ID belongs in `source_request_id`; the outer envelope still requires its own request ID. The run ID is resolved from bootstrap. This action never resends the original mutation. Its local journal record preserves the saved refusal body, transmitted parameters and available headers. Journal `completed` describes receipt of a response, including a refusal, and does not establish successful server processing. Missing historical timestamps or HTTP metadata remain unknown.

The adapter checks `/openapi.yaml` for GET endpoints accepting both run and request identities. If exactly one exists, it reads that source. With multiple candidates, select its documented `endpoint`; supply any remaining documented `path_params` and query `params`. Each call reads one page. Follow returned cursors with the documented range and filters. The adapter supplies supported Basic Auth privately and preserves access errors.

Both actions accept `mode: network` (default), `auto`, or `cache`. Observation exposes discovery under `data.request_diagnostic_sources` in auto mode. Source metadata distinguishes live documentation from cache and preserves retrieval time, hash, age, completeness and failed refreshes. An unavailable document does not establish absence of a source. Each GET uses one attempt, an 8-second socket timeout, a 12-second transport budget, a 16-second main-thread wall-clock cap and a 1 MiB response limit.

`availability=no_documented_direct_lookup` means no GET accepting both identities was found in the inspected contract. The returned GET inventory remains available for reviewing other documented sources. Site request logs are not simulator control-request traces. Without a documented cause or processing status, generic `INTERNAL_ERROR` remains unexplained; do not infer advancement, rollback or a duration constraint from it. These reads emit no success evidence and do not change durations or infrastructure. See `references/request-diagnostics.md`.

## Time and operations

`{"kind":"time.schema"}` is a compatible alias for the current time contract, defaulting to network refresh. After an advance refusal or lost response, use `{"kind":"time.diagnose","target_request_id":"ORIGINAL_REQUEST_ID"}`. It reads the saved journal, one bounded fresh overview and the current time contract without replaying the POST. Original refusals remain visible; unavailable sources produce explicit errors. New advances preserve the serialized JSON, a before overview, original response and transport metadata; HTTP refusals also capture an after overview. Historical entries may lack these fields.

Application is confirmed only by a saved valid successful time response. Later overview clocks cannot attribute advancement to an earlier POST: GETs themselves advance time. The documented endpoint guarantees that identical replay with the original request ID does not advance time twice, but specifies no INTERNAL_ERROR rollback behavior. Cached responses, including HTTP 500, replay locally. Lost responses may be recovered with the identical action and original outer ID. Never change its interval or stop condition to reconcile it. The documented GET inventory provides no direct lookup by original request ID; request_diagnostics remains available for journal inspection and documentation discovery.

`stop_when` requires integer `new_log_errors=1`; optional `error_codes` contains 1–6 unique exact codes: `SERVER_CAPACITY_EXCEEDED`, `DB_CONNECTION_LIMIT_EXCEEDED`, `DISK_FULL`, `SITE_UNAVAILABLE`, `DB_UNAVAILABLE`, `FIREWALL_DENIED`. Extra fields are forbidden. Omitting `stop_when` entirely is a documented unconditional variant already supported by `advance_time`; documentation alone does not prove live server execution. The adapter never selects it automatically after refusal. See `references/time.md`.

Read the full current time-advancement contract without advancing time:

```json
{"kind":"advance_time.schema"}
```

This performs one public GET of `/openapi.yaml`. `data.advance_time_contract` contains the complete endpoint, request body, request schema, nested `stop_when`, duration constraints, responses and recursively referenced schemas in their original YAML. `transport_check` compares wire field names and required fields with the actual document; it does not claim full JSON Schema validation. Inspect its verdict and the duration constraints before interpreting a refusal as a parameter problem.

The action defaults to a network refresh. Optional `mode` accepts `network`, `auto`, or `cache`. `data.contract_fetch` exposes source, freshness, retrieval timestamp, age and document hash. Failed refreshes retain diagnostics alongside any cached contract. Observe uses `auto`, returning `data.advance_time_contract` and `data.time_contract_fetch`; outer `options.time_contract` accepts the same mode setting. See `references/time-advance.md`.

For newly submitted POST refusals, `error.http_call` preserves the actual redacted JSON body, method/path, available diagnostic response headers, response completeness and HTTP duration. `error.response` preserves the full decoded redacted refusal body. Saved journal replies retain those original diagnostics. Earlier replies may lack headers or timing; the adapter does not send a mutation to reconstruct missing metadata.

An HTTP refusal supplies no successful advancement evidence. Without a returned clock, do not infer either advancement or no advancement. The adapter does not automatically retry a refused POST, shorten its duration, split it, or change infrastructure. Transport uncertainty remains `delivery=unknown` with the original request ID. A successful HTTP response lacking a valid advancement clock also produces a diagnostic without success evidence.

```json
{"kind":"advance_time","duration_seconds":300}
```

```json
{"kind":"advance_time","duration_seconds":3600,"stop_when":{"new_log_errors":1,"error_codes":["SERVER_CAPACITY_EXCEEDED"]}}
```

The minimum duration is 300 seconds. The endpoint first returns HTTP 202 with an operation ID; poll that operation until `succeeded` or `failed`. Simulation time advances only through this accepted operation, not through ordinary reads or real thinking time. Use the terminal result's clock, stop reason and event fields when choosing subsequent observations.

```json
{"kind":"operation.get","operation_id":"ACTUAL_OPERATION_ID"}
```

HTTP 202 means acceptance, not completion. Wait for `succeeded` before dependent actions; evaluate `failed` results. Active operations are read at the source. Confirmed terminal results are persisted and returned with the same `operation:ID` evidence identity without further remote polling. Old tracked operations without cached results are read to obtain them. Cached completion data is historical; inspect resources for their current state.

## Probes

```json
{"kind":"probe","params":{"page":"product_list"}}
```

Read `observe.data.probe_contract_yaml` and `probe_contract_references` for request and response schemas. The adapter adds the outer request ID. A probe is an explicit POST diagnostic request that creates simulated load and a log entry. It is never automatically executed by `observe`.

A measured probe produces `kind=effect` evidence with stable identity `probe:ORIGINAL_REQUEST_ID`. Page `status=200` means `outcome=success`; page statuses `403`, `500`, and `503` mean `outcome=failure`, even when the endpoint returns HTTP 200. The complete response is retained in `detail`, including its original clock, page, product ID when present, latency, load, error and other diagnostics. This establishes the individual test result, not an improvement in overall uptime.

`observe` also returns historical probe evidence recovered from completed responses in the private request journal. Original fingerprints verify that these records came from probe actions. Recovery and replay use saved responses without sending new probes; identities and measurement clocks remain unchanged. Records without a valid measured result produce no inferred outcome. Ordinary GET reads, catalogs and command inspections remain observations without test-success evidence.

## Delivery and completion

Preserve the outer request ID for every replay of the same mutation. New commands, including fresh command reads, require new IDs. All existing action formats remain supported, as does the private request journal.

`delivery=known` means a definite response, including an API refusal. `not_sent` means rejection before submission. `unknown` means the mutation may have executed: preserve its exact payload and original request ID for replay. Never replace an unknown action with a different one. For documented read-only `logs.get`, `read_failed` instead means that no complete valid page was obtained; it terminates the failed attempt with explicit diagnostics and no evidence. The model chooses any subsequent read. This exception does not apply to probes, time advancement or infrastructure mutations.

Evidence output is limited to 64 events per invocation. Outer `options.evidence_limit` may select a smaller page size (integer 1–64). `data.evidence_page` reports the limit, total available distinct events, returned count, current cursor, `next_cursor`, `has_more`, and whether traversal is automatic. Counts describe evidence available to this response, not all events in the world's history.

Default `observe` advances a private traversal cursor through events sorted by stable identity. After the last page, it starts another cycle on the next observation. Saved events remain eligible for repeated output; advancing this cursor never marks them delivered or deletes them. Terminal operation results remain cached and do not require renewed remote polling.

For explicit pagination, supply outer `options.evidence_cursor` using the returned `next_cursor`. Pass an empty string to start from the beginning. Explicit cursors do not change automatic traversal state. `next_cursor=null` means that this traversal has reached its end; omit the option to resume automatic observation, or pass an empty string to restart explicitly. New events can appear between calls, so traversal is not a frozen snapshot. Repeated explicit reads can recover a page if a process ended before runtime saved its output. Act responses begin at the first page unless an explicit cursor is supplied. Complete journal responses and evidence identities remain unchanged.

Reads and command inspections are observations, not infrastructure success. Repeated terminal evidence is intentional; runtime deduplicates stable identities. API success does not establish improved uptime. `done` requires explicit terminal world status; `success` remains null unless the world reports a boolean evaluation.

Missing sources appear in diagnostics and must not be treated as healthy or empty. Read `references/protocol.md` and `prompts/world.md` for world rules and completion criteria.
