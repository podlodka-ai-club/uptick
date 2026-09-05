# v2 memory integration evidence — 2026-09-05

These are preregistered, bounded integration exercises against the public
simulator at `http://81.176.229.58:8080`. They do not establish learning utility,
causal-family generalisation, a successful SRE policy, or default promotion.
The full normative Stage 13 effectiveness gate remains open.

## Execution identity

- Provider/model: Codex subscription, `gpt-5.6-sol`, reasoning effort `low`.
- Policy: `simulator-v2-time-budget@1.0`; exact prompt, source, lockfile, runtime,
  configurations and estimator fingerprints are retained in each manifest.
- Context estimator: UTF-8-byte upper bound, 4000 units/module and 16000 globally.
- No retries. Every first attempt remains in its declared cell.
- Public world identity is unverified: neither immutable world-content hashes
  nor causal-family provenance were supplied. No eligible derived-knowledge
  activation is inferred from seed labels.
- Source was clean within `vadim/src`, `pyproject.toml` and `uv.lock` at execution.
  Unrelated user changes at the repository root were preserved.

## Smoke

- Source: `dc3154d7c80985bbf599709b551c8b2ba612dcb1`.
- Manifest: `memory-v2-integration-20260905-smoke-f8b512060803a92a`.
- Manifest hash: `69880fb8820888096947ae73d3e5c10753b48a2e8377c90e89b24af793cd98f5`.
- Conditions A0/A3, training seed 43, evaluation seed 44, replicate 0;
  2 decisions and 120 wall seconds per attempt.
- **4/4 attempts completed the horizon; 0/4 passed SLO.** All used one decision
  to advance through the horizon. Seed 43 uptime was 0.2609921885; seed 44 uptime
  was 0.2665841273. Each total infrastructure cost was 4317712903 minor RUB.
- Frozen A3 evaluation retrieved one training episode (2207 context units).
  A0 retrieved no memory. This verifies the read path; it is not a utility gain.
- The persisted snapshot sets contained 5 members for A0 and 8 for A3, including
  audit records. The original report incorrectly left their measured counts null
  because `_MemoryAdapter` hid the facade property. Commit `2e0b411` fixes this
  and adds a retained-attempt regression. Original smoke evidence was preserved.
- Independently recomputed report, all 12 lifecycle events, 11 artifact hashes,
  and both frozen snapshot sets passed verification.

Raw sanitized evidence is ignored under:
`artifacts/v2-memory-integration-2026-09-05/smoke/`.
The independent check output is `../smoke-verification.json` relative to it.

## A0–A9 and targeted integration matrix

- Source: `2e0b411` (resolve the full SHA from the manifest).
- Manifest: `memory-v2-integration-20260905-matrix-1a933e66d436dbe3`.
- Manifest hash: `34165367f9cecaf2ad636cc7fbeb1a326fc84e4e06971ac3317530155ac3c0ad`.
- 14 supported configurations: A0–A9; A6 minus world model, consolidation and
  structured retrieval; A8 minus tool knowledge. The unsafe minus-contradiction
  condition is explicitly unsupported.
- Training seeds 51/52, evaluation seed 53, replicate 0; **42 declared cells**.
- Fixed per-cell budget: **8 decisions, 120 wall seconds**. Incomplete horizons
  count as unsuccessful task outcomes. Budgets were sealed before execution.
- Directed cumulative and targeted contrasts are declared in the manifest.

**42/42 declared cells have terminal records: 41 completed, 1 interrupted;
0/42 passed SLO.** Training completed 27/28 attempts; evaluation completed all
14/14. There were no startup/finalization failures, exclusions, retries or
binding errors. Evaluation uptime was 0.2221234588 for every configuration;
none demonstrated an SLO improvement over A0. Successful-pair cost comparisons
are unavailable because no pair passed SLO.

The interrupted cell is A6-minus-consolidation, training seed 52, physical run
`st9EQHOvJlsZAme2c3Lkxc2h`: `per-attempt wall time budget exceeded` (120 seconds).
Its terminal record and partial trace are retained, with no replacement attempt
and no invented outcome. Its evaluation cell still completed from the bound
training evidence available to that condition.

Training seed 51 uptime was 0.2611101671 for 13 configurations and 0.2664550055
for A6-minus-consolidation; completed seed 52 attempts had uptime 0.2768240626.
These bounded runs do not establish an effective SRE policy.

Evaluation memory read-path measurements follow. Context items/units are sums
across decisions, so repeated retrievals count repeatedly; units use the declared
UTF-8-byte upper-bound estimator. Frozen member counts include audit records and
are not counts of decision-visible knowledge.

| Configuration | Decisions | Context items | Context units | Frozen members |
| --- | ---: | ---: | ---: | ---: |
| A0 | 8 | 0 | 0 | 66 |
| A1 | 8 | 8 | 22645 | 76 |
| A2 | 7 | 7 | 14964 | 88 |
| A3 | 8 | 8 | 17170 | 100 |
| A4 | 8 | 8 | 17209 | 100 |
| A5 | 7 | 7 | 15266 | 98 |
| A6 | 8 | 8 | 17461 | 98 |
| A7 | 8 | 8 | 17552 | 104 |
| A8 | 8 | 8 | 17554 | 104 |
| A9 | 8 | 8 | 17642 | 104 |
| A6-minus-world-model | 8 | 8 | 17645 | 104 |
| A6-minus-consolidation | 7 | 7 | 15225 | 84 |
| A6-minus-structured-retrieval | 8 | 8 | 17554 | 98 |
| A8-minus-tool-knowledge | 8 | 8 | 19278 | 98 |

All 14 frozen bindings were independently loaded and their reported member
counts matched SQLite. Verification passed for the report, all **126 lifecycle
events and 98 durable artifacts**. The report declares complete cell coverage
and retains `world_context_identity_unverified` as its evidence limitation.
Post-training storage contains 8 consolidation plans/applies and 8 maintenance
plans/applications. There are no activated lesson, world, playbook or tool
knowledge records: unknown context identity cannot qualify new evidence.

After persisting and printing the complete report, the CLI hung during Python's
default-executor shutdown: cancellation had left a Codex SDK synchronous RPC
worker waiting. The completed CLI processes were terminated with SIGTERM
(exit 143) after evidence verification. This is a process-shutdown defect, not
an additional failed or interrupted experiment cell. The separate adapter fix and
regression below address this defect; the historical report remains unchanged.

This historical source still reports null aggregate module-lifecycle and
stored-artifact counters. They mean unavailable measurements, not zero work.
The subsequent instrumentation smoke is a separate execution and does not
rewrite this matrix.

Raw sanitized evidence is ignored under
`artifacts/v2-memory-integration-2026-09-05/matrix/`; independent verification
is retained at `../matrix-verification.json` relative to that directory.


## Instrumentation and shutdown follow-up

Source `e35fd581b57318ff062fc01ea1d62c1e92268978` includes measured module/storage
telemetry (`fdd7865`) and the SDK cancellation fix (`e35fd58`). Owned SDK turns
stay shielded while public client close wakes their registered notification
waiters; the runtime drains the task before propagating cancellation. Borrowed
clients retain their caller-owned lifecycle. A pinned SDK router subprocess test
reproduced the old hang (5-second timeout) and passes with the fix.

The separate telemetry smoke declares manifest
`memory-v2-integration-20260905-telemetry-smoke-b869fcc3b987b146`, hash
`ab3f31bb7e4ee90ce341097c591417c7781774b8fcd2b855fe7e766c6e5177af`.
It repeats disclosed seeds 43/44, A0/A3, replicate 0, with 2 decisions and
120 wall seconds per cell. **4/4 completed, 0/4 passed SLO; CLI exited normally
with code 0.** Verification passed for 12 lifecycle events, 11 durable artifacts
and both frozen bindings. This diagnostic repeat does not replace old attempts.

| Phase | Condition | Construct | Read | Write | Contribute | Stored rows | Frozen members |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| training | A0 | 0 | 0 | 0 | 0 | 5 | — |
| training | A3 | 2 | 2 | 1 | 0 | 8 | — |
| evaluation | A0 | 0 | 0 | 0 | 0 | 9 | 5 |
| evaluation | A3 | 4 | 2 | 1 | 1 | 8 | 8 |

All consolidation counters were measured zero in this A0/A3 smoke. A3 evaluation
retrieved one frozen episode using 2205 context units. A3 constructs modules for
both its frozen reader and isolated writer, hence four construction events.
Stored rows include audit: training counts are cumulative within the condition;
evaluation counts cover the current attempt's isolated outputs. Frozen inputs
are counted separately. Contribution counts measure validated nonempty
contributions entering the global merge, not unique selected items. Finalization
counts are available per module at runtime; the retained-attempt schema has no
aggregate finalization field.

Artifacts: `artifacts/v2-memory-integration-2026-09-05/telemetry-smoke/` and
`telemetry-smoke-verification.json` in its parent directory.

### Deliberate cancellation probe

The same source `e35fd581b57318ff062fc01ea1d62c1e92268978` executed manifest
`memory-v2-integration-20260905-cancellation-probe-ab69007ae5cbadc5`, hash
`765fe5ab97d3251b18675fcef1aed10afd39a94d5ba045a69def85b8cecb7a4a`.
A0/A3 used disclosed training seed 43 and evaluation seed 44, replicate 0,
8 decisions and a deliberately short **12-second wall budget** per cell.

**All 4/4 attempts were interrupted by the declared wall-time limit; the CLI
then exited normally with code 0.** Each retained attempt reports one provider
request. No missing outcomes were invented, no interrupted attempt was replaced,
and all partial traces were retained. The independent check passed for all
12 lifecycle events, 7 durable artifacts and both bindings (3 A0 members,
4 A3 members). Module/storage telemetry was retained on interrupted paths too.

The retained stderr includes transport-closure diagnostics from Python 3.14
shielded futures and SDK cache/state warnings. These did not prevent normal
process exit or artifact verification; stderr is not claimed to be empty.

This verifies cancellation and process shutdown, not SRE success. Physical runs:
training A0 `BM7gc771sAmEQfHcTsZ0hBu6`, training A3 `iVWHVWX6HeHu3lbYmuAazVX1`,
evaluation A0 `Ym3yLaVPYSNUuq2e1GXh7t0g`, evaluation A3 `xxsFNJm8jPJ4FfeTQcKkXk5x`.
Artifacts are under
`artifacts/v2-memory-integration-2026-09-05/cancellation-probe/`; its parent
contains `cancellation-probe-input.json` and `cancellation-probe-verification.json`.

## Reproduction and remaining evidence

Use `scripts/build_v2_integration_manifest.py` and `uptick-agent evaluate-v2`
as documented in `EXPERIMENTAL_MEMORY_GUIDE.md`. Each new execution needs a new
sealed manifest and fresh output directory; the runtime rejects resume/reuse.
The exact historical source SHA must be checked out to reproduce its source pin.

Seeds 43/44 and 51/52/53 are now disclosed integration seeds; do not relabel
them as an unseen locked holdout in a later experiment.

The original Stage 0 B0/B1 balance baseline, locked causal-family holdout,
contamination audit, sufficient complete paired utility runs, comparative
retrieval precision, long-run retention evidence, and default-promotion/
rollback approval record are not supplied by these integration runs. New
mechanisms remain experimental and optional.
