# Uptick Trace Viewer

A dependency-free, read-only browser viewer for saved Uptick AgentCore artifacts.
It runs directly from `file://`; no backend, build step, public network, or package
installation is required.

## Open it

Open `tools/trace-viewer/index.html` in a current browser. Drop files onto the page or
use **Open artifacts**. A trace schema v5 or v6 `trace.jsonl` containing SGR v2 decisions is
the minimum input. Other or mixed trace schemas and legacy SGR v1 decision envelopes are
rejected with a clear error. The same selection may also contain:

- one experiment report v5 `summary.json`;
- one paired comparison report v4;
- one resolved experiment spec v3;
- one decision corpus v3 (experiment or ordinary run);
- one environment bootstrap artifact;
- any number of run manifests.

An ordinary-run corpus may omit the experiment spec and its hash. The viewer preserves
missing seed/repeat metadata and does not create experiment provenance.

A later selection can add companions to the loaded trace. Selecting a new trace
replaces the current in-memory session. Reloading or closing the tab discards all
loaded data.

## Layout

The workspace has four tabs. **Decisions** opens by default.

- **Decisions** — a compact list of every recorded decision on the left and the selected
  decision as a readable story on the right.
- **Map** — the same decisions grouped by exact SGR v2 strategy, drawn as a pannable graph.
- **Diagnostics** — charts of saved measurements and the wall-time breakdown.
- **Memory & experiment** — pinned memory view, learning streams, and loaded companions.

Below the tabs, the overview strip shows the simulator's published score, uptime and
total cost from `run_finished.result_details.final_state` (or a companion's saved final
outcome), plus runner and simulator completion. These values are not recomputed. Missing
outcome fields remain missing; site status, SLO and runner completion are separate facts.
**Run details** opens provenance, model telemetry, and the published outcome.

## Reading a decision

The list shows iteration, capability name, the saved observation summary, the pre-action
simulator time, and a few exact markers (failed observation, policy rejection, structured
incident code, terminal decision, following review status). Search matches saved
reasoning, arguments, observations, memory brief, and retrieval IDs. **Filters** opens
iteration range and category toggles.

The story on the right keeps the stored causal unit in reading order. Long saved
observation summaries, recalled memory, and evidence/alternatives expand on demand;
the complete text stays available. The chosen strategy and expected result remain
visible without opening these details:

1. **What the agent saw** — pre-action `EnvironmentState` status, pending operations, step,
   the latest observation, and the compact `MemoryBrief` (lessons, similar episodes,
   contradictions) with retrieval diagnostics.
2. **Why it chose this** — the SGR v2 envelope: this decision's `previous_verification`
   (labelled with the earlier iteration it assesses when `open_decision.step` links them),
   facts, competing hypotheses, contradicting evidence, strategy, expected result,
   verification plan.
3. **Action** — capability name, arguments, requested versus applied advance, policy result.
4. **What happened** — the observation from the same `decision_trace`, batched or program
   sub-results when present, and any failure record.
5. **Reviewed on iteration N** — the following decision's `previous_verification` when the
   next `AgentWorkingState.open_decision.step` links back exactly; otherwise the story says
   the outcome was not assessed and why.
6. **Episode** — episode close and memory commit events linked to this decision.

Below the story, **State, telemetry and raw data** holds the full decision view,
`AgentWorkingState`, `MemoryBrief`, environment profile, model telemetry, hashes, the
capability catalog, and the raw `DecisionTrace` line. Raw payloads are formatted only
when opened; parsing the trace still happens at load time.

Traces do not contain the full retrieval query or per-record scores, so the viewer does
not invent them. Competing hypotheses are displayed as considered alternatives, never as
selected branches.

Use `↑`/`↓` or `j`/`k` to move through visible decisions, `Home`/`End` to jump, and
`/` to focus search. Focused decision rows retain focus when navigating; native
controls keep their own key behavior. Tabs follow the ARIA tab pattern: `←`/`→` moves between them.

## Map

The **Map** groups decisions by the exact SGR v2 `strategy_started_step`. Each node reads
as input → decision → result → next review. Long alternating time-advance / log-read runs
under one exact strategy are folded only when they contain no fix, scale, deployment,
failed observation, DDoS, or contradicted review. The folded node exposes its condition
and exact iteration range and can be expanded. Use wheel/trackpad or `+`/`−` to zoom, drag
to pan, `0` for Fit all, and `f` to center the selected decision. The side panel shows a
compact version of the same story with a link back to the Decisions tab. If that
decision is excluded by active filters, the link explicitly offers to clear them.

## Diagnostics

Charts plot only saved measurements; sparse simulator points remain points, and each
cumulative series stops at the first missing or unsafe value. **Where wall time went**
lists run wall clock, bootstrap, decision model, memory retrieval, Environment, transport,
and learner work. These bars are not stacked: Environment includes transport, while
bootstrap and learning may occur outside the operational run.

Composite observations expose clocks only when a saved batch response or program-selected
`data` / `data.clock` includes them. Parallel responses use the latest saved timestamp.
The raw composite response remains available under **Observation data**.

Money fields ending in `_minor` are displayed as a compact count of minor units and
the exact saved integer. The viewer does not assume a currency exponent. It refuses
to load an unsafe integer on browsers that cannot expose the original numeric token.

## Trust boundary

Artifacts are treated as untrusted text. The viewer uses `textContent`, never renders
trace strings as HTML or Markdown, creates no clickable artifact URLs, and has a CSP
that denies network connections. It does not persist data or import the Python agent
runtime. Styling uses system fonts only; there are no external assets.

## Tests

```sh
node --test tests/trace_viewer/core.test.cjs
uv run pytest tests/test_trace_viewer_static.py
```

The complete repository check set is documented in `AGENTS.md`.
