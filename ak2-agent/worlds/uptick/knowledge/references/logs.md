# Logs reading contract

`GET /v2/runs/{run_id}/logs` is public and read-only. It returns already occurred site requests and probes in chronological order, including firewall denials and requests during site shutdown. The response preserves `clock`, `logs`, and `next_cursor`. Traffic IP, region and user-agent describe simulated visitors, not the panel client. Logs do not label attacks and do not include the agent audit log. Disk cleanup does not delete this history.

## Query parameters confirmed by OpenAPI

All filters combine with AND.

| Parameter | Contract |
| --- | --- |
| `from` | Inclusive start in simulation time; date-time string |
| `to` | Inclusive end in simulation time; date-time string |
| `cursor` | Opaque cursor from a previous response; maximum 512 characters |
| `page` | `PageType` schema |
| `status` | `PageRequestStatus` schema |
| `has_error` | Boolean selecting requests with or without infrastructure errors |
| `error` | Exact `RequestFailureCode`; incompatible with `has_error=false` |
| `source_ip` | `IPAddress` schema |
| `source_cidr` | `NetworkCIDR` schema |
| `user_agent` | Case-sensitive substring, 1–2048 characters |
| `region_code` | `RegionCode` schema |
| `firewall_rule_id` | `ResourceId` schema |
| `limit` | Integer 1–1000; default 100 |

Use `{"kind":"logs.schema"}` to read the current parameter definitions. `parameters[].openapi_yaml` retains each parameter's name, location and schema. `references` maps local OpenAPI reference identifiers to their original YAML definitions, including enum values and formats. A reference inside a parameter's schema does not replace the parameter itself.

## Calls and pagination

Use `{"kind":"logs.get","params":{}}` for one page. Put documented filters directly in `params`. To select infrastructure errors, use `{"kind":"logs.get","params":{"has_error":true,"limit":1000}}`. To select a particular error use `error` with an exact documented code.

For a bounded interval set `from` and `to` to the chosen simulation timestamps. Each call reads one page. Follow `next_cursor`, preserving filters according to the live contract, until the desired interval is covered. A first page or empty filtered page does not establish complete current state. Cursors are opaque; do not decode or decrement them. A cursor after an event cannot retrieve that event or preceding context; start a bounded interval without that cursor when earlier records are needed.

The adapter validates query names locally and preserves server validation errors in `error.http_status`, `error.response`, and `data`. Reads produce no success evidence. `observe` reads logs only when outer `options.logs_params` is supplied. Range selection and pagination choices belong to the caller.

## Standalone logs.get transport

The legacy `{kind: logs.get, params: {...}}` interface accepts optional sibling fields `timeout_seconds`, `budget_seconds` and `max_response_bytes`. Socket timeout defaults to 25 seconds and total budget to 30; both accept integers 1–60, with timeout no greater than budget. The response limit defaults to 1048576 bytes and accepts integers 1024–2097152. These are adapter transport settings, not HTTP query parameters.

A main-thread Unix timer bounds schema access, optional single OpenAPI GET, validation, per-run gate wait, logs HTTP exchange and response validation together. The logs endpoint receives at most one GET per invocation, through the existing shared gate, without automatic retry. The shared HTTPX client streams the body with a byte limit and parses JSON once after validated HTTP completion. Correct Content-Length/chunked framing permits connection reuse; an incomplete body or missing final chunk is rejected even if its prefix is valid JSON. Saving diagnostics and JSON output follow the bounded read.

Successful pages retain top-level `clock`, `logs`, and `next_cursor` in `data`. Additional `logs_fetch` metadata records actual filters, limits, source, HTTP status, duration, stage, freshness, returned count, and whether the server supplied continuation. `complete` means a valid page was received, not that all matching history was read. No historical logs cache substitutes for the requested read.

Failure diagnostics distinguish schema/validation, gate waiting, request preparation, combined connection/header waiting, body reading, JSON parsing and size rejection. The stdlib opener does not separately identify DNS, TCP, TLS and header-wait failures. Complete decoded refusals retain the HTTP body; malformed or incomplete bodies retain only a redacted diagnostic prefix of at most 8192 bytes, with truncation and completeness metadata. Such bytes never establish an empty or valid logs page. `error.http_call` retains the available HTTP status, diagnostic headers, request path and transport timing.

A valid page or complete decoded HTTP refusal uses `delivery=known`; rejection before the logs HTTP attempt uses `not_sent`. A lost, incomplete, oversized or structurally invalid page uses `delivery=read_failed`, because this documented endpoint only reads historical records and starts no operation. It returns an explicit error, `complete=false`, `fresh=false`, `evidence=[]`, `pending=false`, `done=false` and `success=null`. Diagnostic body bytes never substitute for a complete page. This ends the failed read attempt; the decision loop selects any further read with a new request ID. Recovery preserves the original filters, interval, limit and transport settings and performs at most one logs GET. No GET produces success evidence. The latest metadata, error and delivery are stored privately under `last_logs_read`, separately from mutation idempotency records. The adapter does not replay a read merely to recover its diagnostics. Existing `observe` logs collection continues to use observation source settings.

Terminal asynchronous operation results are separately persisted and repeated with their original evidence identities. They describe historical completion, not current resource state.

## Aggregated traffic summary

`logs.summary` reads one page from the public read-only `GET /v2/runs/{run_id}/logs/summary` endpoint. It requires `from`, `to`, and `group_by`; `to` must not exceed the current simulation time. Supported grouping keys are `source_ip`, `source_cidr`, `user_agent`, `region_code`, `page`, and `status`. The common filters are `source_ip`, `source_cidr`, `user_agent`, `region_code`, `page`, `status`, `has_error`, `error`, and `firewall_rule_id`; all combine with AND. `error` is incompatible with `has_error=false`.

`limit` is an integer from 1 through 1000 and `offset` is a nonnegative integer. They paginate the sorted group list only. Preserve the exact window and filters and replace only `offset` with `next_offset`; null means the aggregation is exhausted. The totals describe the complete filtered selection, not merely the returned page. `cursor` is not supported.

For `group_by=source_cidr`, optional `ipv4_prefix_length` and `ipv6_prefix_length` select masks from 0–32 and 0–128. They are rejected for other grouping modes. Groups contain the server-observed `key`, nonnegative `requests`, and nonnegative `unique_ips`. Counts and grouping are facts about traffic, not a classification of attackers.

The action makes at most one summary GET. Optional sibling transport settings are `timeout_seconds` (1–60, default 25), `budget_seconds` (1–60, default 30), and `max_response_bytes` (1024–2097152, default 1048576). The endpoint GET has no automatic retry. A valid result returns `clock`, `groups`, `total_requests`, `total_groups`, `next_offset`, `page_complete`, `complete`, freshness, transport diagnostics, and an exact continuation action. `complete=true` only when `next_offset` is null.

A decoded HTTP refusal is definite and uses `delivery=known`. Timeout, lost/incomplete framing, malformed JSON, or a structurally invalid success response uses `delivery=read_failed`, empty evidence, `fresh=false`, `page_complete=false`, and `complete=false`. Validation or schema failure before the summary request uses `not_sent`. No partial groups or diagnostic bytes are presented as a valid summary, and no summary read creates success evidence.

## Narrow recent-log projection

`logs.recent` is a compatible read-only wrapper around the same documented logs endpoint. It is intended for a small, explicitly selected interval and exact failure code when the caller needs attribution fields without returning bulky request details.

The action requires server query parameters `from`, `to`, `error`, and a strict integer `limit` from 1 through 50. It also accepts the documented `cursor`, `status`, `has_error`, `source_ip`, `source_cidr`, `user_agent`, `region_code`, `firewall_rule_id`, and `page` filters. All filters are forwarded to the server and combine according to the OpenAPI contract. `has_error=false` is rejected when an exact `error` is supplied.

The wrapper makes at most one logs GET. Its defaults allow a 55-second socket wait, 60-second total budget, and 2 MiB response, using the shared HTTPX reader with full HTTP-body validation and one JSON parse. Optional transport bounds use the same fields and limits as `logs.get`.

On success, each returned record contains only `timestamp`, `error`, `source_ip`, `source_cidr`, `user_agent`, `region_code`, `firewall_rule_id`, and `status`. A field absent from the source is returned as null and is not reconstructed. The response also includes the source clock, wall-clock retrieval time, requested limit, returned count, and continuation action.

`page_complete=true` means a structurally valid server page was decoded. `has_more=true` and non-null `cursor` mean another page exists. `complete=true` is stronger: the cursor is null, so the selected filtered range is exhausted. Preserve all original filters when following the opaque cursor. No page is treated as evidence that an address is hostile, and the adapter never chooses a firewall rule.

Timeout, incomplete framing, malformed JSON, invalid records, or other incomplete reads return `delivery=read_failed`, empty evidence, `fresh=false`, `page_complete=false`, `complete=false`, and the original transport diagnostics. Diagnostic prefixes and partially projected entries never count as a complete page.
