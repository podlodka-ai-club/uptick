# Explicit historical observation reads

Status: experimental runner capability; default and live launchers remain off.
This capability proves bounded access and accounting. It does not establish
smaller prompts or improved decisions.

The environment owns the typed read action in its decision schema. Composition
provides `ObservationHandoff` over the same canonical write store and namespace
used by episodic memory, plus a mapper from the validated environment action to
`ObservationReadRequest` (or `None` for an ordinary action). Both runner arguments
and an explicit `max_actions` budget are required. The runtime depends on a port,
not the concrete store. Evaluation callers must bind their current-run write
store, not the frozen historical corpus used for retrieval.

After recording an ordinary action, the controller verifies the canonical
transition and issues its bookmark for subsequent decisions. Default index
bounds are eight bookmarks and 8000 serialized JSON bytes. References removed
from this index become unavailable through this capability; canonical evidence
is retained. Missing canonical records produce no bookmark. Unknown world
identity remains unknown. Every read revalidates the source and immutable binding.

Reads accept only issued prior references and return UTF-8-aligned chunks of
4–8192 bytes. The response retains source identity, digest, offsets and an
explicit historical marker. Invalid or unavailable reads return a failed
receipt; this is not evidence that a fact is false. Transient store errors are
not silently converted to absence.

A read consumes one normal action and stops the current batch. Its receipt is
exposed as `memory_read_result`; bookmarks use `observation_bookmarks`. The
latest environment result and metrics remain unchanged. Reads do not create
world transitions, recursively issue bookmarks, or enter ordinary environment
history/legacy experience. The correlated `decision.memory_read_completed`
audit event forbids a world transition ID; observer records use a null ID.
Existing world completion events still require their transition ID.

Disabled runners omit the new context fields. A handoff-enabled runner currently
adds bounded context; no replacement/compression of ordinary history is implied.
Individual bookmarks survive serialization and process restart. An opt-in
`ObservationHandoff` durable index can now persist the latest bounded issued
bookmark list in a separate structured-store namespace and restore it only for
an exact caller-supplied run/namespace/environment/scenario identity and
current-iteration cutoff. The index revalidates every listed bookmark against
the canonical transition and fails closed on a missing source, changed binding,
wrong identity, corrupt index, or future cutoff; it never scans transitions to
invent issued references. A valid empty latest index remains empty, so evicted
references are not resurrected.

This restores the handoff controller's issued index only. The generic runner
still starts a fresh environment session at iteration one and has no persisted
session, public-state, or action-budget resume contract. Composition may inject
an explicit async new-run initializer with a caller-owned identity resolver;
the runner passes the opaque started session and initial public result to that
initializer before the first decision. The durable initializer rejects an
existing exact-run index rather than treating a new run as a resume. A full
process restart therefore remains unsupported until an environment and runner
resume boundary supplies those inputs.

Evidence is in `artifacts/memory-tower-preparation/STAGE05.md`, including exact
real-response recovery after process restart and a scripted composed-runner
probe. Two different environment-owned action schemas exercise the same core.
Scripted actions and replay prove transport, accounting and isolation only.
Before enabling a utility profile, compare against an unchanged episodic
baseline at equal budgets and measure total tokens, read actions, latency and
task results. Retain negative results; do not assume savings from smaller refs.
