# Environment instructions and tool ownership

The 2026-09-05 user requirements are implemented at the runtime boundary:
world tools belong to the environment, and the world description is a fixed
external startup input. Learned evidence remains a separate memory input.

## Current ownership

`environment/EnvironmentDecisionSpec` supplies a typed response model, startup
briefing and optional objective. It freezes the response JSON Schema and exposes
an immutable public input/fingerprint. Schema mutation is rejected. It contains
no environment instance or hidden evaluator state.

`decisions/runtime.py`, `runs/execute.py` and `runs/runtime_results.py` preserve
arbitrary declared action parameters, public working state and objective metrics.
The runner revalidates serialized decisions before execution, including values
made with `model_construct`. It owns no SRE action union or SRE state reducer.
The environment projects public state; the runner copies it. Generic runtime
imports do not load the simulator or its fixed action schemas.

`llm/decision_model.py` requires an explicit response model. Its universal core
covers evidence, a falsifiable hypothesis, a short plan and choosing one declared
action. It has no default SRE schema or world description. The composed prompt is
read-only after construction; there is no late binding/mutation hook.

`simulator/actions.py` and the simulator adapter own the existing SRE action
surface and execution. Historical typed schemas/imports remain compatibility
facades; they are not the canonical runner contracts. Native memory needs no
world-specific change for an additional tool or environment.

## External startup and evaluation

Ordinary v2 CLI runs start the environment, preserve its sanitized public start
response and fixed specification, construct the model, then pass a one-shot
prestarted session to the runner. Benchmarks repeat this lifecycle per world;
memory carry-over remains explicit. A model is never reused by silently changing
its prompt for another world.

The effective description is the real server's sanitized `commands_markdown`.
A missing/empty document fails before a model decision; no local v2 briefing is
substituted. The goal is read from those startup instructions. Later observations
or memories cannot change this description or add capabilities. The initial
local-briefing checkpoint `c05864f` is retained as history, not the final origin
contract.

For paired v2 evaluation, both the offline manifest builder and `evaluate-v2`
require a previously observed sanitized document through `--environment-briefing`.
The composed prompt hash is declared before external calls. The actual startup
text must match that pin before provider construction; the tool schema must
match the source-pinned v2 adapter. Actual start observations/specifications are
retained even for rejected inputs. Their hashes enter the sealed attempt trace,
and the verifier checks those links. Rehashing a replaced startup file does not
make it match the retained report.

Source/API/startup-schema hashes identify code and public inputs. They are not
immutable world-content identities or causal-family labels and cannot activate
otherwise ineligible SRE world knowledge. See `PUBLIC_TOOL_COVERAGE.md` for the
public API and observed description fingerprints.

## Acceptance evidence

The third-world regression uses a thermostat tool unknown to the SRE action
union. The actual provider request advertises it, the runner executes its exact
arguments, the second request retains the first action, and both transitions
survive reopening native SQLite. Undeclared tools and malformed arguments are
rejected before execution. Runtime step serialization preserves subclass fields.

Root verification found and fixed legacy environment detection/factory arity,
startup artifact binding, the historical CLI default schema, and missing-spec
failure finalization. Final full suite: 567 passed, two opt-in live tests skipped. Review pass 2 is
clean. All 56 historical schemas and identities match, and all four earlier
sealed SRE reports still verify. Formatting of six files preserved their Python
ASTs. This is not a new live decision-quality measurement.

The next bounded step is completing public diagnostic query parameters, then
running a separately sealed regression of the real learning cycle. Historical
learning and SRE results remain tied to their original frozen source and inputs.
