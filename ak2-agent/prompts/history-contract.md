# Historical interface, version 1

Optional entry point: scripts/history.py, Python stdlib, same isolated working
directory and bootstrap.json as adapter.py. The core invokes this separate entry
point; it never dispatches retrospective queries through adapter.py act.

stdin includes operation=discover|read, source_id, parameters (object), seed, task,
options and budget_seconds. Sources belong ONLY to the current run from bootstrap.
The agent has stopped, but the world may still be unfinished. Never complete a
pending operation or advance the world to make its history accessible.
Discover inspects documented historical read facilities and may perform bounded
read-only capability checks. It outputs exactly:
{"status":"supported|unsupported|unavailable","reason":"...","sources":[
 {"id":"...","description":"...","parameters":"documented parameters and pagination"}]}

supported means documented past observations can be read at this stopping point; an
actual read must still verify availability. unsupported requires affirmative
documentation/inspection that no historical facility exists. Missing documentation,
authorization failures, timeouts and unimplemented integration mean unavailable,
NOT unsupported. Only supported has a nonempty sources list (at most 16).

Read validates source_id and parameters against the discovered documented catalog,
then reads one bounded page. Output exactly:
{"source_id":"...","data":{},"evidence":[],"complete":true,
 "next_parameters":null,"error":null}

complete describes this decoded page, not all history. next_parameters contains
the explicit continuation parameters for that same source, or null. Never silently
truncate a page and mark it complete. Maximum output is 500000 UTF-8 bytes per call;
use smaller server-side pages/windows when necessary. Return a diagnostic error,
complete=false and evidence=[] for unavailable/incomplete data. Do not substitute
cached current state for historical observations. Expose source, original timestamps,
coverage and pagination. Discovered sources and queries must never select another run.

Evidence uses the normal adapter schema: identity, outcome=success|failure,
kind=action|operation|effect|completion, detail. Return real historical outcomes,
NOT successful delivery of a GET. Reuse EXACTLY the adapter's identity for any event
already exposed during execution (including probes and operations). Do not create
a second identity for a summary of events already cited individually. An ordinary
metric snapshot without a verified outcome remains data, not success evidence.
New events need stable source-native IDs independent of fetch time, query/window,
pagination or restarts. Preserve original records and timestamps in detail. Never
invent facts or infer the success of an unexecuted alternative.

This is strictly observational code: no bootstrap/start, act, probe, time advance,
control commands, request replay, mutation, or trial of alternative strategies.
HTTP GET alone does not prove read-only semantics: use only documented observational
operations valid for the stopped run, whether finished or unfinished. Do not call adapter.main/observe/act, since
those may execute work or repair pending requests. Existing transport/serialization
helpers may be imported only after inspecting their behavior. Validate all inputs.
No background processes, package installs, new secrets or URLs guessed from names.
Credentials stay in the isolated process; redact their values, including free-text
messages, from stdout. Never include credentials or current run IDs in source code.

Honor budget_seconds across retries, connection waits, response reads and parsing.
On busy/conflict responses wait with bounded backoff; never overlap HTTP calls for
the same run. Limit response bytes. Read incrementally with read1 and return a fully
decoded JSON document immediately, without waiting for connection EOF. Temporary
errors stay explicit, and every retry consumes the same budget.
