# Working memory and the minimum agent harness

Date: 2026-09-05. Supporting research for the memory hackathon, not a new
orchestration roadmap. Separate agents investigated general-agent controls and,
at the user's request, file handoff using `gpt-5.6-sol`.

## Findings from this agent

Dev-05 carries the previous decision correctly in all 159 eligible subsequent
requests. Nevertheless, a catalog read at step 117 has disappeared from the
context at step 124. The model asks again whether a larger instance exists,
although its previous observation already answered that question. A carried
plan is not a substitute for the evidence used to make or reject that plan.

The input alternates between too much fresh evidence and too little historical
evidence: user contexts range from 5,676 to 104,595 characters. The memory
orchestrator's retrieval budget does not bound the whole decision request.
Forty-one of 159 executed actions read logs; broad pagination often returns
old successful traffic while the agent searches for a current failure.

Episodic memory already stores full sanitized transitions when enabled. Its
lexical retrieval returns short canonical-JSON prefixes, however, rather than
providing an exact lookup for a known observation. A 512-character result
excerpt can lose status, summary or the useful part of a tool payload.
Cross-run exclusion of failed/interrupted episodes is a separate intentional
policy; it must not be weakened merely to obtain a positive memory comparison.

## What established agents actually do

| Mechanism | Primary evidence | Transfer decision |
|---|---|---|
| Detect repeated tool calls | OpenCode detects repeated identical tool/input pairs and asks for a `doom_loop` permission. [Processor source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/processor.ts) | Consider a warning only when outputs and cursors also show no progress. Repeated pagination and polling are not inherently loops. |
| Separate raw output from model context | Codex applies a model-output truncation policy separately from raw output/logging. [Context source](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/context.rs) | Keep full evidence recoverable while bounding the view; do not silently turn a partial view into a complete result. |
| Compact tool history | OpenCode supports automatic compaction, pruning and context reserve. [Configuration](https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/config.mdx) | A small bounded observation view is useful now. Persistent provider-thread compaction is a larger change. |
| Small index, details on demand | Claude Code loads the first 200 lines or 25 KB of `MEMORY.md`, whichever is smaller, and keeps detailed topic files separate. [Memory documentation](https://code.claude.com/docs/en/memory) | Copy the index/detail separation, not the literal directory structure or those particular limits. |
| External structured notes | Anthropic recommends compaction and structured notes outside the context, preserving critical details before optimizing compression. [Context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Separate model-authored working notes from immutable observed evidence. Summaries need source references and explicit coverage. |

These are shipped mechanisms or vendor guidance, not evidence that adding all
of them improves this agent. Exact thresholds need local experiments.

The minimum local set should distinguish **stopping work** from **making
progress**. The runner already has a finite decision count (default 160), the
Codex client permits one structured-output retry, and dev-05's diagnostic
harness enforced a 2,400-second wall limit. Its failure therefore does not show
an unbounded loop. Those limits stopped an expensive unsuccessful run; they did
not make the policy effective. Keep three responsibilities explicit:

- Hard request/run limits and visible remaining budget prevent unbounded cost.
- A no-progress signal needs repeated action, unchanged returned evidence and
  unchanged cursor/state; it should prompt revision without banning valid polls.
- Recoverable observations and source-linked working notes preserve evidence
  across decisions. Measure their effect on completed objectives and total cost.

The current work changes the third responsibility. A general loop governor and
model-tier comparison remain deferred until evidence justifies their cost.

## File handoff: the useful part is the retrieval contract

The decision provider has an isolated, ephemeral workspace and no file/tool
access. Giving it a path does not make an observation retrievable. Opening the
repository or experiment directory would also break the intended information
boundary. Persistent provider sessions and native compaction would require a
different provider lifecycle.

Use a runner-owned short-term observation store instead. Files are optional:
Anthropic's client-executed memory tool can use files, databases or other
storage; bounded access and scope are the relevant contract.
[Memory tool documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool).
OpenCode supplies a useful concrete pattern: bounded output with a managed
reference to the full result and explicit handling of lossy retention.
[Tool implementation](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/tool.ts),
[context specification](https://github.com/anomalyco/opencode/blob/dev/CONTEXT.md).

Keep the objective, current public state, budget, latest result envelope,
bounded previous plan, and a small observation index inline. Each retained
observation needs its source step, success/failure and truncation status.
Historical evidence must not claim to be current state. A model-authored
hypothesis is revisable data, not an established fact.

If a bounded preview demonstrably hides necessary evidence, the next isolated
experiment should add one generic runner action:

```text
read_observation(issued_ref, offset, max_bytes)
```

It should read only sanitized outputs actually returned to this run, using
refs already issued to that model. It must not accept filesystem paths, SQL,
arbitrary store IDs, another run's references or evaluator artifacts. Return
bounded bytes, coverage/next offset, provenance and a historical-data marker.
An unavailable reference is an explicit failure, not evidence of absence.

For an in-process experiment, an in-memory backing store is enough. If crash
recovery becomes a requirement, reuse the existing SQLite storage under a
runner-owned namespace. Short-term continuity must be identical in the
persistent-memory and no-persistent-memory conditions.

## Hackathon ordering

1. Preserve useful short-term evidence and measure whether it changes decisions.
2. Keep the result status and summary outside truncated bodies; test run reset,
   parameter identity, secret handling and an unrelated environment.
3. Measure bounded history and generic guidance separately on public-prefix
   probes. Report extra prompt bytes as a cost, not as a free improvement.
4. Add exact read-by-reference only after a decisive fact is lost by the preview.
   Use a synthetic large observation and a retained public prefix; measure
   successful recall, total model calls/tokens and forbidden-reference rejection.
5. Then evaluate persistent episodic/learned memory utility with the same
   within-run mechanism in both conditions.

Defer autonomous governors, filesystem tools, a new summarization service,
provider-session rewrites and automatic bans on repeated actions. They are
not prerequisites for demonstrating useful memory.

## Experiment update

The [exact-read pilot](OBSERVATION_HANDOFF_RESULTS.md) completed all nine episodes.
It recovered all three omitted facts with issued refs, but required three model
calls per answer and increased input tokens by 34.3% versus full inline evidence.
The 90.1% reduction in peak user-message size did not reduce total cost. Treat
file/observation handoff as a retrieval design problem, not an automatic context
optimization. The component remains experimental; the next gate is reuse and
navigation across decisions without answer-aware snippet selection.


## Memory-first checkpoint (2026-09-06)

Owner requested a simpler proof before economic tuning. Exact20trainingrecords
were verified against SQLite, four known situations retrieved directly relevant
rank1episodes, and a five-question guided probe read successes/failures and abstained
on a novel case5/5. An ordinary-agent four-case paired test then recovered4/4
with raw experience versus2/4without, with identical requests except memory items.
This supports utility on the small development family; full SRE benefit remains
unestablished. See [persistent recall results](PERSISTENT_RECALL_RESULTS.md).

The immediate short-term candidate uses spare inline budget for complete redacted
observations while preserving existing retained entries. No file tools, provider
session rewrite or general loop governor were added. Keep it opt-in until the
ordinary-policy public-prefix decision probe is reviewed.
