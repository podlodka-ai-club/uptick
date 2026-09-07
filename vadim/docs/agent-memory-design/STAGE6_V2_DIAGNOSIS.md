# Stage 6 v2 evidence gate investigation

The v2 development pilots do not establish that lessons beat episodic memory.
They used no memory and seed 42 has already informed policy changes. It cannot
serve as an unseen holdout. The investigation below explains concrete obstacles
before implementing further optional modules; it is not a positive evaluation.

## Observed obstacles

1. The public simulator start contract supplies no immutable environment/world
   content hashes. `LessonRunDeclaration` requires both hashes, and candidate
   acceptance requires two distinct immutable contexts. Inventing hashes from
   seed numbers, endpoint URLs or API schemas would defeat that check. Unknown
   identity must therefore disable eligibility, with a visible report reason.
2. V2 objective metrics currently occur on `get_overview` and `get_metrics`
   responses. The generic transition assembler computes deltas only for metrics
   present on both adjacent observations. Inspection of the retained traces for
   attempts 3, 4 and 5 found respectively 6, 7 and 40 decisions, **zero adjacent
   response pairs with overlapping objective metrics in every attempt**. Thus
   those observed action sequences cannot yield metric-delta lesson candidates
   through the current exact transition query, even if identity were available.
   These counts exclude the initial start observation; the v2 start response
   supplies no objective metrics either.
3. Stage 6 matches the complete typed action and configured top-level observation
   conditions. Different wait durations and resource/operation IDs create
   different action keys. Repeated action kinds do not imply repeated supported
   lessons. Whole payload fields with timestamps/cursors also make poor stable
   conditions. This is a deliberate limit of the existing exact query contract.
4. Cumulative uptime/cost changes around observation calls are associations,
   not causal credit for the observation action. Carrying the last metric
   forward across intervening actions would not by itself solve delayed credit
   assignment and must not silently relabel those deltas as causal effects.

## Consequences for subsequent work

- The evaluation harness must allow multiple declared immutable contexts and
  preserve unknown identity explicitly. Frozen evaluation and retries remain
  ineligible training evidence.
- Report candidate/active counts and context coverage alongside task scores.
  A3 with zero active lessons measures an inactive learning path, not evidence
  against all lesson methods or evidence for useful generalisation.
- Later world hypotheses describe scoped observational regularities with
  uncertainty. Playbook/tool suggestions must retain evidence and separate
  validation. They must not obtain activation by weakening provenance or
  pretending metric association is causal proof.
- Implement remaining modules as opt-in experiments. The Stage 6 held-out
  effectiveness gate and module-default promotion remain open until measured.

## Reproducible evidence

Ignored local evidence: `artifacts/v2-codex-pilot-2026-09-05/attempt-{3,4,5}/`
contains `attempt.json` and `seed-42/trace.jsonl`. For each `step` event, compare
the `(name, unit)` pairs in consecutive `data.result.objective_metrics` arrays.
The relevant implementation boundaries are `runner.py` transition assembly,
`simulator/v2_environment.py::_objective_metrics`, and
`memory/candidate_validation.py::_transition_matches` / `extract_candidates`.

The development-policy pilot 6 is a separate run. Its outcome does not replace
any earlier failed, incomplete or low-SLO attempt.
