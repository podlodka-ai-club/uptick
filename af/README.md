# af-sgr — Uptick v2 agent

An independent Python project for running an SGR agent against Uptick v2.
The agent discovers the world's tools and instructions at startup, makes structured
operational decisions, and learns from closed episodes across runs.

The universal core contains no simulator actions or gameplay rules. Environment-specific
starting strategies live in [prompts/uptickv2.md](prompts/uptickv2.md).
See [architecture](docs/architecture.md) for the runtime, context, memory and boundaries.

## Setup

Run from `af/` with Python 3.12+ and `uv`:

```sh
uv sync --extra codex
cp agent.example.yaml agent.yaml
codex login
unset OPENAI_API_KEY CODEX_API_KEY
export UPTICK_PARTICIPANT_TOKEN='YOUR_PARTICIPANT_TOKEN'
```

Edit `agent.yaml`: set `environment.endpoint` to your simulator, `environment.seed` to
its world number, and `run_store.path` to your output directory. The checked-in example
uses a local server at `http://localhost:8080`; it contains no participant credentials.
The default example uses Codex Sol / medium for decisions and Terra / high for learning.

```sh
uv run --extra codex uptick-agent run --config agent.yaml --live
```

`--live` explicitly enables model and simulator calls. Configuration is typed YAML;
paths resolve relative to the configuration file. Environment variables supply only
secrets named by `from_env`. The CLI does not load `.env` files automatically.
The participant token is sent in `/v2/start` and removed from saved/model-visible data.

`environment.step_limit: null` runs until the world completes or execution fails.
A positive limit caps local decisions; reaching it does not complete the simulator.
Each invocation creates a new world. Resuming an interrupted run is not implemented.

## Memory and learning

The example enables SQLite memory and `learning.trigger: after_run`.
Keep `memory.path` unchanged across runs to accumulate experience. A v6 run store can
hold multiple runs; separate directories are optional. Use a new directory when the old
one contains v5 or mixed-version traces. Copying discovery caches is not required for shared memory:
compatibility depends on the published contract, not generated profile wording.

A closed Episode records an assessed decision and its observed outcome. After a run,
a separate learner reads Episodes and active Lessons from Memory, then proposes a
Lesson or returns `no_lesson`. Activation requires deterministic evidence checks;
the example requires evidence from at least two distinct worlds. Repeating one world
does not provide another independent evidence group.

To start with clean but enabled memory, choose a new SQLite path. To freeze existing
memory, remove `learning` and `reasoners.learner`; to disable it, also use
`memory: {backend: none}`. Do not run simultaneous writers against the same database.
No run histories or trained memory are distributed with this project.

## Prompt and context

`environment.prompt_file` adds operator guidance to the universal core prompt; it
replaces neither the core nor the world's instructions. The text is read once and
saved in the manifest for replay. Remove the field to run without operator guidance.

The model receives current observations, its working state, relevant recalled
experience and available tool schemas. Requested tool responses are preserved after
credential removal. History is bounded by snapshot count and explicit release, not
silent truncation of requested data. Large queries can still produce large contexts.

The adapter discovers tool names, schemas, endpoints and authentication from the world.
`execute_batch` groups independent known calls and executes them sequentially with
failure reporting. `execute_program` reuses bounded compositions of existing tools.
Neither grants shell access or arbitrary network access.

## Results and diagnostics

Each `run_store.path` contains:

```text
trace.jsonl             operational and learning events
manifests/              configuration, model and memory provenance
bootstrap/              extracted environment profiles
discovery/              published contracts and discovery receipts
```

The `discovery/launches/` receipt retains the simulator run ID even if discovery fails.
Final run events preserve the world's public result, including score, uptime and costs
when published. A local process finishing is not sufficient evidence that the world
completed: check its final status and evaluation.

[Trace Viewer](tools/trace-viewer/README.md) opens saved traces locally without a server.
Export a completed or failed ordinary run directly from its trace and manifest:

```sh
uv run uptick-agent export-corpus \
  --trace .artifacts/runs/trace.jsonl \
  --run-id RUN_ID \
  --output .artifacts/run-corpus.json
```

The CLI also provides frozen decision replay and offline comparison of prepared
reports. Use `uv run uptick-agent --help` and each subcommand's `--help`.
Replay checks a saved decision context, not a counterfactual world trajectory.

The old live `benchmark` and `compare` commands have been removed. Their fixed-profile
workflow did not reproducibly pin the current discovered tool descriptions. Saved
reports use current v2 score, cost and uptime fields; old profit-based reports are not
accepted as equivalent. Report generation is available as the Python function
`summarize_experiment(spec, attempts)`; ordinary `run` emits its result and trace,
not an experiment `summary.json`. Historical v5 operational traces remain readable.

Runtime failures are recorded with stage and provider telemetry. Some provider errors
currently terminate a run without retry, including model-capacity errors. After-run
learning does not run after an operational failure; already committed Episodes remain.

## Development

```sh
uv lock --check
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
node --test tests/trace_viewer/core.test.cjs
```

Tests use scripted reasoners and mocked HTTP; they do not call a live model or simulator.
The project builds independently of sibling agents. Local configuration, credentials,
SQLite databases, caches and run artifacts are ignored by Git. See [AGENTS.md](AGENTS.md)
for contributor boundaries.
