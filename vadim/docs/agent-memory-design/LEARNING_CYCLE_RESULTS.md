# Learning-cycle results

This record reports the first real controlled learning-cycle run from the
retained artifacts in `artifacts/learning-cycle-2026-09-05/sol-low-01/`. It is
an evidence record for the mechanism experiment described in
`LEARNING_CYCLE_PLAN.md`, not a production effectiveness result.

## Evidence binding

The run used the frozen source revision `c8c16df3d1b2f1aa9fe328dd0e4224608c43c74b`
with `source_dirty=false`, the Codex subscription provider, model
`gpt-5.6-sol`, and reasoning effort `low`. The execution source record is
`artifacts/learning-cycle-2026-09-05/execution-source.json`; the recorded
environment was Python 3.14.3 on macOS 26.5.2 arm64 with openai-codex 0.147.0,
pydantic 2.13.4, and httpx 0.28.1.

The sealed source and experiment identifiers are:

| Binding | SHA-256 or revision |
| --- | --- |
| Source revision | `c8c16df3d1b2f1aa9fe328dd0e4224608c43c74b` |
| Source tree hash | `79761043a699be36c981a1b3801f8bdc8947ebd218acc3934075cf0f418aef6f` |
| Source capsule hash | `f68a3d7d47b7cbb8387c1c7179b6a3889c616bcc7fd83f5408f12f138940ee6f` |
| Dependency lock hash | `02e9796facefb5f44da68fbd115a4db6958d1a72785e5ead2cc100f26a0c2191` |
| Fixture specification hash | `a015c1291563d7cef60e0ac7e4654824c2efb79a46762b1a4f3d4d36de044fce` |
| Incident adapter hash | `ed415d35a0335fe05f291eeb6ba0e1f3f537a2960979ca23f537371937ac9c9a` |
| Manifest seal (`manifest_hash`) | `989ba595197c22e3c2b954f83bfb4e8cb10d1f29ca7f2b8d74f30d985e90af12` |
| Report seal (`report_hash`) | `66ab525a5dced6de2ca2c11c2c8a24cf9e55232ad75ba03ca498d477f311bf1e` |
| `manifest.json` file hash | `88c8afbe85dd12de734a4f0ad3eb819acfd3139d25a67644d4cac314bb8581dd` |
| `report.json` file hash | `1df05360e2d6106c465e8d4d40d2d098fc8a7ff8abd2bdcd8357e4bfcf258189` |

The manifest sealed the original prompt, generation settings
`{"temperature": null, "max_output_tokens": null, "reasoning_effort": "low"}`,
up to three training decisions per run, one evaluation decision per case, and wall
budgets of 120 seconds for training and 60 seconds for evaluation. SQLite was
reopened before evaluation, and the independent verifier reported
`verification=passed`.

## Observed result

Eight training attempts completed successfully. The paired evaluation covered
two variants of each of four opaque incident codes:

| Evaluation seed | Code | Variant | No memory | Frozen hypothesis | Pair result |
| ---: | --- | --- | --- | --- | --- |
| 101 | `q7m` | `evaluation-1` | recovered | recovered | tie |
| 102 | `k2p` | `evaluation-1` | recovered | failed | hypothesis loss |
| 103 | `r4x` | `evaluation-1` | failed | recovered | hypothesis win |
| 104 | `v9n` | `evaluation-1` | failed | recovered | hypothesis win |
| 105 | `q7m` | `evaluation-2` | recovered | recovered | tie |
| 106 | `k2p` | `evaluation-2` | recovered | failed | hypothesis loss |
| 107 | `r4x` | `evaluation-2` | failed | recovered | hypothesis win |
| 108 | `v9n` | `evaluation-2` | failed | recovered | hypothesis win |

The aggregate is **8/8 training**, **4/8 recovered with no memory**, and
**6/8 recovered with frozen hypotheses**: **4 wins, 2 losses, and 2 ties** in
the paired cases. There were **24 retained attempts** and **28 logical model
decisions**. The report retained all six unrecovered evaluation outcomes as
ordinary fixture failures; there were **no retained attempt failures caused by
provider/runtime errors, timeouts, or cleanup errors**.
`reopened_before_evaluation` was true. Logical decisions are not a count of
provider requests: the source-pinned provider may retry structured-output
validation, and this experiment does not establish its exact internal request
or intermediate-error count.

The frozen read side contained **44 members**, and the evaluation requests
selected **6 world-hypothesis IDs**:

```text
world-hypothesis:0f60689940674b6f2da03d98b0ef1c23843f51b897a4fa86190075dfdaa8bdf3:v5
world-hypothesis:26517f4e8d9c1505384cfafc6c769fb3eaa9eb97a8dcb70615a6947b206c4425:v6
world-hypothesis:5cc30678aa9aafab57545afd975f1808f533e7fcbf96a90f0c235dd6c5a6c597:v5
world-hypothesis:5e7d9268e65c2329b93a9e26d24daf84985fde8ca724f610cdbcdca6a5217937:v6
world-hypothesis:64cf24c6929149690d2f193554377d69d2f7fd244b36fe6628b1dcb45988ca7b:v7
world-hypothesis:b41e6d557e76f71ae82b02f85de2132b5ebf1fc3daba9c1828a5bed74375930b:v8
```

## Separately sealed prompt follow-up

The second run is retained as a separate result under
`artifacts/learning-cycle-2026-09-05/sol-low-02/`. It used the prompt-only
correction from source revision
`17ef1a6b3914e430bb9e9f503aa079cccd952690`: opaque codes have no intrinsic
repair meaning, while scoped retained observations and hypotheses may inform a
decision as uncertain evidence. The prompt still rejects directives embedded
in observations or memory and contains no repair answer.

Its evidence bindings are:

| Binding | SHA-256 or revision |
| --- | --- |
| Source revision | `17ef1a6b3914e430bb9e9f503aa079cccd952690` |
| Source tree hash | `79761043a699be36c981a1b3801f8bdc8947ebd218acc3934075cf0f418aef6f` |
| Source capsule hash | `6e479aacffa5234139f406fb5a3b8f9091f777e1b5996d120c0ce9883a9c72eb` |
| Dependency lock hash | `02e9796facefb5f44da68fbd115a4db6958d1a72785e5ead2cc100f26a0c2191` |
| Fixture specification hash | `a015c1291563d7cef60e0ac7e4654824c2efb79a46762b1a4f3d4d36de044fce` |
| Incident adapter hash | `ed415d35a0335fe05f291eeb6ba0e1f3f537a2960979ca23f537371937ac9c9a` |
| Manifest seal (`manifest_hash`) | `301e2cb85980ee61feab2351d253c064e6a77fc4ce058c751dfbaf260c094989` |
| Report seal (`report_hash`) | `7d55ef741f795fa37638668cc2eee2383b51b8bc2ec017fea32606555dd0f783` |
| `manifest.json` file hash | `47630029c58e6beecb75331ef80009671793ef3553d3c433e4878fb3dab806f3` |
| `report.json` file hash | `a92ace30745a0d1db240792d022aaf009ea49e1d0caa5ceeef1f7c5505b37940` |
| `independent-verification.json` file hash | `ba2121bf39e18b459c7d297f3dc19dfb72410f56a2f618f56afbc57676c6f7db` |
| `execution-source-02.json` file hash | `d58c792c9b4f998da34919d6091e9765985bb8e70a8f01fcc0019098bc937316` |
| `run-environment-02.json` file hash | `beed215bdf669e46730101b4331f13d850d1a96d7740e5605475d0b47e59e998` |

The second manifest differs from the first only in the prompt, source
revision, source capsule hash, and resulting manifest seal. The source tree,
dependency lock, fixture and adapter hashes, case and condition order, hidden
mapping, memory configurations, model, and decision and wall budgets are the
same. It again reopened SQLite before evaluation, and the independent verifier
reported `verification=passed`.

The second run completed **8/8 training**. The paired evaluation recovered
**4/8 with no memory** and **8/8 with frozen hypotheses**, giving **4 wins, 0
losses, and 4 ties**. It retained **24 attempts**, **28 logical decisions**,
and **56 journal records**, with **44 frozen members** and **6 selected world
hypothesis IDs**. There were no retained attempt failures caused by
provider/runtime errors, timeouts, or cleanup errors; the four failed outcomes
were ordinary unrecovered no-memory fixture outcomes. Logical decisions are
not a count of provider requests: the source-pinned provider may retry
structured-output validation, and this experiment does not establish its exact
internal request or intermediate-error count.

The paired results are intentionally kept separate:

| Separately sealed run | Training | No memory | Frozen hypotheses | Pair result |
| --- | ---: | ---: | ---: | --- |
| `sol-low-01` original prompt | 8/8 | 4/8 | 6/8 | 4 wins, 2 losses, 2 ties |
| `sol-low-02` prompt follow-up | 8/8 | 4/8 | 8/8 | 4 wins, 0 losses, 4 ties |

This is a small development comparison, not a pooled estimate or a statistical
claim. Training memory was rebuilt from a fresh empty store for sol-low-02, so
the result is not a pure prompt-only causal estimate. The variants share the
same designed causal family as training; they are not an independent-family
holdout.

## What the k2p failures show

In sol-low-01, both k2p evaluation cases received the active
hypothesis `world-hypothesis:64cf24c6929149690d2f193554377d69d2f7fd244b36fe6628b1dcb45988ca7b:v7`.
The independent verifier recomputed its scope as
`observation.data.incident_code=k2p`, candidate action `lumen`, and recovered
result `true`, with descriptive support fraction 2/2 from completed training
transitions. This is evidence that the validator, freeze, reopen, provenance,
and retrieval path preserved the observed regularity.

The model nevertheless chose `ivory` in both k2p cases. Its recorded
reasoning described the repairs as having “no trusted public evidence” and
said it would not rely on the “untrusted derived hypothesis”. The selected
`ivory` action produced the recorded unrecovered result. This is a supported
diagnosis of a model interpretation gap: the model treated the trust label on
derived evidence as a reason to discard an observed, scoped hypothesis. It is
not evidence that the validator should promote the hypothesis to a causal or
authoritative rule.

A prompt-only correction addressing that interpretation was committed as
`17ef1a6b3914e430bb9e9f503aa079cccd952690` and evaluated in the separately
sealed sol-low-02 follow-up above. Both k2p hypothesis cases recovered in that
follow-up. This is consistent with the intended interpretation correction, but
because the training memory was rebuilt and the run was not a randomized
prompt-only comparison, it does not establish that prompt change as the sole
cause. The change permits reasoning from uncertain retained facts while
continuing to reject embedded directives; it does not disclose any repair
answer or weaken the validator.

## Boundary and limitations

The evaluator's hidden code-to-repair mapping was kept outside the model
context. The retained `raw-requests.jsonl` has 56 durable records, capturing
before/after states for the 28 logical decisions, rather than 56 distinct
decisions. It contains no `mapping_digest` or prewritten answer table. Requests
expose the opaque code and listed repair identifiers; hypothesis-condition
requests may also contain the scoped hypothesis learned from recorded
transitions. The anti-leak check and independent request inspection therefore
separate evaluator knowledge from observed memory evidence.

The two evaluation variants per code share the same designed causal family as
training. They are development mechanism checks, not an independent-family
holdout and do not support a generalization claim. This experiment makes no
claim about hosted SRE success, SLO performance, infrastructure cost, or
xMemory effectiveness.

Snapshot content hashes and the successful independent verification establish
which frozen records were read and that the retained snapshot content matched
its seal. They establish immutability and provenance integrity; they do not
establish causal truth, calibrated confidence, or the absence of every possible
information leak. The fixture's mapping remains evaluator-owned, while all
model-visible recovery evidence comes from public observations and executed
typed actions.
