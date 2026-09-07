# Optional episodic query-match excerpt

Status: implemented and mechanically checked, 2026-09-05. No model utility or
SRE success claim. The running dev-06 capsule predates this change.

## Observed defect

The lexical retriever ranks full stored transitions but exposes prefix excerpts
of their fields. A selected result can therefore hide the very evidence that
matched the query. The retained local probe used a public synthetic result with
900 padding characters before `ok:false` and a unique summary token. Retrieval
selected it with lexical overlap 1; neither the matching token nor the failure
flag appeared in its 512-character result prefix. Full storage, provenance and
trust classification were intact. The excerpt is a string: incomplete JSON is
not itself a contract violation.

Evidence: `artifacts/episodic-excerpt-2026-09-05/retrieval-probe.json`. This is a
deterministic store/retrieve probe with no model, simulator or remote calls.

## Minimal opt-in change

Set `MemoryConfiguration.episodic.version` to `1.1` to supplement the existing
view with at most one `query_match` window. Version `1.0` remains the default;
other caller-selected versions retain the original prefix-only behavior. The
ordinary runtime composition already forwards this setting.

The window contains an actual request query token absent from that field's
ordinary prefix. Fields are considered in order: result, observation, action.
It contains exact canonical JSON text, source field, character offsets, and
`complete:false`. Its serialized size is at most 600 bytes, with at most 80
context characters on each side. Tokens longer than 160 characters are skipped
while searching for another eligible match. Offsets address canonical JSON
characters, including JSON escape sequences.

Candidate eligibility, ranking, score, IDs, stored records, provenance, trust,
finalization and learning gates remain unchanged. A longer context item can
consume more of the existing module/global admission budget and reduce the
number of admitted items. This change does not guarantee all relevant evidence:
it supplies only one lexical window from three fields, and cannot fix an
uninformative retrieval query or establish that historical evidence is current.

## Verification and next experiment

Three added tests cover default/custom-version compatibility, exact escaped
Unicode/quote/newline slices, the serialized cap, unchanged ranking/storage/
provenance/trust, run eligibility, no-match fallback, and the actual configured
runtime. Review found an oversized first matching token suppressing a later
usable match; the fix and regression case are included. Final full suite:
680 passed, 2 opt-in live skips; Ruff passed. Two bounded review passes and
validated markers are retained under the same artifact directory.

Before any default promotion, compare frozen prefix-only and supplemented
retrieval on identical public experience/query inputs and model budgets. Measure
answer/action correctness, uncertainty, stale-evidence mistakes and total model
cost. A mechanical availability fix alone cannot establish persistent-memory
utility or close the active goal.

## Later offline query shadow

The corrected nine-case shadow in `artifacts/episodic-excerpt-2026-09-05/shadow-v2/`
compared the actual latest-result query with a512-byte previous-plan supplement.
Each case stored only earlier public same-run transitions. Both arms selected
the immediately preceding transition in all9cases; the4000estimated-unit budget
admitted one item. This exposes self-match dominance in this reconstruction;
it does not demonstrate cross-run retrieval behavior or meaningful relevance.
No query enrichment was wired. Root independently reproduced all9results and
checked129provider latest-result bindings against their preceding trace steps.

The initial shadow used startup instead of step1's result for queryiteration2.
That flawed attempt is preserved with an erratum; it is superseded by v2. V2
still omits original pre_state/environment metadata during episode reconstruction,
so it is not a complete replay of original stored transition ranking. Neither
shadow made model or simulator calls or established behavioral utility.
