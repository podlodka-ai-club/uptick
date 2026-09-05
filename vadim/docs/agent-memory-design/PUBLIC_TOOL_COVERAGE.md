# Public simulator tool coverage

Audit date: 2026-09-05. Only the authorized public API contract was inspected.
The retained `openapi.yaml` has SHA-256
`452b622ebf8e1734cfd630ff2dfe4cb1c25350f0e9b67d5ff5cf3e64e9cd1dc0`.
It is an API identity, not a simulator world-content identity.

The initial inventory described `c05864f`. The environment boundary was completed
in `62bce25`; the current change closes the two query gaps below. Runtime
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

Twelve focused cases cover schema separation, action-to-HTTP forwarding, optional
parameters including false, independent log bounds, repeated/different filters,
returned data and invalid query validation. Full offline suite: **577 passed,
2 opt-in live skips**; after the one-sided timezone correction, all **48**
focused startup/query/v2 regressions passed. All **56** historical schemas and
identities remain identical. This is contract evidence; real API and model observations
are recorded separately after running the committed source.

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
