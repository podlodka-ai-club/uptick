# Resources observation transport

`GET /v2/runs/{run_id}/resources` is a public read of current infrastructure. The documented endpoint has no query filters or pagination. Do not invent a cursor, limit, or server filter. Its response requires `clock`, `active_instances`, `total_capacity_units`, `used_load_units`, `total_cost_per_hour_minor`, and `servers`. Server entries include their identity, name, role, type, status, capacity, load, hourly cost, disk counters, database IDs, and credential reference. A resource read produces no success evidence.

## Independent limits

Outer observation options accept:

```json
{"resources":{"timeout_seconds":3,"budget_seconds":4,"max_response_bytes":1048576}}
```

These are the defaults. Socket timeout accepts integers 1–30 seconds; total request budget accepts integers 1–45 seconds, with socket timeout no greater than budget. The response limit accepts integers 1024–2097152 bytes. These options affect only resources and do not become HTTP parameters. There is exactly one resources GET when the collection allowance permits it, without automatic retry or cache substitution.

Observation executes jobs serially on the main thread, retaining the shared run gate. Overview, inbox, enabled metrics, selected logs, catalog, and the selected active/unconfirmed operation checks are attempted before resources. Thus a stalled resources response cannot delay their HTTP requests. Their clocks still differ, and their age at final assembly includes any subsequent resources wait. Existing metrics omission, caller-selected filters, operation selection, terminal caches, credential handling and evidence pagination remain supported.

A main-thread timer bounds each observation job, including gate acquisition, connection/header waiting, body reading and validation. The collection deadline is 100 seconds from entry to observe, including documentation work; each job receives the smaller of its configured budget and the remaining allowance. Jobs reached after exhaustion produce explicit errors without HTTP. Documentation loading also has bounded attempts. Time remains for persistence and JSON output under the runtime's 120-second limit. No background worker continues a timed-out read.

## Framing and completeness

The shared reader uses one reusable `httpx.Client` per adapter process. HTTPX/httpcore handles Content-Length and chunked transfer decoding. The adapter enforces the byte limit while streaming, then decodes UTF-8 and parses JSON once after HTTP-body completion. Truncated Content-Length, missing chunk termination, and trailing invalid JSON are rejected. Correctly framed keep-alive responses return without waiting for socket closure. JSON completeness and HTTP framing completion are recorded separately.

`source_status.resources.http_call`, or its error's `http_call`, preserves HTTP status, bytes returned by the body reader, chunk count, first/last body-byte timing, failure stage, HTTP duration, and gate wait. `framing` records Content-Length, Transfer-Encoding, Content-Encoding, Connection, HTTP version, and the framing mode reported from HTTP headers. Remaining Content-Length bytes are recorded when available; internal chunk-byte counters are null because HTTPX owns the framing. `framing_complete=true` is set only after the body iterator finishes successfully. Missing headers remain null.

`json_complete` reports successful decoding; `body_complete` retains the existing complete-document meaning. The latest JSON parse error preserves only its type, message and position, never a raw partial body. Resources additionally validates required response and server fields. A decoded but structurally invalid resource response has `body_complete=true` and `response_valid=false`; it remains an observation error. Partial, malformed, oversized and structurally incomplete bodies never populate `data.resources`, never supply credential metadata, and never establish an empty server list.

The latest resources source metadata and diagnostics are saved privately as `last_resources_read`, with a retrieval timestamp. No resource snapshot is substituted after failure. Other fresh sources and historical operation/probe evidence remain available, with the current error reported as `OBSERVATION_INCOMPLETE`.

A body timeout after HTTP 200 does not establish whether the cause is the server, network, HTTP framing, or client. Byte counts alone do not establish a complete JSON document. Compare actual framing and parse diagnostics from subsequent checks; do not infer the cause from the absence of Content-Length in an older diagnostic header allowlist.

## Standalone recovery read

Use `{"kind":"resources.get"}` when an ordinary observation could not obtain a complete inventory. This action performs only documented `GET /v2/runs/{run_id}/resources` reads and never executes a command, probe, time advancement, or other mutation. Defaults are three attempts, a 15-second socket timeout, a 20-second per-attempt budget, and a 2 MiB response limit. Optional sibling fields are `max_attempts` (1–3), `timeout_seconds` (1–20), `budget_seconds` (1–25), and `max_response_bytes` (1024–2097152); timeout cannot exceed the per-attempt budget and the configured aggregate budget cannot exceed 75 seconds.

Each attempt is fresh and uses the shared per-run gate with no nested automatic retry. `data.resources_fetch.attempt_diagnostics` preserves its HTTP status, framing, byte and chunk counts, parse state, failure stage, timing, and exception. A successful result preserves the source `clock`, every required top-level inventory field, all validated server records, retrieval time, freshness, server count, and attempts made. It returns immediately on the first complete valid response.

A partial, malformed, oversized, timed-out, or structurally incomplete HTTP 200 response is never accepted and never interpreted as an empty inventory. After all selected attempts fail this way, the action returns `delivery=read_failed`, `complete=false`, `fresh=false`, no evidence, and an explicit error containing every attempt diagnostic. It does not return partial server data or substitute any previous snapshot. A completely decoded HTTP refusal is definite and returns `delivery=known` with its body; it is not retried as though it were a lost read.

## Verification

Run the tests with the project virtual environment: `uv run --locked python -m unittest discover -s worlds/uptick/knowledge/scripts -p 'test_*.py'` from the agent root. The tests use HTTPX/httpcore with local socket pairs to cover Content-Length, chunked transfer, complete JSON on an open connection, rejection of missing chunk termination after complete JSON, incomplete body/header stalls, premature EOF, malformed bodies, byte limits and deadline exhaustion. An isolated observation test verifies critical-source ordering, active operation completion, retained historical evidence and explicit resource failure. These tests make no external requests.

Then run one ordinary observe with explicit resources limits and inspect its actual source diagnostics. Observe sends no infrastructure commands, probes, or time/advance request. Run GETs still account for real elapsed simulation time under the world protocol. Prepared tests or successful local framing checks do not establish that the live resources endpoint will return a complete response.

The HTTP client is closed when the adapter process exits. Pooling applies within one invocation (for example, its observation reads), not across separate subprocesses. I/O timeouts are bounded by an outer main-thread wall-clock timer; generic GET retries share one call budget. Explicit resources.get attempts retain their documented per-attempt budgets.
