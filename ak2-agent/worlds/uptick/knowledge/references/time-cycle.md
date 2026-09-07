# Explicit time-cycle plans

The adapter exposes a mechanical executor over the documented time advancement and observation endpoints. This is a local capability, not a new simulator endpoint. It selects no intervals, objects, thresholds or infrastructure changes. It performs no control commands or probes.

## Start

Use `operation=act` and a new outer request ID. The action is:

```json
{
  "kind": "time_cycle.run",
  "budget_seconds": 95,
  "plan": {
    "intervals": [300, 300],
    "stop_when": {"new_log_errors": 1},
    "observe_options": {
      "include_metrics": false,
      "inbox_params": {"limit": 1000},
      "resources": {"timeout_seconds": 3, "budget_seconds": 4},
      "max_source_age_seconds": 30,
      "max_inbox_pages": 32
    },
    "baseline": {},
    "require_all": [],
    "stop_if": []
  }
}
```

This illustrates syntax, not an operational recommendation. Select all values before invocation from actual observations. All six plan fields are required. `intervals` contains 1–16 explicit integer durations, each at least 300 seconds; nothing repeats beyond this list. `stop_when` is forwarded unchanged to every interval. Explicit null means omit the wire field and use the documented unconditional variant. No interval is split, shortened or replaced after a refusal.

Within `observe_options`, `required_sources` explicitly selects mandatory sources from `overview`, `metrics`, `resources`, `inbox`, and `server_inspect`. It must include `overview`; `metrics` membership must match `include_metrics`. `inspect_server_ids` contains at most 64 distinct actual IDs and requires `server_inspect`. Existing plans without these fields retain the prior defaults: overview and resources are required, metrics is required when enabled, and selected inspections are required.

When `resources` is omitted, no resources GET occurs and no cached snapshot substitutes for it. Observations report `resource_inventory.complete=false`; `/servers` then contains only the explicitly selected addressable inspections and is not a complete inventory. Each inspection uses a deterministic child request ID scoped to the cycle phase, interval index and selected ordinal, sends the documented idempotent `server.inspect` control read, and is journaled before progression. Any required inspection error, invalid response or uncertain delivery stops execution before another advance.

`baseline` contains caller-supplied original measurements. It is not evidence of a fresh observation. Optional `baseline.inbox_message_ids` specifies already examined message identities. Without it, messages first encountered by this plan count as new. Store no secrets in the plan.

## Conditions

`require_all` contains predicates that must all match. `stop_if` stops when any predicate matches. Empty lists explicitly request no additional predicates. API errors, missing required sources, invalid data, a time response with `stop_reason=log_error`, and world completion always stop progression.

Each predicate has `path`, `op` and exactly one of `value` or `baseline_path`. Paths are JSON pointers, with `~0` and `~1` escaping. Arrays accept numeric indexes. There are no wildcards, expressions or arbitrary Python execution. The observation context has `overview`, `resources`, `servers` (a dictionary indexed by actual server ID), `inbox`, optional `metrics` and `logs`, and `advance` after an interval. Every `servers` value uses the stable command-result shape `{"result":{"server": ...}}`, whether sourced from the resources inventory or an explicit `server.inspect`. Thus server state and free disk are addressable as `/servers/{server_id}/result/server/status` and `/servers/{server_id}/result/server/disk/free_bytes`. A mandatory inspection is incomplete unless its response contains the matching server ID, a nonempty status, and a nonnegative integer `disk.free_bytes`; no advance follows such an incomplete response.

Operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in` and `contains`. Ordered comparisons require finite numbers. `in` requires an array on the right. `contains` requires a string or array on the left. Missing paths, invalid comparisons and unavailable optional sources stop with explicit errors.

`baseline_path` reads a value from the immutable baseline. Optional numeric `offset` adds to that value before comparison. For example, after supplying the actual baseline downtime under `downtime_seconds`:

```json
{"path":"/overview/availability/downtime_seconds","op":"gt","baseline_path":"/downtime_seconds","offset":0}
```

Put that predicate in `stop_if` to stop on a measured increase. It compares readings; it does not attribute all elapsed time to the POST.

Optional `when` is `always` (default), `before_advance`, or `after_advance`. Always predicates run during the initial preflight and after each interval. A successfully checked postflight also supplies the next interval's preflight data within that invocation: before sending that next interval, the executor additionally checks every `before_advance` predicate against this context. Both sets of checks remain in `last_checks`. No next-interval checks run after world completion, a stopping condition, an error, or exhaustion of the selected intervals. Predicates accessing `/advance` must select `after_advance`.

`inbox` includes `messages`, `new_messages`, `message_count`, `new_message_count`, `pages`, `complete` and `next_cursor`. A condition such as `{"path":"/inbox/new_message_count","op":"gt","value":0}` stops for newly encountered messages. Successful complete checks update the plan's seen IDs. A positive stop match is a requested stopping point, not proof of infrastructure failure. Failed `require_all` predicates return their actual and expected values as an error.

## Observation and pagination

Cycle observation is a compact collector using the existing shared run gate and incremental HTTP reader. Ordinary `observe` remains compatible and never executes a plan. Cycle observations fetch inbox pages, selected metrics/logs, overview, then resources. Overview and resources are mandatory live reads. No saved resource snapshot substitutes for a failure. The latest complete measurement summary is returned, while full responses and assembled observations remain accessible through the paginated local journal.

Supported `observe_options` are `include_metrics` (default true), `include_inbox` (default true), `metrics_params`, `logs_params`, `inbox_params`, `source_timeout_seconds` (default 10), `source_budget_seconds` (default 12), `resources`, `max_source_age_seconds` (default 30), `max_inbox_pages` (default 32), `required_sources`, `inspect_server_ids`, and `read_retry`. This deliberately bounded subset does not accept catalog, documentation, origin, operation polling or evidence options. Unsupported fields are rejected before execution. Standalone actions and ordinary observe keep their existing options.

`read_retry` is selected by the model as `{"max_attempts":1..3,"total_budget_seconds":1..75,"delay_seconds":0..30}`. All values are integers. Defaults preserve the old behavior: one attempt and no automatic repeat; `delay_seconds` defaults to zero. The total budget is shared by all read-only overview, metrics, resources, logs and inbox page attempts and by retry delays in one preflight or postflight collection.

Each attempt is journaled before and after transport, including HTTP status, failure stage and elapsed budget. A selected nonzero delay is applied only between a retryable failed attempt and its next attempt. The adapter journals the planned delay before waiting and its completion afterward, including the requested delay, actual elapsed time, charged budget and adjacent attempt numbers. The complete delay must fit both the remaining `total_budget_seconds` and the current invocation's `budget_seconds` with a persistence margin. Otherwise the cycle stops with `CYCLE_READ_RETRY_BUDGET_EXHAUSTED` before another read and without sending advance. Completed delay time is charged to the shared retry budget. No delay occurs when `max_attempts=1`, after a successful read, after the last attempt, or after a non-retryable refusal.

Retries are allowed only for transport/read failures, invalid or incomplete read responses, and a decoded HTTP 409 whose error is exactly `CONCURRENT_RUN_REQUEST`; other HTTP refusals stop immediately. No advance is sent until every mandatory source is complete and fresh. Exhaustion returns an explicit incomplete-source or retry-budget error with attempt and delay record indexes.

Set `include_inbox=false` only when the selected plan does not need inbox. The adapter then sends no inbox GET, marks it explicitly omitted, and rejects the plan locally if `required_sources` contains `inbox` or any predicate path is `/inbox` or below it. The default remains true for compatibility with existing plans.

Source timeout and budget accept integers 1–30, timeout no greater than budget. Resources uses existing defaults and validation, additionally capped at a 30-second request budget and 1 MiB response in this executor. Other source responses are capped at 512 KiB. `max_source_age_seconds` accepts 1–300 and is checked against wall-clock retrieval age for overview, included resources, selected metrics, and every selected `server.inspect`. Each successful addressable inspection records its actual local retrieval time; a missing timestamp, failed inspection, or stale inspection stops before another advance. Explicitly omitted resources has no retrieval timestamp and is excluded from freshness checks without being treated as an inventory. Clocks are preserved separately; different endpoints do not form an atomic snapshot. Resuming an unsent interval requires a new preflight. Incomplete collections may continue across invocations, but stale critical readings cause an explicit stop.

Inbox accepts only documented `cursor` and `limit`; the adapter follows opaque `next_cursor` values until null, saving each page and cursor before reading the next. Repeated cursors, malformed pages, exceeding `max_inbox_pages` (1–128), or more than 1 MiB of assembled messages stop as incomplete. Budget exhaustion between pages preserves continuation. No time POST follows incomplete pagination. The next cycle revisits the final page's input cursor to include later messages; it never derives a cursor from an ID, timestamp or decoded cursor. Previously read message IDs are deduplicated locally. Completeness is relative to the selected initial cursor and the source clocks, not all future delivery.

Selected logs remain one caller-filtered page, with their cursor and range preserved. The executor does not infer that this covers the entire logs history. Metrics retains the documented comma-separated `names` serialization. Explicitly omitting metrics is supported; an attempted metrics failure stops the plan. The source timeout observed during adaptation does not imply that all current-state sources are unavailable.

## Checkpoints, identities and continuation

Before the first HTTP request, the entire plan, its fingerprint and all child request IDs are saved privately. Child IDs use `tc.<hash of original outer ID>:advance:<zero-based index>`. Each advance has its own stable ID and an existing-format mutation-journal fingerprint. Received advance results are saved before any postflight or subsequent advance. Read attempts, full replies, errors and assembled observations are also saved in private record files, with monotonically increasing record indexes.

A definite response to an invocation is cached, including a yield or refusal. Replaying that outer ID returns the saved response without extending the plan. A budget yield returns `pending=true`, `delivery=known`, and a local `external_id` identifying the accepted continuation. It does not claim a remote asynchronous time operation.

Continue only by an explicit new act invocation:

```json
{"kind":"time_cycle.continue","continuation_id":"RETURNED_CONTINUATION_ID","budget_seconds":95}
```

The new outer invocation has its own ID. It references the existing immutable plan and existing child IDs. A stopped or finished plan cannot be restarted through continue; it returns its saved state. A new strategy requires a newly selected plan and new outer ID. Only one active cycle is accepted at a time.

If an advance response is lost or invalid, execution stops with `delivery=unknown`, the original child ID and `replay_outer_request_id`. Replay the identical outer action under that original outer request ID. A different continuation invocation is rejected until this is resolved. The time endpoint explicitly guarantees that identical replay of the same request ID does not advance time twice. If a definite acceptance containing `operation_id` was already persisted in either the normalized entry or its transport diagnostic, recovery polls that exact operation and does not resubmit `time/advance`. The executor makes at most one POST attempt for a child per invocation only when no accepted operation identity is available, and never replays completed journal responses remotely. After recovery and postflight it yields before another interval so the model can explicitly continue.

A decoded HTTP refusal is `delivery=known` and is cached. It does not establish rollback or applied advancement. A generic INTERNAL_ERROR remains unexplained. No direct server lookup by original request ID exists in the inspected OpenAPI; `time.diagnose` and `request_diagnostics.get` remain available. Successful reads cannot substitute for the missing request-correlated advance result.

## Results and journal

`data.cycle.index` is the number of intervals whose successful advance and postflight checks have completed; a failed postflight can leave a confirmed advance at the current index. Inspect `phase`, the child journal and records before interpreting progress. `status` is `active`, `unknown`, `stopped` or `finished`. `intervals_exhausted` only finishes the selected plan; it does not complete the world.

HTTP 202 confirms acceptance of an asynchronous advance but emits no success evidence. The executor saves its operation ID, performs bounded status polling, and yields a continuation with `delivery=known` and `pending=true` if the operation is still queued or running. A continuation polls the saved operation instead of creating a new advancement request. Only a valid `AdvanceTimeResponse` from a succeeded terminal operation emits effect evidence, using the existing `action:CHILD_REQUEST_ID` identity and exact result. GETs, condition checks and plan acceptance produce no success evidence. Retained facts are eligible for repeated output, including through ordinary observe, using the original identities. World `done` requires explicit terminal overview status or the time endpoint's `run_completed`; final success remains null until the world's boolean evaluation is obtained.

Read a checkpoint without any HTTP or execution:

```json
{"kind":"time_cycle.get","continuation_id":"RETURNED_CONTINUATION_ID"}
```

Read one complete historical record:

```json
{"kind":"time_cycle.get","continuation_id":"RETURNED_CONTINUATION_ID","record_index":0}
```

Follow `next_record_index`. The journal contains both attempt markers and responses. `last_observation_record` points to the most recent complete assembled observation. Record reads return historical data with original source clocks. They never acknowledge delivery, remove records, replay mutations or resume execution. Standard evidence pagination still applies.

Each invocation has a 10–100 second execution allowance (default 95), leaving runtime time for persistence and output. Each HTTP exchange has a main-thread timer. A new request starts only when its complete configured allowance plus a persistence margin remains; otherwise the executor yields between steps. Small caller budgets may require a continuation with a larger budget to fit a POST's 32-second allowance. No background work continues after return.

## Verification

Run `python3 scripts/test_time_cycle.py` with the existing transport tests. The cycle tests use temporary private state and simulated endpoint replies, without external calls. They cover pagination, refusal, unknown replay, child identities, condition stops and finished-response replay. Runtime should then check ordinary observe; no live time advancement is required merely to validate adaptation. Actual cycle execution is selected by the decision loop.
