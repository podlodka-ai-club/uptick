# Pantry HTTP protocol

## Objective

Finish three hunger cycles with health greater than zero and as few actions as possible.

## Transport

The world uses synchronous HTTP JSON with no authentication.

- `POST /start` with `{"seed": <positive integer>, "request_id": <unique string>}` creates a run.
- `GET /runs/{run_id}` returns the full current state without advancing time.
- `POST /runs/{run_id}/act` with `{"request_id": <unique string>, "action": "cook|eat|wait"}` performs one action.

All POST requests are idempotent when both request ID and body are identical, including after retries. Reusing an identity with a different body returns HTTP 409.

Action responses include the current state and an event with stable `identity`, `outcome`, and `description` fields. There are no asynchronous operations.

## State

State contains:

- `run_id`: identifier of the current run.
- `seed`: positive run seed.
- `health`: remaining health.
- `stomach_growling`: whether the character is hungry.
- `meal_ready`: whether a prepared meal is available.
- `cycles_completed`: completed hunger cycles.
- `actions`: number of actions taken.
- `done`: whether the run has ended.
- `success`: `null` before completion, then the world's Boolean verdict.

Initial state has health 4, `stomach_growling=true`, `meal_ready=false`, zero completed cycles, and zero actions.

## Action rules

- `cook` prepares a meal and costs one action.
- `eat` succeeds only when a meal is ready. It consumes the meal and completes one hunger cycle.
- After a successful non-final cycle, the next hunger cycle starts immediately.
- `wait` while hungry loses one health.
- Cooking again while a meal is already prepared loses one health.
- Eating without a prepared meal loses one health.
- Reaching zero health ends the run unsuccessfully.

The game advances only through actions, not through elapsed wall-clock time. The server's returned event, `done`, and `success` values are authoritative.

## Adapter contract

`observe` performs only `GET /runs/{run_id}` and exposes the full state plus the action catalog. It does not advance the game.

`act` accepts an action object with one field:

```json
{"type":"cook|eat|wait"}
```

It sends one corresponding remote action. The adapter journals request IDs and completed responses so identical retries remain stable across adapter process restarts. Because the remote POST endpoint guarantees idempotency, an identical request whose outcome was interrupted may safely be retried with the same request ID and body.
