"""Independent deterministic toy environment, not an agent or an API adapter."""
from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL = """You control a character in a small kitchen. Finish three hunger cycles
with health > 0 and as few actions as possible. Protocol is HTTP JSON, no auth.
POST /start {seed: positive integer, request_id: unique string} creates a run.
GET /runs/{run_id} observes the full current state without advancing time.
POST /runs/{run_id}/act {request_id: unique string, action: cook|eat|wait}.
All POSTs are idempotent with the same request_id AND body, including after retries.
Different bodies under the same identity return 409. Responses include state and,
for actions, event with a stable identity, outcome and description.
State fields: run_id, seed, health, stomach_growling, meal_ready, cycles_completed,
actions, done, success (null until done). Initial state: health=4, growling=true,
meal_ready=false. Cook prepares a meal and costs one action. Eat succeeds only
when a meal is ready, consumes it and completes one cycle. The next hunger cycle
starts immediately until three cycles are complete. Waiting while hungry loses
one health. Re-cooking an already prepared meal also loses one health. Invalid
eating loses one health. Health reaching zero completes the run unsuccessfully.
Game state only advances through actions, not wall-clock time. No async operations.
Do not fabricate outcomes; use the returned done/success and event values.
"""


class Server(ThreadingHTTPServer):
    def __init__(self, address, state_path):
        super().__init__(address, Handler)
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = json.loads(state_path.read_text()) if state_path.exists() else {"runs": {}, "requests": {}}
        self.lock = threading.Lock()

    def save(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.db))
        temporary.replace(self.state_path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        with self.server.lock:
            run = self.path.removeprefix("/runs/")
            if self.path.startswith("/runs/") and run in self.server.db["runs"]:
                self.reply({"state": self.server.db["runs"][run]})
            else:
                self.reply({"error": "not_found"}, 404)

    def do_POST(self):
        with self.server.lock:
            try:
                data = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)))
                request_id = data["request_id"]
                signature = json.dumps([self.path, data], sort_keys=True)
                old = self.server.db["requests"].get(request_id)
                if old:
                    return self.reply(old["response"] if old["signature"] == signature else {"error": "idempotency_conflict"},
                                      200 if old["signature"] == signature else 409)
                if self.path == "/start":
                    if not isinstance(data.get("seed"), int) or data["seed"] <= 0:
                        return self.reply({"error": "seed_must_be_positive"}, 400)
                    ident = uuid.uuid4().hex
                    state = {"run_id": ident, "seed": data["seed"], "health": 4, "stomach_growling": True,
                        "meal_ready": False, "cycles_completed": 0, "actions": 0, "done": False, "success": None}
                    self.server.db["runs"][ident] = state
                    result = {"run_id": ident, "protocol": PROTOCOL, "state": dict(state)}
                else:
                    parts = self.path.strip("/").split("/")
                    if len(parts) != 3 or parts[0] != "runs" or parts[2] != "act" or parts[1] not in self.server.db["runs"]:
                        return self.reply({"error": "not_found"}, 404)
                    state = self.server.db["runs"][parts[1]]
                    if state["done"]:
                        return self.reply({"error": "already_done", "state": state}, 409)
                    action = data.get("action")
                    if action not in {"cook", "eat", "wait"}:
                        return self.reply({"error": "unknown_action"}, 400)
                    outcome, description = "success", action
                    if action == "cook" and not state["meal_ready"]:
                        state["meal_ready"] = True
                    elif action == "eat" and state["meal_ready"]:
                        state["meal_ready"] = False
                        state["cycles_completed"] += 1
                    else:
                        state["health"] -= 1
                        outcome, description = "failure", "The action lost one health"
                    state["actions"] += 1
                    state["done"] = state["cycles_completed"] >= 3 or state["health"] <= 0
                    state["success"] = state["health"] > 0 if state["done"] else None
                    state["stomach_growling"] = not state["done"]
                    result = {"state": dict(state), "event": {"identity": request_id,
                        "outcome": outcome, "description": description}}
                self.server.db["requests"][request_id] = {"signature": signature, "response": result}
                self.server.save()
                self.reply(result)
            except (ValueError, KeyError, TypeError):
                self.reply({"error": "invalid_request"}, 400)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8097)
    p.add_argument("--state", type=Path, default=Path(__file__).resolve().parents[1]/".local/environment.json")
    args = p.parse_args()
    print(f"Pantry environment: http://127.0.0.1:{args.port}", flush=True)
    Server(("127.0.0.1", args.port), args.state).serve_forever()
