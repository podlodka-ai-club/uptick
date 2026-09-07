# Public simulator tool coverage

## Deployment refresh — 2026-09-06

Public API 0.7.1 retained as `artifacts/simulator-refresh-2026-09-06/openapi.yaml`,
SHA-256 `297574eddcb3f227cd8f3155ed4204b8f9a713af7e37f4fef6e34b2815f580d4`.
The release adds `GET /v2/runs/{run_id}/logs/summary` and public
`overview.evaluation` with policy `cost-bands.v1`. Summary groups observed
traffic by public attributes and reports exact request counts and unique IPs;
all matching statuses and probes are included. Totals cover the complete
filtered interval even when groups are paginated. No attack labels are exposed.
New v13 logs contain normal and attacking traffic; old logs are not backfilled.

The target is a complete run with uptime >= .99 and total cost strictly below
50 million RUB for 100 points. Costs in the API are minor units (kopecks):
5 billion, 15 billion and 50 billion are the score boundaries 100/75/50/0.
Incomplete runs or uptime below .99 receive 0. The running score is null and
max_achievable_score excludes future cost and failures. Higher uptime adds no
points; lower cost breaks ties. Public score data are legitimate observations,
not private evaluator answers.

`query_logs_summary` is now a typed canonical v2 action with all public filters,
required from/to/group_by, offset pagination and optional CIDR prefix lengths.
It preserves the full sanitized response and keeps incremental log cursors and
seen IDs unchanged. Legacy historical decision schemas remain unchanged.
Focused adapter/schema tests: 49 passed. Full suite: **710 passed, 2 skipped**.
Scoped Ruff lint/format and local review passed. A real read-only request through
Environment.execute and the HTTP client returned 9 logs in 2 user-agent groups
for a five-minute interval of the existing dev06 run. This verifies live wire
compatibility, not v13 attack recognition or memory utility. Evidence is retained
in `artifacts/simulator-refresh-2026-09-06/summary-live-smoke.json`.

The refreshed sanitized startup document was fetched through an idempotent
repeat of an evidenced old start tuple; the same run ID was verified. No new run
or model call was made. Its SHA-256 is
`9aaec4adb7b36cbaf8bb29d8ca24ad1ee4ea064003599ac81b3d622450773c2e`
(36,895 UTF-8 bytes), retained at
`artifacts/simulator-refresh-2026-09-06/expected-startup.md` with tuple/source
provenance. The next attempt must match this actual server-supplied text before
its first decision; it must not substitute the old frozen startup document.

Historical raw episodes remain evidence of the earlier runs only. They cannot
establish current prices, resource IDs, scenario timing or current causal rules.
The neutral prompt now asks the model to check recalled experience against the
current environment contract and observations. No old corpus rows are rewritten
or selected by answer content. Cross-version development runs are not a matched
memory comparison. The sections below retain earlier validation evidence.


Audit date: 2026-09-05. Only the authorized public API contract was inspected.
The retained `openapi.yaml` has SHA-256
`452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0`.
It is an API identity, not a simulator world-content identity.

The initial inventory described `c05864f`. The environment boundary was completed
in `62bce25`; queries were added in `373f6ee`, with live-discovered corrections
in `2fc633b` (CIDR schema) and `c92e094` (exact timestamps). Runtime
evidence is retained under ignored `artifacts/`.

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
an absent pagination implementation, and explicit queries now let the model request a historical page without
changing those cursors.

## Implemented observability queries

| Capability | Public API | Canonical adapter action |
| --- | --- | --- |
| Log diagnosis | Independent from/to, page, status, error flag/code, source IP/CIDR, user-agent, region, firewall rule, cursor, limit | `query_logs`: explicit single-page historical/filter query; returns the next cursor and preserves incremental-reader state |
| Metric history | Paired from/to, aggregation interval, metric names and page | `query_metrics`: current snapshot plus returned historical series, with exact public HTTP parameters |

`SimulatorV2Decision` belongs to `simulator/decisions.py` and is published by the
started environment. The generic decision loop has no simulator tool list.
Historical `V2NextStep` remains a compatibility schema; real v2 execution and
evaluation use the canonical environment schema, pinned before model creation.

Changing filters does not reuse another query's cursor or hide matching results
through the incremental reader's seen-ID set. Explicit reads are repeatable and
do not advance that reader's watermark. Returned traffic fields are observed
data, never hidden ground truth about whether traffic is malicious.

Sixteen focused cases cover schema separation, action-to-HTTP forwarding,
optional parameters including false, independent log bounds, repeated/different
filters, CIDR validation, exact fractional timestamps and invalid windows.
Final full suite: **583 passed, 2 opt-in live skips**. All **56** historical
schemas and identities remain identical. Real API calls verified nonempty
repeat reads, cursor isolation, precise boundary-error retrieval and metric
windows. The model selected both new queries in the bounded diagnostic, and a
separate final schema-acceptance run exited 0. Exact attempts, discovered defects
and limitations are recorded in `OBSERVABILITY_RESULTS.md`.

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
