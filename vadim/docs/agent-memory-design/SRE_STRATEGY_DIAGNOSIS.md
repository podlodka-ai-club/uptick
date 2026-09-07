# SRE decision-efficiency diagnosis — 2026-09-05

Status: implementation correction passed local tests; live verification pending.
No new successful SRE result is claimed.

The two policy-1.2 development attempts stopped at the diagnostic harness's
2400-second wall timeout. This is our limit, not a simulator-imposed limit.
The independent runner limit is 160 decisions. Public startup documentation
states that requests also account for real elapsed time, while explicit time
advances can move the simulated clock further. A model response can therefore
consume incident time as well as experiment wall time.

## Observed timing

| Attempt | Completed decisions | Reported provider seconds | Mean seconds per completed decision | Simulated horizon covered |
| --- | ---: | ---: | ---: | ---: |
| dev-02, Sol low | 156 | 2341.45 | 15.01 | 20.42% |
| dev-04, Sol high | 132 | 2351.20 | 17.81 | 20.01% |

Provider totals are partial: the final interrupted request has no complete
usage report. These totals include the observed provider boundary, not a
measurement of model computation alone. Nearly all wall time was spent there;
raising reasoning effort produced fewer completed decisions without finishing
the horizon. Neither run demonstrates an SLO pass.

## Confirmed implementation defects and constraints

The public traces show a serial response to aggregate overload. Dev-04's
iteration 62 measured 632 load units against 200 capacity; iteration 64's
plan proposed provisioning enough capacity for that load, while its actual
action read the type catalog. The first creation followed at iteration 65, but subsequent
decisions repeatedly added a single 100-unit backend and waited/checked it.
Later, iteration 131 still measured 3828 load against 600 capacity. Dev-02
ended with 3851 load against 1500 capacity. These are observed deficits, not
claims about the workload's hidden cause or a guaranteed optimal remedy.

The backend type catalog was read seven times in dev-02 and six times in
dev-04; its result payload was identical across those reads. Each operation
was inspected once, so repeated polling of the same operation is not the
observed cause. The proposed actions in retained provider results matched
effective trace actions in both policy-1.2 attempts. The prior policy-1.1
pending-wait override must not be attributed to these corrected attempts.

Dev-02 used 38 explicit advances, 20 of them 300-second pending waits;
dev-04 used 30 advances, 15 of them pending waits. Only the remaining advances
could cover large portions of the horizon, and error-stopping waits often
ended early: 9/18 and 7/15 respectively. Extending the wall timeout alone does
not repair the plan.

The canonical runner carried actions and short result summaries into the next
request, but discarded the decision's working assessment, hypothesis and plan.
The Codex provider starts a fresh thread for each structured request. A plan
written in one decision therefore disappeared unless separately retrieved from
enabled memory. The no-memory baseline has no such retrieval. This is a
continuity defect; the size of its effect on SRE outcomes requires a new run.

The v2 adapter also overwrote its terminal result for `RUN_COMPLETED` and
`RUN_NOT_RUNNING` with a generic error result. This can prevent correct loop
termination when the API reports an already stopped run. The two timed-out
attempts had no failed action results, so this defect does not explain their
early-horizon stalls. Final status must still come from the authoritative
overview; a stopped run is not automatically a successful run.

Lossless compact JSON rendering reduces total user-context bytes in retained
requests from 3361572 to 2228461 for dev-02 and from 2552539 to 1685733 for
dev-04, about 34%. These counts exclude system instructions and response
schemas. They are not token savings or a measured latency improvement.

The environment startup document and tool schema remain external, fixed inputs.
Any decision-continuity change belongs to the generic decision context and
must carry prior model output as revisable, untrusted data. It must not teach
the runner simulator commands, copy evaluator labels, or make a prior plan
authoritative. Within-run working state must be identical in subsequent
memory/no-memory comparisons and reset between runs.

The correction carries one sanitized prior validated decision, bounded to
6000 UTF-8 bytes with an explicit truncation marker. A no-memory test in an
unrelated toy environment verifies a multi-step plan, latest-only carry,
run reset and exact trace/context parity. Generic instructions ask the model
to revise its plan from current evidence, retain essential commitments, size
changes to observed deficits and overlap only contract-permitted independent
actions. They contain no simulator commands or learned world signatures.
The locked suite passed **661 tests**, with two opt-in live skips; Ruff passed.

## Experiment boundary

Retain every attempt and distinguish development diagnostics from a controlled
memory-utility comparison. Equal seeds are not independently verified immutable
world/family identities. A new baseline diagnostic can test corrected runtime
behavior, but cannot by itself close the held-out transfer gate.
