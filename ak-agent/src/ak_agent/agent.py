from __future__ import annotations

import fcntl
import hashlib
import json
import shlex
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .memory import Memory, dumps
from .simulator import Simulator, redact

DEFAULT_ORIGIN = "http://81.176.229.58:8080"


def emit(kind, **fields):
    print(dumps(redact({"event": kind, **fields})), flush=True)


@contextmanager
def run_lock(memory, run):
    path = memory.path.parent / (run + ".lock")
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ValueError("Этот run уже обслуживается другим процессом") from e
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def create_run(memory, seed, origin, model, experiment="", recall=True, learn=True):
    if seed <= 0:
        raise ValueError("seed должен быть > 0")
    local_id = "ak-" + uuid.uuid4().hex[:16]
    state = {"local_id": local_id, "seed": seed, "origin": origin, "model": model,
             "status": "starting", "step": 0, "plan": "", "tags": [], "pending": [],
             "queue": [], "queue_index": 0, "experiment": experiment,
             "recall": recall, "learn": learn, "llm_seconds": 0, "decisions": 0,
             "started_at": time.time()}
    memory.save_run(local_id, state)
    if learn:
        with memory.db:
            memory.db.execute("INSERT OR IGNORE INTO training_seeds VALUES (?)", (seed,))
    return state


@contextmanager
def checkpoint_errors(memory, state):
    try:
        yield
    except BaseException as error:
        if state.get("runner_status") != "blocked":
            state["runner_status"] = "paused" if isinstance(error, KeyboardInterrupt) else "error"
        state["last_error"] = str(error) or type(error).__name__
        memory.save_run(state["local_id"], state)
        raise


def bootstrap(memory, state):
    sim = Simulator(state["origin"], memory, state)
    if not state.get("run_id"):
        response = sim.request("POST", "/v2/start", {
            "seed": state["seed"], "agent_id": "ak-agent", "agent_version": "2.0"},
            request_id=state["local_id"] + "-start")
        if response["http_status"] >= 400:
            raise RuntimeError("Start failed: " + dumps(redact(response)))
        # Save instructions and access immediately, BEFORE any further network call.
        state.update(run_id=response["run_id"], auth=response["control_panel_auth"], status="running")
        document = response["commands_markdown"]
        key = "protocol-" + hashlib.sha256(document.encode()).hexdigest()[:16]
        memory.put(key, document)
        state["protocol_key"] = key
        memory.save_run(state["local_id"], state)
        emit("started", local_id=state["local_id"], run_id=state["run_id"], seed=state["seed"], model=state["model"])
    if not state.get("catalog"):
        catalog = sim.get("control/commands")
        if catalog["http_status"] >= 400:
            raise RuntimeError("Cannot read command catalog")
        state["catalog"] = catalog
        memory.save_run(state["local_id"], state)
    return sim


def summarize_logs(response):
    if response.get("http_status", 200) >= 400:
        return response
    logs = response.get("logs", [])
    grouped = Counter((x.get("error"), x.get("user_agent"), x.get("region_code"), x.get("source_ip"), x.get("server_id")) for x in logs)
    by_agent = {}
    for x in logs:
        group = by_agent.setdefault(x.get("user_agent", ""), {"count":0,"load_units":0,"errors":0})
        group["count"] += 1
        group["load_units"] += x.get("load_units", 0)
        group["errors"] += bool(x.get("error"))
    return {"sample_size": len(logs), "next_cursor": response.get("next_cursor"),
            "sampling_note": "Chronological bounded sample, NOT whole-run traffic proportions.",
            "by_user_agent": by_agent,
            "by_region": dict(Counter(x.get("region_code") for x in logs)),
            "groups": [{"error": k[0], "user_agent": k[1], "region": k[2], "ip": k[3], "server_id": k[4], "count": n} for k,n in grouped.most_common(16)],
            "last_records": logs[-4:]}


def observe(sim, state):
    overview = sim.get("overview")
    if overview["http_status"] >= 400:
        raise RuntimeError("Observation failed: " + dumps(redact(overview)))
    if overview["status"] in ("completed", "failed"):
        return {"overview": overview}
    now = overview["clock"]["simulation_time"]
    earliest = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(minutes=2)
    previous = state.get("last_observation", {}).get("overview", {}).get("clock", {}).get("simulation_time")
    if previous:
        earliest = max(earliest, datetime.fromisoformat(previous.replace("Z", "+00:00")))
    since = earliest.isoformat()
    # The server rejects concurrent requests WITHIN a run. Parallelize worlds instead.
    resources = sim.get("resources")
    metrics = sim.get("metrics")
    inbox = sim.get("inbox", limit=1000)
    logs = sim.get("logs", **{"from": since, "limit": 100})
    errors = sim.get("logs", **{"from": since, "limit": 100, "has_error": "true"})
    operations = [sim.get("operations/" + oid) for oid in state["pending"]]
    state["pending"] = [oid for oid,o in zip(state["pending"], operations)
                        if o.get("status") not in ("succeeded", "failed")]
    return redact({"overview": overview, "resources": resources, "metrics": metrics.get("current", metrics),
                   "inbox": inbox, "recent_logs": summarize_logs(logs),
                   "recent_errors": summarize_logs(errors), "operations": operations})


def passport(memory, state):
    # Command schemas are discovered, not a handwritten strategy.
    catalog = [{k: x[k] for k in ("command", "params_schema", "target_auth_required", "execution")}
               for x in state["catalog"]["commands"]]
    return memory.get(state["protocol_key"]) + "\n\nКаталог схем params:\n" + dumps(catalog) + """
Транспорт наблюдений: GET overview, resources, metrics (текущий snapshot), inbox
(limit 1..1000, cursor), logs (from/to RFC3339, limit 1..1000, cursor, has_error,
error, status, user_agent, source_cidr, region_code, server_id НЕ поддерживается),
operations/{operation_id}. Логи идут от старых к новым; используй from для свежих.
POST probes: {page:'product_list'} или {page:'product_page',product_id: фактический ID}.
"""


def apply_decision(memory, state, decision, recalled, final=False):
    allowed = [x["id"] for x in memory.recent(state["local_id"], 14)]
    learned = []
    if state["learn"]:
        for lesson in decision["lessons"][:4]:
            key = memory.learn(lesson, state["local_id"], allowed)
            if key:
                learned.append(key)
    used = set(decision["used_memory_ids"]) & {x["id"] for x in recalled}
    if state["recall"]:
        memory.used(used)
    state.update(plan=decision["plan"], tags=decision["tags"], mission=decision["mission"])
    memory.event(state["local_id"], "reflection" if final else "decision", {
        **decision, "used_memory_ids": sorted(used), "accepted_lessons": learned})
    emit("reflection" if final else "decision", run_id=state["run_id"], step=state["step"],
         assessment=decision["assessment"], actions=[a["name"] for a in decision["actions"]],
         used_memory=sorted(used), learned=learned)
    return learned


def execute_queue(memory, sim, state, stop=None):
    while state["queue_index"] < len(state["queue"]):
        if stop is not None and stop.is_set():
            return  # Keep the unexecuted tail for resume.
        i = state["queue_index"]
        action = state["queue"][i]
        rid = f"{state['local_id']}-{state['step']}-{i}"
        try:
            response = sim.execute(action, rid)
        except ValueError as e:
            response = {"http_status": 400, "error": "LOCAL_VALIDATION", "message": str(e)}
        memory.event(state["local_id"], "outcome", {"request_id": rid, "action": action, "response": redact(response)})
        key = action["kind"] + ":" + action["name"] if "kind" in action else action["name"]
        counters = state.setdefault("internal_errors", {})
        if key not in counters:
            counters[key] = 0
            for past in reversed(memory.recent(state["local_id"], 80)):
                if past["kind"] != "outcome" or past["data"].get("request_id") == rid:
                    continue
                pa = past["data"].get("action", {})
                if pa.get("kind", "") + ":" + pa.get("name", "") != key:
                    continue
                pr = past["data"].get("response", {})
                if pr.get("error") != "INTERNAL_ERROR":
                    break
                counters[key] += 1
        if response.get("error") == "INTERNAL_ERROR" and response["http_status"] >= 500:
            counters[key] = counters.get(key, 0) + 1
        elif response["http_status"] < 400:
            counters[key] = 0
        state["queue_index"] += 1
        oid = response.get("operation_id")
        unfinished = oid and (response["http_status"] == 202 or response.get("status") in ("queued", "running"))
        if unfinished and oid not in state["pending"]:
            state["pending"].append(oid)
        # Do not execute a dependent step on async acceptance or error.
        if response["http_status"] >= 400 or response.get("error") or (unfinished and action.get("wait_for_completion", True)):
            state["queue_index"] = len(state["queue"])
        memory.save_run(state["local_id"], state)
        emit("action", run_id=state["run_id"], step=state["step"], action=action["name"],
             http=response["http_status"], error=response.get("error"), operation_id=oid,
             stop_reason=response.get("stop_reason"))
        if counters.get(key, 0) >= 5:
            state.update(queue=[], queue_index=0, runner_status="blocked", phase="blocked",
                blocked_reason=f"Simulator returned INTERNAL_ERROR five times for {key}")
            memory.save_run(state["local_id"], state)
            emit("blocked", run_id=state["run_id"], reason=state["blocked_reason"])
            raise RuntimeError(state["blocked_reason"] + "; checkpoint saved. Resume after simulator recovery.")
    state["queue"] = []
    state["queue_index"] = 0
    memory.save_run(state["local_id"], state)


def expand_actions(actions):
    # The LLM supplies the count; expansion only assigns recoverable queue positions.
    return [{**action, "repeat_count": 1} for action in actions
            for _ in range(action.get("repeat_count", 1))]


def run_agent(memory, state, brain, max_steps=250, strategy="", stop=None):
    with run_lock(memory, state["local_id"]), checkpoint_errors(memory, state):
        build = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()[:12]
        if build not in state.setdefault("runtime_versions", []):
            state["runtime_versions"].append(build)
        state["strategy"] = strategy or state.get("strategy", "")
        state["effort"] = brain.effort
        state["runner_status"] = "running"
        state.pop("blocked_reason", None)
        state.pop("last_error", None)
        sim = bootstrap(memory, state)
        instructions = passport(memory, state)
        # A restart may have happened between recording intent and receiving response.
        if state["queue"]:
            execute_queue(memory, sim, state, stop)
        for _ in range(max_steps + 1):
            if stop is not None and stop.is_set():
                break
            state["phase"] = "observe"
            memory.save_run(state["local_id"], state)
            observed = observe(sim, state)
            overview = observed["overview"]
            state["last_observation"] = observed
            state["status"] = overview["status"]
            done = state["status"] in ("completed", "failed")
            memory.event(state["local_id"], "completed" if done else "observation", observed)
            memory.save_run(state["local_id"], state)
            availability = overview["availability"]
            emit("state", run_id=state["run_id"], step=state["step"], time=overview["clock"]["simulation_time"],
                 uptime=availability.get("uptime_ratio"), cost=overview["costs"]["total_cost_minor"], status=state["status"])
            if done and state.get("reflected"):
                state["runner_status"] = "finished"
                memory.save_run(state["local_id"], state)
                return state
            if not done and _ == max_steps:
                break
            query = dumps([state["tags"], state["plan"], observed.get("recent_errors"), observed.get("resources")])
            recalled = memory.recall(query) if state["recall"] else []
            context = {"observation": observed, "working_memory": state["plan"],
                "remembered_mission": state.get("mission"), "recalled_memory": recalled,
                "recent_events": memory.recent(state["local_id"], 10), "final_reflection": done,
                "review_mode": done or state["step"] % 8 == 0,
                "execution_stats": {"decisions_so_far":state["decisions"],
                                    "llm_seconds_so_far":round(state["llm_seconds"],2)},
                "experiment_hint": state["strategy"], "remaining_decision_budget": max_steps - _,
                "instruction": "Choose next action; only the simulator defines final completion."}
            state["phase"] = "decide"
            memory.save_run(state["local_id"], state)
            decision = brain.decide(instructions, context)
            apply_decision(memory, state, decision, recalled, done)
            state["decisions"] += 1
            state["llm_seconds"] += brain.usage.get("seconds", 0)
            memory.event(state["local_id"], "usage", brain.usage)
            if done:
                state["reflected"] = True
                state["phase"] = "completed"
                state["runner_status"] = "finished"
                memory.save_run(state["local_id"], state)
                memory.maintain() if state["learn"] else memory.trim_history()
                emit("completed", **result_summary(state))
                return state
            state["step"] += 1
            state["queue"] = expand_actions(decision["actions"])
            state["queue_index"] = 0
            state["phase"] = "execute"
            # This commit makes the entire action plan recoverable before any mutation.
            memory.save_run(state["local_id"], state)
            execute_queue(memory, sim, state, stop)
            if state["learn"]:
                forgotten = memory.maintain()
                if forgotten:
                    emit("forgotten", lessons=forgotten)
            else:
                memory.trim_history()
        reason = "interrupted" if stop is not None and stop.is_set() else "decision budget"
        emit("paused", local_id=state["local_id"], run_id=state["run_id"], reason=reason,
             resume=shlex.join(["./run.sh", "resume", state["local_id"], "--memory", str(memory.path)]))
        state["runner_status"] = "paused"
        memory.save_run(state["local_id"], state)
        return state


def result_summary(state):
    overview = state.get("last_observation", {}).get("overview", {})
    a = overview.get("availability", {})
    return {"local_id": state["local_id"], "run_id": state.get("run_id"), "seed": state["seed"],
            "model": state["model"], "status": state["status"], "uptime": a.get("uptime_ratio"),
            "slo_passed": a.get("slo_passed"), "cost_minor": overview.get("costs", {}).get("total_cost_minor"),
            "currency": overview.get("costs", {}).get("currency"), "downtime_seconds": a.get("downtime_seconds"),
            "decisions": state["decisions"], "llm_seconds": round(state["llm_seconds"], 2),
            "memory_enabled": state["recall"], "experiment": state.get("experiment", ""),
            "runtime_versions": state.get("runtime_versions", []), "effort": state.get("effort", "low"),
            "runner_status": state.get("runner_status"), "blocked_reason": state.get("blocked_reason"),
            "error": state.get("last_error")}
