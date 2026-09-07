# Stage 10 retrieval results

This document records the sealed optional retrieval microbenchmark from
`artifacts/retrieval-evaluation-2026-09-05/`. It evaluates relevance on one
fixed development fixture: 16 documents, 8 queries, and three themes
(infrastructure, logistics, and workshop). The seven conditions are paired on
the same candidate pool and queries. They are not a holdout and do not support
a statistical, causal-family, SRE, or xMemory effectiveness claim. The
conditions are variants within one designed development family, rather than an
independent-family evaluation.

## Implementation and evidence bindings

The implementation is pinned to source revision
`39cfb7478b33fb9ed39ed39f479bdba617b73141`, source capsule hash
`7b6639a7e9297b7ef90d68c059ef8384b2ebaf6972a49b469763b75004b69cd0`, and
dependency-lock hash
`5a829da956c2fbe5cf0e1d5c9fa32dbeaff584f81f8864523118dd4ab3851e2f`.
Stage 10 adds neutral embedding and reformulation ports, cosine semantic
ranking, bounded provenance-neighborhood scoring, confidence and recency
signals, deterministic ordering, and optional adapters. The FastEmbed adapter
was pinned to version 0.8.0 and loaded the local
`sentence-transformers/all-MiniLM-L6-v2` model with 384-dimensional vectors.
The model manifest records archive SHA-256
`2735afe656e156af64ed603dbb1c96f3cae7f937286a8feb27fff7fa979f6a77` and the full local file inventory; the
manifest itself has SHA-256
`a663c89794bc2d45e42b21d3e3ea992d570ec5aea137ad445288d8c1c0f5d689`.

The sealed files and their exact hashes are:

| Artifact | SHA-256 |
| --- | --- |
| `manifest-01.json` | `ff81240198edee8ecb79f6686d9929c595696c6bdca41f066b945df2325a8aaa` |
| `comparison-01/report.json` | `41e597154518a7a578620504719d93c202d27a45c981e7378fdba16e3d183631` |
| `comparison-01/independent-verification.json` | `5cc1f174a1263086aa9960af4a644cdb3953ae8d406b057f7eaf3b8ed3485d92` |
| `embedding-model-manifest.json` | `a663c89794bc2d45e42b21d3e3ea992d570ec5aea137ad445288d8c1c0f5d689` |
| `fixture.json` | `e4bda055e2fbb9b2be0fec8e24bb140e4ea0d67a1ed9eb3a773c1ea63d5fcb8b` |
| `labels.json` | `5b9d08cce62ae3c5c98567020798d2e800800e2fdbc84d7671f30166a9a342bf` |
| runner (`run_evaluation.py`) | `c4f0d6574f58bc290a71b165ff89ca6299f9bdfef80672705869613c69f58102` |

All paths above are under the ignored artifact directory
`artifacts/retrieval-evaluation-2026-09-05/`; the source capsule is at
`source-39cfb74/` and the retained output is at `comparison-01/`.

## Paired results

The manifest declared 56 cells (7 conditions × 8 queries), `max_items=3`, a
2,400 estimated-token cap, and a 90-second per-query timeout. Every cell was
retained and completed; the report has zero cleanup errors and no experiment
error. Independent verification passed with 56 cells, 8 logical Codex query
requests and results, zero failures, matching fixture queries, and
`labels_seen_by_model=false`.

| Condition | Hit@1 | MRR@3 | Recall@3 | Total elapsed seconds |
| --- | ---: | ---: | ---: | ---: |
| lexical | 0.250 | 0.354167 | 0.4375 | 0.008130 |
| lexical_structured | 0.375 | 0.520833 | 0.6250 | 0.007833 |
| semantic | 0.750 | 0.854167 | 0.9375 | 0.499157 |
| hybrid | 0.875 | 0.937500 | 1.0000 | 0.496939 |
| hybrid_graph | 0.875 | 0.937500 | 1.0000 | 0.453840 |
| hybrid_all | 0.875 | 0.937500 | 0.9375 | 0.463726 |
| reasoned_hybrid_all | 0.875 | 0.937500 | 1.0000 | 54.631368 |

The fixed fixture therefore gives Hit@1 for 2/8 lexical queries, 6/8
semantic queries, and 7/8 hybrid queries. The graph condition shows no
relevance improvement over hybrid. Reasoned reformulation takes about 54.6
seconds over its eight logical requests versus about 0.5 seconds for hybrid
and adds no Hit@1 gain. These are descriptive results on eight queries; they
do not justify promoting graph or reasoned retrieval to a default. No new
final SRE evaluation matrix is claimed by this benchmark.

The evaluator labels were loaded only after retrieval/model calls. The
independent verifier confirmed that request contexts stayed within the fixture
domain and that labels were not seen by the model. This guards the measured
request boundary; it is not a formal security sandbox. The reported telemetry
contains 112,587 input tokens and 291 output tokens. SDK-internal retry counts
and monetary cost are unknown.

## Composition boundary

Retrieval is optional and injected at the composition edge. A minimal semantic
composition has these actual calls:

```python
embedder = FastEmbedPort(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    specific_model_path=Path("/local/fast-all-MiniLM-L6-v2"),
    local_files_only=True,
)
strategy = SemanticRetrievalStrategy(
    embedder,
    settings=SemanticRetrievalSettings(max_items=3, max_estimated_tokens=2400),
)
runtime = compose_experimental_runtime(
    preset,
    store,
    namespace="experiment",
    retrieval_strategy=strategy,
)
```

`compose_experimental_runtime` accepts the optional
`retrieval_strategy`; semantic configuration without an injected strategy
fails closed rather than silently substituting lexical ranking. The ordinary
`evaluate-v2` default factory does not inject these extensions, so existing
default behavior and hashes remain unchanged. The reasoned adapter is also
optional and uses the neutral `ReasonedQueryPort` with a structured
`LlmClient`; it receives only the public request and context.

The strategy ranks an already admitted pool under the caller's item and token
budgets. Provenance graph scoring stays inside that pool and uses verified
artefact ID/content-hash edges. Snapshot or envelope hashes prove evidence
immutability, not causal truth. The benchmark uses hand-authored relevance
labels and shared development variants; it does not establish an independent
causal-family result, hosted SRE utility, or broader generalization.
