---
name: operate
description: Observe and operate the current Pantry kitchen simulation through the world adapter.
---

# Operate Pantry

Use the adapter observation to inspect the authoritative current state. The world advances only through actions, so observation is safe and does not consume an action.

## Action format

Pass exactly one of these objects to an `act` call:

```json
{"type":"cook"}
```

```json
{"type":"eat"}
```

```json
{"type":"wait"}
```

Every `act` call performs exactly the action supplied by the model. Reuse the same outer `request_id` only when retrying the identical action. Use a new request ID for a new action.

## Reading results

Treat `data.state` as the current authoritative state. `done=true` is terminal, and `success` then contains the world's verdict. `pending` is always false because Pantry actions are synchronous.

Evidence from an action is copied from the server event and has a stable identity. A successful HTTP request only establishes that the action was processed; inspect the returned event and state to determine its actual effect. If `error` is present, assess it before deciding whether to retry or choose another action.

Do not claim completion unless the adapter reports `done=true`. Stop issuing actions after a terminal state.

For the complete protocol and state-field definitions, read `../../references/protocol.md` relative to this skill.
