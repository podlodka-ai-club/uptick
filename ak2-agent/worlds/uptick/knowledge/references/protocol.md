# Uptick API v2 protocol reference

This file records the stable world protocol. It intentionally contains no run-specific IDs, credentials, incident conclusions, or operational heuristics.

## Access and authorization

The API origin is `http://81.176.229.58:8080`; `/openapi.yaml` is public. `POST /v2/start` creates or idempotently replays a run and returns its run ID, simulation clock, documentation, and private control-panel HTTP Basic credentials.

Basic authorization is required for the control command catalog, command submission, and credential-version retrieval. Overview, inbox, probes, logs, metrics, resources, operations, and time advancement do not require it.

Six commands also require server-level `target_auth`: `database.create`, `database.inspect`, `database.backup`, `database.restore`, `disk.usage`, and `disk.cleanup`. The adapter stores these secrets privately and resolves an action’s `credential_id` into `target_auth`.

Credential IDs may be reported as structured fields or embedded in inbox prose. They identify credential versions; they are not themselves secrets. Usernames and passwords must never be returned in adapter output.

## Observation and probes

Overview, metrics, logs, resources, inbox, operation status, and the authenticated command catalog are GET endpoints. The probes endpoint is POST-only. Because a probe issues a diagnostic request, the adapter exposes it as an explicit `act` action and does not run it automatically during `observe`.

The public OpenAPI document is authoritative for probe request fields. The adapter adds the world request ID to the supplied probe parameters.

`ProbeRequest` requires `request_id` and `page`. Pages are `product_list` and `product_page`; `product_id` is required for `product_page` and forbidden for `product_list`. HTTP 200 from the endpoint indicates that the diagnostic request executed. The response's integer `status` is the measured page outcome: 200 is success; 403, 500 and 503 are failures. `ProbeResponse` includes `clock`, `request_id`, `page`, `status`, `latency_ms`, `load_units`, `source_ip`, `user_agent`, `region_code` and `firewall_rule_id`, with optional `product_id`, `error` and `message`.

Measured probe results are effect evidence identified by `probe:` followed by the original request ID. Evidence detail preserves the full response after secret redaction. Completed journal responses can restore these facts without repeating a diagnostic request; the original fingerprint distinguishes probe records from other actions. Historical facts retain their original clock and identity on every observation or replay. The runtime accepts at most 64 evidence entries per response. The adapter paginates distinct evidence by stable identity, exposing continuation metadata in `data.evidence_page`. Default observations cycle through retained events; explicit `options.evidence_cursor` selects continuation without advancing the automatic cursor, and `options.evidence_limit` accepts 1–64. Traversal does not acknowledge delivery, remove historical facts, or replace complete private journal responses. Reading a catalog or ordinary GET endpoint does not establish a successful diagnostic trial.

## Idempotency and asynchronous work

A command body contains `request_id`, `command`, `params`, and, only where required, `target_auth`. Request IDs are 1–128 characters and use letters, digits, `.`, `_`, `:`, and `-`.

Repeating the same command and parameters under the same request ID replays the stored response. Changing the payload produces `IDEMPOTENCY_CONFLICT`. Target credentials are excluded from the idempotency payload, but authorization is checked again during replay. Authorization failures do not reserve the request ID, so the same command and request ID can be retried with a current credential version.

`server.create`, `server.delete`, `database.backup`, `database.restore`, and `site.stop` are asynchronous. HTTP 202 only supplies an operation ID. Poll the operation until `succeeded` or `failed` and use its terminal result or error.

Time advancement accepts `request_id`, `duration_seconds` of at least 300, and optional `stop_when`. `stop_when.new_log_errors` can be combined with exact `error_codes`. Stop reasons are `log_error`, `duration_elapsed`, and `run_completed`.

`advance_time.schema` reads the actual public OpenAPI document and exposes the complete POST endpoint, request body, duration constraints, nested stop conditions, responses and recursively referenced schemas. It defaults to a single network refresh; explicit cache/auto modes preserve source timestamps, freshness and document hash. Observation uses auto mode. The wire-field comparison reports its scope separately from full API validation. See `references/time-advance.md` for retrieval and diagnostic details.

The time transport preserves the original request ID and selected parameters, sends one POST without Basic Auth, and does not automatically retry or split intervals. Newly captured refusal diagnostics retain the decoded redacted body, actual redacted request body, available diagnostic headers and HTTP duration in the private journal and returned error. Refusals do not establish advancement; a successful advancement effect requires a returned simulation clock with a nonnegative numeric applied duration.

## Commands

The 18 control commands are:

- `firewall.rules.list`, `firewall.rules.upsert`, `firewall.rules.delete`
- `server.types.list`, `server.create`, `server.inspect`, `server.delete`
- `database.create`, `database.inspect`, `database.backup`, `database.backups.list`, `database.restore`
- `site.config.get`, `site.stop`, `site.start`, `site.database.set`
- `disk.usage`, `disk.cleanup`

The live command catalog is authoritative for JSON schemas. The adapter exposes its authenticated GET separately as `command_catalog.get`, with a single HTTP attempt and independent bounded socket timeout, total wall-clock budget, and response-byte limit. It returns the complete decoded catalog, preserving schema definitions for authentication fields while redacting actual credentials. Successful responses are cached within the current run and origin with a UTC retrieval timestamp. Every return identifies whether the catalog is live or historical; a failed refresh retains its structured error even when cached data is available. See `skills/operate/SKILL.md` for action settings. Catalog retrieval does not perform a control command or produce action-success evidence. Automatic observation defaults to `auto`: use the matching run/origin cache without HTTP when available, otherwise attempt one bounded network fetch. The explicit `command_catalog.get` action still defaults to network refresh. Both interfaces accept `auto`, `cache`, and `network`; source, age, freshness, requested/effective mode, and network-attempt metadata remain visible. Failed refreshes preserve their errors alongside any cached data.

Observe reads overview, inbox, optional metrics, an explicitly selected logs page, the catalog, selected unconfirmed operation statuses, and finally resources. Its returned representation is compact: stable schemas and the catalog are linked through separate schema/catalog actions; persisted terminal operations are listed by ID and read through `operation.get`; inbox retains its clock, compact message metadata and opaque continuation. Fresh source failures, clocks, pagination, completion and evidence remain explicit. Jobs execute serially on the main thread through the shared run gate, with per-job timers and a 100-second collection allowance. Resources has independent outer `options.resources` socket, total-request and byte limits, defaulting to 3 seconds, 4 seconds and 1048576 bytes. Its one GET cannot delay previously attempted critical-source or operation HTTP requests. The shared HTTPX client reuses connections and reads the complete HTTP body before parsing JSON once; it records HTTP framing, byte counts, parse completeness, stages and timing. Partial or structurally invalid resources remain explicit failures without snapshot substitution. The separate read-only `resources.get` action can make up to three explicitly bounded fresh attempts when the endpoint intermittently leaves a chunked response incomplete. It accepts only a fully decoded and structurally validated current inventory, never substitutes a cache, and returns `delivery=read_failed` with every attempt's diagnostics when no complete response is obtained. See `references/resources.md`. Outer `options.include_metrics` is a boolean defaulting to true; false omits only the automatic metrics GET and reports omitted/incomplete without substituting cached metrics. The separate `metrics.get` action remains available. Both interfaces share HTTP transport, but automatic metrics uses one attempt with observation limits, while standalone metrics retains up to two attempts with a 25-second I/O timeout and one shared 30-second transport budget. Source durations include gate waiting; returned HTTP diagnostics separate gate wait and HTTP duration and preserve the failure stage. Metrics `names` arrays use one comma-separated query value according to OpenAPI form/explode=false. Because Uptick serializes requests that advance one run clock, a shared per-run transport gate prevents those jobs from overlapping their HTTP exchanges and causing `CONCURRENT_RUN_REQUEST`. Each live source has one independently bounded attempt. `source_status` preserves requested cursors/ranges, response cursors, source clocks, elapsed time, freshness, age at response assembly, and explicit partial failures. Stable terminal operation payloads are persisted and returned from the run-local cache with their original identities. Pending, nonterminal, and legacy-unconfirmed IDs are ordered with known active work first and stale checks next; a configurable bounded batch is polled on each observation. Deferred IDs expose cached status and age when available or explicit incompleteness otherwise. Credential IDs discovered in observations are cached as metadata, while private credential versions are fetched only on explicit request or when required by a selected command. Observation collection duration is returned in `data.execution_stats.last_observation_seconds` and persisted privately.

### Firewall

Rules have an ID, nonnegative priority, `allow` or `deny`, enabled state, and a nonempty match. Matches support source CIDR, two-letter uppercase region code, and case-sensitive user-agent `equals` or `contains`. Conditions in one match are ANDed. Ordering is ascending priority and then rule ID; the first active match wins. No match allows the request. Optional RFC 3339 expiry becomes inactive at its timestamp.

### Servers

Server types define role, capacity, disk, connection behavior, price, and provisioning duration. Creation requires name, role, and a listed instance type. A ready backend joins a running site automatically. A new database server contains no database.

Backend deletion drains traffic; the last backend cannot be deleted. The connected database server cannot be deleted. Billing stops only when deletion completes. Server inspection does not expose passwords.

### Databases and site

A database is created empty on an active database-role server using that server’s credential. Backups are external, billable objects. Backing up the site’s connected database requires the site to be fully stopped. Restore requires a ready backup, an empty target database, enough disk, and the target server’s credential.

The managed site state is `running`, `stopping`, or `stopped`. `site.stop` is asynchronous. `site.start` requires an active backend and a ready connected database and is blocked by relevant backup or restore work.

Changing the site database requires a fully stopped site, a ready target restored from the final version of the current database, completed operations on both databases, and `expected_current_database_id` for atomic conflict detection. Switching does not start the site.

### Disks

Disk usage consists of system, database, and log bytes. Only logs are cleanable. `disk.cleanup` zeroes log bytes and does not alter databases, system data, request history, or simulation event history. Disk commands remain available during `DISK_FULL` but require current server credentials.

## Simulator diagnostics discovery

`request_diagnostics.schema` inspects the documented GET inventory. `request_diagnostics.get` selects a historical journal entry by `source_request_id` and reads a documented source accepting the current run ID and original request ID when available. It does not replay POST requests. Discovery distinguishes missing direct lookup, incomplete inspection, cached documentation and failed refreshes. Server access errors remain explicit. Availability of private simulator traces is not inferred from the public contract. See `references/request-diagnostics.md` for provenance, authentication and pagination.

## Local finite time-cycle executor

`time_cycle.run`, `time_cycle.continue` and `time_cycle.get` compose existing public time/advance, overview, resources, inbox, caller-selected metrics/logs interfaces, and explicitly selected idempotent `server.inspect` reads. The plan declares its mandatory sources. Overview is always mandatory; resources may be explicitly omitted only with an incomplete-inventory marker, while every selected inspection remains mandatory and addressable by its model-supplied server ID. They introduce no simulator endpoint. An immutable model-selected plan supplies finite intervals, stop conditions, observation options and baseline measurements. It may also select at most three bounded attempts for read-only collection sources, one shared preflight/postflight retry budget, and a fixed bounded delay between retryable failures and their next attempts. Attempts and delays are journaled and both consume the shared budget. Delay is never applied after a definite non-retryable refusal or when no further attempt exists. Only transport/read failures, incomplete read responses, and exact `CONCURRENT_RUN_REQUEST` refusals are retryable, and no advance follows an incomplete or stale mandatory set. Inbox may be explicitly omitted only when it is not required and no predicate reads `/inbox`. Private checkpoints retain child request IDs, results and paginated reads. Only explicit act continuation executes further work; ordinary observe exposes status and historical evidence. See `references/time-cycle.md` for exact schemas, bounded execution, stop semantics and recovery. The documented lack of a direct server lookup by original request ID remains unchanged.

## Completion and errors

The objective is uptime at least 99% with minimum total infrastructure cost. Planned shutdown and blocked legitimate traffic count against uptime. Page status and control API status are separate concepts.

Important control errors include `CONTROL_UNAUTHORIZED`, `TARGET_UNAUTHORIZED`, `CREDENTIALS_EXPIRED`, `IDEMPOTENCY_CONFLICT`, `DB_NOT_EMPTY`, `BACKUP_NOT_READY`, `SITE_CONFIG_CONFLICT`, and `DB_BACKUP_STALE`. HTTP 503 can mean a temporarily unavailable target server. Diagnose the structured error code and terminal operation result rather than relying on HTTP status alone.
