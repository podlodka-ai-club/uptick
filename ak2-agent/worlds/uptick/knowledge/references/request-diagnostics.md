# Simulator request diagnostics

`request_diagnostics.schema` inspects the public `/openapi.yaml` without submitting commands. It lists documented GET endpoints, their original YAML, recursively referenced definitions, query and path parameters, and authentication requirements. A direct lookup candidate must accept both `run_id` and `request_id` as inputs. A response field named `request_id` alone does not qualify.

`request_diagnostics.get` reads the private local journal entry selected by `source_request_id` and can make one documented GET for that original identity. It never replays the original POST, changes its parameters, or performs infrastructure actions. The outer envelope request ID identifies this diagnostic invocation; it is distinct from the original ID being investigated.

The current run ID comes from bootstrap. The adapter rejects attempts to select another run or substitute a different original request ID. A single documented candidate is selected automatically; multiple candidates require an explicit `endpoint`. Other required path inputs belong in `path_params`; documented query filters, time ranges, limits and cursors belong in `params`. Each invocation retrieves one page. Follow the source's `next_cursor` and preserve its documented filters. A page is not a complete history or a proof of causation.

Only documented same-origin GET paths are eligible. Public sources require no authentication; supported HTTP Basic authentication is supplied privately from bootstrap. Unsupported authentication and HTTP access refusals remain explicit errors. The adapter does not guess debug, trace, audit or administrative endpoints.

Both actions default to `mode: network`, making one bounded documentation GET. `auto` uses the origin-scoped cache when available; `cache` never refreshes. Observation discovers sources in auto mode. Each new GET has an 8-second socket timeout, a 12-second transport budget, a 16-second outer wall-clock cap on the main thread, and a 1 MiB response limit. No GET is retried automatically by this interface.

Document provenance includes URL, retrieval timestamp, SHA-256, freshness, age, extraction completeness and errors. A failed refresh retains the cache and the refresh error. Unsupported YAML extraction produces incomplete inspection, not a claim that an endpoint is absent.

`no_documented_direct_lookup` means the inspected OpenAPI has no GET accepting both identities. It does not establish that private server diagnostics do not exist. The returned GET inventory allows examination of other documented observability sources. `unverified` means inspection could not establish availability. The existing site logs contain simulated visitor and probe requests; they do not expose the agent audit log.

## Confirmed documented inventory

The inspected OpenAPI document with SHA-256 `452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0` exposes these GET paths relative to `/v2/runs/{run_id}`: `/overview`, `/metrics`, `/logs`, `/resources`, `/operations/{operation_id}`, `/inbox`, `/control/commands`, and `/credentials/{credential_id}`. Extraction completed without errors. None accepts the original control request's `request_id` as a path or query input. This document therefore provides no direct lookup of a time-advancement request's internal cause or processing state. Operation lookup requires an actual issued operation ID; it cannot substitute a request ID.

`request_diagnostics.get` remains available for the saved transport record and explicit source limitation. A later documentation refresh can discover newly documented sources. An HTTP 500 containing only `INTERNAL_ERROR` and no clock or processing details does not establish advancement, rollback, or a parameter constraint. Historical operation and probe results do not establish current source availability when live observations fail.

The local journal is historical transport information. Its `status: completed` means the adapter received and saved a response, including HTTP refusals; it does not mean the simulator completed processing successfully. Legacy entries may lack timestamps or HTTP metadata. Missing metadata remains unknown and is never reconstructed by replaying a mutation.

Server source responses retain the decoded body, access errors, HTTP diagnostics, returned clock and cursor. Read completeness describes the returned response, not whether it explains the internal error. Generic `INTERNAL_ERROR` without a processing state or cause remains unexplained. Neither source lookup nor journal reading emits success evidence or infers time advancement.
