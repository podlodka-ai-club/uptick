# Agent comparison: `vadim` and `simple_agent`

**Status:** comparison of two repository trees, not an identified comparison
with Alex's current agent. The earlier provisional “Alex” label is withdrawn.
Shared Team Uptick Sync 3 notes describe a simple/oracle baseline separately
from Alex's multi-step agent, and Sync 4 describes additional memory/macros.
Those meeting reports do not identify a matching source revision or prove that
this repository's `simple_agent` is the oracle implementation. Its code/test
observations below remain attached to the inspected tree only.

Sources: [Sync 3](https://notes.granola.ai/d/6f474ea2-9ae9-4928-a4dc-f8dcb5f0fd5b?list_id=6353c08b-78c1-4bce-95a5-555a7d97076a),
[Sync 4](https://notes.granola.ai/d/d02f95f9-15fb-407e-b32e-1727343ef805?list_id=6353c08b-78c1-4bce-95a5-555a7d97076a).
These are shared meeting summaries, not verbatim transcripts or independent
performance verification.

## Scope and revisions

This comparison covers the two agent trees as they exist in the shared repository. It does not modify `simple_agent`, call an external model, call a simulator endpoint, or claim a performance result that was not measured under a common protocol.

| Tree | Location | Revision used | Identity / notes |
|---|---|---|---|
| `vadim` | `/Users/mingazhev/Repos/podlodka/uptick/vadim` | `b3596cebb136bb2872c805dac527ca3f2407852b` | This agent’s tree; instructions in [`vadim/AGENTS.md`](</Users/mingazhev/Repos/podlodka/uptick/vadim/AGENTS.md>) |
| `simple_agent` | `/Users/mingazhev/Repos/podlodka/uptick/simple_agent` | `dc7ac3e20022fb7cebb17e42ac4f00c49e9f5806` | Sibling baseline tree; Alex identity withdrawn; no sibling `AGENTS.md` was found |

The sibling revision is the latest commit touching that tree (`fix(cli): allow benchmark trace naming`). Its recent history also includes the extensible SGR baseline and the separation of working context from durable memory. The `vadim` revision includes the completed v2/memory integration work and the Codex cancellation boundary fix recorded by the parent task.

Both lockfiles resolve the same relevant versions in the available environment, including Pydantic 2.13.4, HTTPX 0.28.1, OpenAI 2.54.0, `openai-codex` 0.147.0, and pytest 8.4.2. This reduces dependency variance for a v1 comparison, but it does not make the agents equivalent: their prompts, schemas, defaults, providers, and evaluation harnesses differ.

## Functionality and architecture

| Area | `simple_agent` | `vadim` | Practical comparison consequence |
|---|---|---|---|
| Decision schema | One typed v1 `AgentAction` union with 12 simulator actions and one `NextStep` response. | Generic, explicit `V1NextStep` and `V2NextStep`; typed v2 inbox/control actions; richer trace and outcome models. | The common surface is the v1 action family. A v2 comparison would require adding v2 support to the sibling or inventing a baseline adapter. |
| Runner | Small sequential loop: start, observe, recall, decide, execute, remember, finish. | Retains the v1 loop while adding versioned responses, richer observers, operation links, objective metrics, and evaluation/runtime boundaries. | v1 loop behavior can be aligned; v2 lifecycle and evidence behavior cannot be treated as an accidental drop-in replacement. |
| Memory | `none`, in-memory lexical memory, or JSONL memory. Carrying JSONL across runs is an explicit benchmark option. | Persistent stores and typed episodic/lesson memory, immutable snapshots and frozen evaluation bindings, audit, validation, consolidation, retrieval, maintenance, and compatibility paths. | The sibling’s memory conditions are useful as a small pilot. They are not equivalent to frozen, isolated v2 evaluation conditions. |
| Simulator | HTTP v1 client/environment with overview, metrics, logs, resources, deployments, scaling, fixes, operations, probes, time advance, and economy. | Keeps v1 and adds a typed v2 client/environment with inbox/control commands, asynchronous operation handling, auth resolution, v2 time stopping, uptime/SLO/cost outcomes, and version-aware policy. | A fair shared endpoint protocol exists for v1 only. The v2 action and outcome contracts are absent from the sibling. |
| Provider boundary | OpenAI structured parse and Codex subscription wrappers; no common telemetry contract. | Provider-neutral structured/text boundaries, prompt traces, provider settings/policy pins, and `LlmCallTelemetry` coverage fields. | Provider/model/account mode, prompt, token accounting, and retry policy must be frozen explicitly for a comparison. |
| Evaluation | `ExperimentRunner` aggregates v1 balance statistics over sequential seeds; carry-memory is optional. | Versioned manifests, ordered paired matrices, immutable bindings, append-only attempt lifecycle, first-attempt primary selection, failure/missing-cell accounting, and v2 SLO/cost reporting. | Existing sibling summaries cannot be compared directly with the v2 report or used as a promotion result. |
| CLI | Runs the v1 agent and benchmark, selecting memory and provider options. | Preserves explicit v1 selection, defaults to v2, and provides `evaluate-v2` preflight/artifact wiring. | A v1 run must explicitly select `--simulator-api-version v1` in `vadim`; otherwise defaults differ. |
| Tests and evidence | 40 tests passed in the redirected run, one simulator integration test skipped without an endpoint; local pilot artifacts exist. | Broad unit/contract coverage for v1/v2, memory/evaluation invariants, telemetry, and live v2 evidence artifacts. | Test counts measure different scopes. They are evidence of implementation coverage, not agent quality. |

The sibling is smaller and easier to inspect for a basic v1 SGR loop. `vadim` has stronger controls for typed v2 actions, reproducible evaluation, memory isolation, provenance, failure retention, and provider accounting. Those controls also introduce more configuration and validation that must be held constant when measuring behavior.

## Verification performed

The sibling tests were run with API keys unset and generated Python/ruff caches redirected outside the repository:

```text
env -u OPENAI_API_KEY -u CODEX_API_KEY \
  UV_PROJECT_ENVIRONMENT=/private/tmp/uptick-simple-agent-locked-venv \
  UV_CACHE_DIR=/private/tmp/uptick-uv-cache \
  PYTHONDONTWRITEBYTECODE=1 \
  uv run --extra codex --locked pytest -q -ra \
  -o cache_dir=/private/tmp/simple-agent-pytestcache
```

Result: **40 passed, 1 skipped in 1.35s**. The skipped test is the live simulator integration test because `UPTICK_INTEGRATION_SIMULATOR_URL` was not set. This is the exact locked `uv` invocation requested for the sibling, with public dependencies installed into `/private/tmp`; no API keys were present. The run therefore verifies the sibling’s locked test environment, while the skipped integration test still leaves live simulator behavior unverified.

Sibling lint/format checks also passed with redirected cache:

```text
ruff check .       -> All checks passed!
ruff format --check . -> 27 files already formatted
```

No external API or model run was performed for this comparison. The starting `vadim` revision had 504 passed and 2 skipped. After the architecture/xmemory changes in this continuation, the parent reran the full suite: **524 passed, 2 skipped in 6.37s**, plus a separate current live v2 adapter check (**1 passed**). The historical quality measurements below retain their original source revisions; these current regression checks do not replace them.

## Existing artifacts and what they show

The sibling’s [`memory-study` report](</Users/mingazhev/Repos/podlodka/uptick/simple_agent/artifacts/memory-study-20260827-01/report.md>) is a two-seed, one-repeat pilot using a local Codex subscription session, `max_steps=60`, and three conditions. It reports these historical v1 balances:

| Condition | Seeds | Mean balance, billion minor | Mean steps | Mean time, seconds |
|---|---:|---:|---:|---:|
| `none` | 1, 2 | 83.899 | 58.5 | 713.0 |
| `in-memory` | 1, 2 | 73.289 | 49.5 | 613.9 |
| `jsonl-carry` | 1, 2 | 78.061 | 57.5 | 949.6 |

The report itself correctly labels the result as a pilot. It notes Codex nondeterminism, one repeat per condition, the confounding of JSONL carry with run order, and the absence of recalled-memory IDs in traces. Those numbers therefore describe that artifact and cannot establish a general memory effect or rank `simple_agent` against `vadim`.

The sibling’s simulator integration test was not run against a live endpoint, and neither the test nor the artifacts contain an uptime/SLO outcome. Its traces contain v1 actions such as `advance_time`, `apply_fix`, `get_logs`, `get_overview`, and `probe_page`; they contain no v2 `get_inbox`, `get_control_commands`, `control_command`, or `advance_time_v2` actions.

The current `vadim` v2 artifact is [`matrix/report.json`](</Users/mingazhev/Repos/podlodka/uptick/vadim/artifacts/v2-memory-integration-2026-09-05/matrix/report.json>). Its recorded summary is 42 attempts, 41 completed and 1 interrupted, with complete declared coverage and an exploratory report. Every condition has zero SLO-passing first attempts. The report explicitly records `world_context_identity_unverified`, so it is evidence about the v2 harness and run outcomes, not a successful SLO comparison or a cross-agent score.

## Apples-to-apples decision

A controlled **v1** benchmark is possible in principle. Both trees can use the same v1 simulator endpoint and the same broad action family, and their lockfiles match the relevant dependency versions. The existing artifacts are not that benchmark.

A controlled **v2** benchmark is not currently possible between these trees without changing `simple_agent` to implement the v2 API or constructing a compatibility baseline that would change its behavior and violate the requested comparison boundary. No such baseline should be invented for this report.

The minimum fair v1 protocol is:

1. Use the same live v1 endpoint/world version and set `vadim --simulator-api-version v1` explicitly.
2. Freeze the exact agent version, legacy system prompt, provider, model, subscription/API mode, provider settings, retry policy, timeout, and `max_steps` for both trees.
3. Use the same declared seeds and a preregistered sample size with enough independent repeats to estimate variance (for example, 5 seeds × 3 repeats); this is an illustrative design, not an established power analysis.
4. Run conditions in paired, interleaved order. Give every seed a fresh memory namespace for the no-memory and within-run-memory conditions; make cross-run carry a separately declared condition.
5. Retain startup failures, interrupted runs, provider failures, and incomplete traces. Compare only the same terminal definition and the same metric units across both agents.
6. Record source/dependency/prompt/provider pins and exact trace/action data, including recalled-memory identifiers where memory is being evaluated.
7. Report balance only for the shared v1 objective. Do not mix it with v2 uptime, SLO, or conditional-cost metrics.

Until that protocol is executed, the defensible conclusion is architectural: `simple_agent` is a smaller v1 baseline with a useful but underpowered memory pilot; `vadim` supplies the versioned v2 and evaluation machinery. There is no evidence here that one agent performs better than the other.

A comparison with Alex requires his actual implementation and revision; the
shared meeting summaries cannot substitute for them. This report makes no
ranking, causal memory claim, SLO claim for the sibling, or claim that current
v2 outcomes are a successful evaluation.
