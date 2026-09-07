# Opt-in nested lesson conditions

Lesson settings keep the legacy `condition_keys` contract by default: each key
is read literally from the top level of `transition.observation`. In
particular, a key such as `"data.duplicate_lines"` remains a literal key and
is never interpreted as a path.

Settings may opt in to nested observations with `condition_paths`, an explicit
one-to-one mapping from every condition key to a dotted path rooted at
`observation`, for example:

```json
{
  "condition_keys": ["format", "duplicate_lines"],
  "condition_paths": {
    "format": "observation.format",
    "duplicate_lines": "observation.data.duplicate_lines"
  }
}
```

The alias keys remain the stable candidate condition names. The complete path
mapping is included in the candidate semantic identity and statement, so two
candidates with the same observed values but different path meanings cannot
silently share an identity. Extraction and independent validation use the same
projection. Exact request scoping is applied only to path-bound lessons and
uses the runner's explicit `latest_result`; missing or mismatched paths do not
match. Legacy settings and persisted legacy batches remain query-only and are
not rewritten. Consolidation preserves this boundary: an applied lesson with
path bindings is filtered against the same explicit `latest_result` before it
can contribute context; legacy consolidated lessons remain query-only.
