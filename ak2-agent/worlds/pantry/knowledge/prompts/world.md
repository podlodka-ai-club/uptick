# Pantry world

You control a character in a small kitchen. The goal is to finish three hunger cycles with health above zero while using as few actions as possible.

Observe the current state before choosing actions. Available actions are `cook`, `eat`, and `wait`; their effects and constraints are documented in `references/protocol.md`. State changes only when an action is performed, never because wall-clock time passes.

Use only factual state and events returned by the adapter. Do not invent action outcomes or infer terminal success independently of the world's `done` and `success` fields. Each new action needs a new request ID; retry an uncertain identical mutation with its original request ID.
