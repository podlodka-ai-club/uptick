# Public simulator tool coverage

Audit date: 2026-09-05. Only the authorized public API contract was inspected.
The retained `openapi.yaml` has SHA-256
`452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0`.
It is an API identity, not a simulator world-content identity.

This inventory describes the implementation at `c05864f`, before the current
environment-boundary and observability work. Runtime evidence is under ignored
`artifacts/environment-boundary-2026-09-05/`.

## Existing coverage

All 18 public control commands have typed requests, parameter objects and an
execution path through the v2 adapter: firewall list/upsert/delete; server type
list/create/inspect/delete; database create/inspect/backup/backup-list/restore;
site configuration/stop/start/database-set; disk usage/cleanup. Required public
parameter names match the contract. Authentication is resolved privately by the
HTTP adapter and is not a model capability or model-supplied credential field.

The adapter also supports start, overview, metrics, logs, resources, page probes,
operation polling, inbox, command catalogue and time advance. Start and private
credential resolution are lifecycle/transport operations. The agent does not
need a credential-reading tool. The public API has no arbitrary shell endpoint.

The existing log/inbox readers already paginate with a bounded number of pages
and report remaining unread data. Their cursors are adapter-owned. This is not
an absent pagination implementation, although the model cannot request an
arbitrary historical page with the current action schema.

## Confirmed gaps and next implementation

| Capability | Public API | Existing model/adapter | Planned correction |
| --- | --- | --- | --- |
| Log diagnosis | Time window, page, status, infrastructure error, source IP/CIDR, user-agent, region, firewall rule, cursor, limit | Model chooses only HTTP status; client handles time/cursor/limit, adapter consumes new logs | Add a typed v2 query with public filters and explicit historical reads; preserve bounded incremental reading |
| Metric history | Paired from/to, aggregation interval, selected metric names and page | Snapshot-only action and HTTP client | Add typed v2 metric query and exact HTTP forwarding |

For log queries, changing filters must not reuse another query's cursor or
silently hide matching results through a global seen-ID set. Explicit historical
reads must be repeatable and must not advance the default incremental reader's
watermark. A query result is observed data, never hidden ground truth about
whether traffic is malicious.

Tests must cover the actual action-to-HTTP boundary, rejected invalid query
combinations, omitted optional parameters, cursor/filter isolation and returned
evidence in subsequent context. Adding parameter fields alone is insufficient.
Implement these in the simulator owner after the neutral environment contract
is stable. They do not belong in the generic agent action union or memory core.

## External startup description

The public start response requires `commands_markdown`: the complete COMMANDS.md
embedded in the current server build. There is no separate public startup-prompt
field. The existing client removes credentials before returning this text.
The effective sanitized text can therefore be the externally supplied startup
description, frozen before the first decision. It must not be described as a
byte-for-byte copy of the unsanitized response.

An already observed sanitized description was retained separately for
preregistration: 18,364 characters, SHA-256
`645f9492e4aa0123537b6cc9201981f16e6f0d78d7da6ce2ee1adb3a90c23d29`.
Its source record and namespace are retained alongside it. It contains public
operating rules, not the SRE world's immutable content hash or causal family.

Paired experiments must declare their expected effective prompt before any
declared environment/model call, then compare the actual startup document and
tool specification before invoking the model. A mismatch is a retained failed
attempt; it must not cause the manifest to be rewritten.

## Evidence limits

Command/schema coverage does not prove successful incident handling. Historical
SRE runs have not demonstrated an SLO success, and no fair comparison with Alex's
actual agent has been executed. Additional diagnostic controls must earn their
place through observable use and outcomes; their presence alone establishes no
memory benefit or generalisation claim.
