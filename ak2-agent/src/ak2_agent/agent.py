from __future__ import annotations

import json
import re
import signal
import threading
import time
import uuid

from .files import ROOT, atomic, digest, dumps, emit, inside, lock, redact, tree
from .models import Observation
from .recovery import recoverable, wait_retry
from .budget import TimeBudgetExpired, set_budget, budget_status, check_budget
from .sandbox import Sandbox
from .worlds import apply_proposal, snapshot
from .context import context_history, observation_outline
from .retrospective import retrospect, TERMINAL
from .learning import learn_checked


def runtime_fingerprint():
    return digest({str(p.relative_to(ROOT)): p.read_text() for pattern in
        ("src/ak2_agent/*.py", "prompts/*.md") for p in sorted(ROOT.glob(pattern))})


LOADED_RUNTIME = runtime_fingerprint()


class RuntimeUpdated(RuntimeError):
    pass


class AdaptationPaused(RuntimeError):
    pass


class AdaptationExhausted(RuntimeError):
    pass


def create_run(world, memory, seed, model, effort="low", *, task="", options=None,
               recall=True, learn=True, experiment="", knowledge_source=None, time_budget_seconds=None):
    world.require_exists()
    if seed <= 0:
        raise ValueError("seed must be positive")
    ident = "ak2-" + uuid.uuid4().hex[:20]
    state = {"id": ident, "world": world.id, "seed": seed, "model": model, "effort": effort,
        "task": task, "options": {**world.manifest.get("defaults", {}), **(options or {})},
        "recall": recall, "learn": learn, "experiment": experiment,
        "status": "starting", "step": 0, "decisions": 0, "llm_seconds": 0,
        "plan": "", "mission": "", "tags": [], "queue": [], "queue_index": 0,
        "accepted_lessons": [], "created": time.time(), "reflected": False,
        "knowledge_frozen": knowledge_source is not None or not learn, "repairs": 0}
    set_budget(state, time_budget_seconds)
    folder = world.local / "runs" / ident
    (folder / "io").mkdir(parents=True, mode=0o700)
    (folder / "knowledge").mkdir()
    if knowledge_source:
        state["knowledge_hash"] = snapshot(knowledge_source, folder / "knowledge")
    memory.save(state)
    if learn:
        with memory.db:
            memory.db.execute("INSERT OR IGNORE INTO trained VALUES (?)", (seed,))
    return state


def folder_for(world, state):
    world.require_exists()
    if state["world"] != world.id:
        raise ValueError("Run belongs to a different world")
    return inside(world.local, "runs/" + state["id"])


def payload(state, operation, action=None, request_id=None):
    return {"operation": operation, "action": action or {}, "request_id": request_id or "",
            "seed": state["seed"], "task": state["task"], "options": state["options"]}


def sandbox_for(folder, state):
    check_budget(state)
    return Sandbox(folder / "knowledge", folder / "io")


def bootstrap(world, memory, state):
    check_budget(state)
    folder = folder_for(world, state)
    if state.get("bootstrapped"):
        return json.loads((folder / "io/bootstrap.json").read_text())
    request_id = state["id"] + "-bootstrap"
    env = {"AK_" + k.upper(): str(v) for k, v in state["options"].items() if re.fullmatch(r"[a-z][a-z0-9_]*", k)}
    env.update(AK_SEED=str(state["seed"]), AK_REQUEST_ID=request_id)
    request = {"command": world.manifest["bootstrap"], "env": env}
    response = memory.prepare(request_id, state["id"], request, world.manifest["bootstrap_replay_safe"])
    if response is None:
        for attempt in range(3):
            try:
                response = sandbox_for(folder, state).run(
                    ["/bin/sh", "-c", request["command"]], extra_env=env, first_json=True)
                break
            except (RuntimeError, ValueError, OSError):
                if not world.manifest["bootstrap_replay_safe"] or attempt == 2:
                    raise
                emit("bootstrap_retry", local_id=state["id"], attempt=attempt+2, request_id=request_id)
                time.sleep(attempt+1)
        memory.received(request_id, response)
    atomic(folder / "io/bootstrap.json", dumps(response))
    state["bootstrapped"] = True
    memory.save(state)
    memory.event(state["id"], "bootstrap", redact(response))
    emit("started", world=world.id, local_id=state["id"], seed=state["seed"])
    return response


def adapt(world, memory, state, brain, *, max_rounds=8, force=False):
    folder = folder_for(world, state)
    check_budget(state)
    pending = bool(state.get("adaptation_pending"))
    requested = bool(state.get("adapt_feedback"))
    if state.get("knowledge_hash") and not force and not pending and not requested:
        return
    with lock(world.local / "adapt.lock", wait=True):
        world.require_exists()
        if world.ready() and not force and not pending and not requested:
            state["knowledge_hash"] = snapshot(world.knowledge, folder / "knowledge")
            memory.save(state)
            return
        published_hash = digest(tree(world.knowledge))
        base_hash = state.get("adaptation_base_hash", state.get("knowledge_hash"))
        if pending and base_hash != published_hash:
            state["unpublished_draft"] = tree(folder / "knowledge")
            snapshot(world.knowledge, folder / "knowledge")
        elif not pending:
            snapshot(world.knowledge, folder / "knowledge")
        state["adaptation_pending"] = True
        state["adaptation_base_hash"] = published_hash
        memory.save(state)
        files = tree(folder / "knowledge")
        feedback = state.get("adapt_feedback")
        capability_request = (feedback or {}).get("capability_request")
        recovery_request = (feedback or {}).get("recovery_request")
        verified = False
        if pending and "scripts/adapter.py" in files:
            # Saved failure feedback is historical. Recheck the current source before
            # asking the model whether a resumed adaptation must remain paused.
            if runtime_fingerprint() != LOADED_RUNTIME:
                raise RuntimeUpdated()
            observed_at = time.monotonic()
            try:
                observed = Observation.model_validate(sandbox_for(folder, state).adapter(payload(state, "observe"))).model_dump()
                feedback = {"validation": "partial" if observed["error"] else "passed",
                            "observation": redact(observed)}
            except (ValueError, RuntimeError, OSError) as error:
                feedback = {"validation": "failed", "error": str(error)[:4000]}
            feedback.update(phase="resume_check", checked_at=time.time(),
                            observation_seconds=round(time.monotonic()-observed_at, 3))
            if capability_request:
                feedback["capability_request"] = capability_request
            if recovery_request:
                feedback["recovery_request"] = recovery_request
            state["adapt_feedback"] = feedback
            memory.save(state)
            emit("adaptation_recheck", local_id=state["id"], validation=feedback["validation"],
                 observation_seconds=feedback["observation_seconds"])
        for number in range(max_rounds):
            check_budget(state)
            if runtime_fingerprint() != LOADED_RUNTIME:
                raise RuntimeUpdated()
            world.require_exists()
            emit("adapting", world=world.id, round=number+1)
            proposal = brain.adapt({"world": world.manifest,
                "bootstrap": redact(json.loads((folder / "io/bootstrap.json").read_text())),
                "knowledge": files, "feedback": feedback,
                "unpublished_draft": state.get("unpublished_draft")})
            state["llm_seconds"] += brain.usage.get("seconds", 0)
            emit("brain_usage", local_id=state["id"], phase="adapt", **brain.usage)
            if proposal.get("pause_reason"):
                raise AdaptationPaused(proposal["pause_reason"])
            try:
                check_budget(state)
                candidate = apply_proposal(files, proposal)
                if len(dumps(candidate).encode()) > 1_000_000:
                    raise ValueError("Knowledge exceeds 1 MB")
                for name, content in candidate.items():
                    atomic(inside(folder / "knowledge", name), content)
                files = candidate
                required = {"scripts/adapter.py", "skills/operate/SKILL.md", "prompts/world.md"}
                if not required <= files.keys():
                    raise ValueError("Missing required files: " + dumps(sorted(required-files.keys())))
                skill = files["skills/operate/SKILL.md"]
                if not re.match(r"---\n(?=.*\bname:)(?=.*\bdescription:).*?\n---", skill, re.S):
                    raise ValueError("SKILL.md needs YAML name and description")
                observed_at = time.monotonic()
                observed = Observation.model_validate(sandbox_for(folder, state).adapter(payload(state, "observe"))).model_dump()
                if observed["error"] and not observed["data"]:
                    raise ValueError(observed["error"])
                feedback = {"validation": "partial" if observed["error"] else "passed",
                            "observation_seconds": round(time.monotonic()-observed_at, 3),
                            "observation": redact(observed)}
                # Always let the model inspect at least one actual protocol/catalog response.
                if proposal["ready"] and verified:
                    check_budget(state)
                    world.publish(files)
                    state["knowledge_hash"] = digest(files)
                    state.pop("adapt_feedback", None)
                    state.pop("adaptation_pending", None)
                    state.pop("adaptation_base_hash", None)
                    state.pop("unpublished_draft", None)
                    memory.save(state)
                    memory.event(state["id"], "adapted", {"knowledge_hash": state["knowledge_hash"], "files": sorted(files)})
                    emit("adapted", world=world.id, files=sorted(files), knowledge_hash=state["knowledge_hash"])
                    return
                verified = True
            except (ValueError, RuntimeError, OSError) as e:
                feedback = {"validation": "failed", "error": str(e)[:4000]}
                verified = False
            if capability_request:
                feedback["capability_request"] = capability_request
            if recovery_request:
                feedback["recovery_request"] = recovery_request
            state["adapt_feedback"] = feedback
            memory.save(state)
        raise AdaptationExhausted("Adaptation budget exhausted; checkpoint saved, resume to continue")


def reflect_failure(world, memory, state, brain, error):
    """Close a failed adaptation's learning loop without issuing world actions."""
    incident = "runtime-incident:" + str(state["step"])
    if not state["learn"] or incident in state.get("reviewed_incidents", []):
        return
    world.require_exists()
    failed = (state.get("adapt_feedback") or {}).get("observation") or {}
    # This fact describes agent execution only, not the world's result or a cause.
    detail = {"scope": "agent_execution", "phase": "adaptation", "error": str(error),
              "does_not_evaluate_world_goal": True, "observation_error": failed.get("error")}
    memory.observe(state["id"], {"data": {}, "evidence": [{"identity": incident,
        "kind": "effect", "outcome": "failure", "detail": detail}]})
    facts = memory.facts(state["id"])
    recalled = memory.recall(dumps([state["plan"], detail])) if state["recall"] else []
    folder = folder_for(world, state)
    try:
        reflection = brain.reflect_failure({"incident": redact(detail), "evidence": facts,
            "mission": state["mission"], "working_memory": state["plan"], "recalled_memory": recalled,
            "knowledge": {p:c for p,c in tree(folder/"knowledge").items() if not p.startswith("scripts/")},
            "last_observation_outline": observation_outline(state.get("last_observation", {})),
            "failed_observation_outline": observation_outline(failed),
            "recent_events": context_history(memory.recent(state["id"])),
            "learning_feedback": state.get("learning_feedback", [])})
        state["llm_seconds"] += brain.usage.get("seconds", 0)
        emit("brain_usage", local_id=state["id"], phase="failure_reflection", **brain.usage)
        accepted, feedback = [], []
        for lesson in reflection["lessons"]:
            key = learn_checked(memory, state, brain, lesson, [f["id"] for f in facts],
                context={"recent_events": context_history(memory.recent(state["id"])),
                         "observation": state.get("last_observation", {})})
            feedback.append(memory.learning_feedback)
            if key:
                accepted.append(key)
        memory.used(set(reflection["used_memory_ids"]) & {x["id"] for x in recalled})
        state["reviewed_incidents"] = [*state.get("reviewed_incidents", []), incident][-20:]
        state["failure_reflection"] = {**reflection, "incident": incident,
            "accepted_lessons": accepted, "learning_feedback": feedback}
        memory.event(state["id"], "failure_reflection", state["failure_reflection"])
        memory.save(state)
        emit("failure_reflection", local_id=state["id"], assessment=reflection["assessment"], accepted_lessons=accepted)
    except Exception as review_error:
        # A secondary model failure must not replace the original failure/checkpoint.
        memory.event(state["id"], "failure_reflection_error", {"error": str(review_error)[:2000]})
        emit("failure_reflection_error", local_id=state["id"], error=str(review_error)[:2000])


def repair_adapter(world, memory, state, brain, error, *, recovery=None):
    check_budget(state)
    if brain is None or state.get("knowledge_frozen") or state.get("repairs", 0) >= 2:
        raise error
    if "sandbox_apply" in str(error) or "sandbox unavailable" in str(error):
        raise error
    state["repairs"] = state.get("repairs", 0)+1
    state["adapt_feedback"] = {"validation": "failed", "error": str(error)[:4000]}
    if recovery:
        state["adapt_feedback"]["recovery_request"] = redact(recovery)
    memory.save(state)
    emit("repairing_adapter", world=world.id, local_id=state["id"], attempt=state["repairs"])
    adapt(world, memory, state, brain, force=True)


def invoke(world, memory, state, action, request_id, replay_safe, brain=None, _attempt=0):
    folder = folder_for(world, state)
    check_budget(state)
    body = payload(state, "act", action, request_id)
    result = memory.prepare(request_id, state["id"], body, replay_safe)
    if result is None:
        try:
            result = Observation.model_validate(sandbox_for(folder, state).adapter(body)).model_dump()
        except (RuntimeError, ValueError, OSError) as error:
            if not replay_safe:
                raise RuntimeError("Unknown action result; unsafe to replay. Checkpoint preserved") from error
            repair_adapter(world, memory, state, brain, error)
            result = Observation.model_validate(sandbox_for(folder, state).adapter(body)).model_dump()
        delivery_unknown = result["delivery"] == "unknown" or (
            result["delivery"] is None and result["error"] and not any(
                f["kind"] == "action" and f["outcome"] == "failure" for f in result["evidence"]))
        if delivery_unknown:
            memory.event(state["id"], "uncertain_delivery", {"request_id": request_id, "response": result})
            if replay_safe and _attempt < 2:
                emit("action_retry", local_id=state["id"], request_id=request_id, attempt=_attempt+2)
                time.sleep(2**_attempt)
                return invoke(world, memory, state, action, request_id, replay_safe, brain, _attempt+1)
            error = RuntimeError("Action outcome is unknown; queue and request_id preserved. Resume retries the same request only when replay_safe")
            if replay_safe and _attempt == 2:
                repair_adapter(world, memory, state, brain, error,
                               recovery={"request": body, "response": result})
                # One check after repair, preserving the original payload and identity.
                return invoke(world, memory, state, action, request_id, replay_safe, brain, _attempt+1)
            raise error
        # A failed read is a completed diagnostic attempt, not a receipt for a world effect.
        # Unknown effects remain pending; they cannot be replaced by a new action.
        memory.received(request_id, result)
    memory.observe(state["id"], result)
    memory.event(state["id"], "outcome", {"request_id": request_id, "action": action, "response": result})
    return result


def execute_queue(world, memory, state, stop=None, brain=None):
    while state["queue_index"] < len(state["queue"]):
        check_budget(state)
        if stop is not None and stop.is_set():
            return
        item = state["queue"][state["queue_index"]]
        result = invoke(world, memory, state, item["action"], item["request_id"], item["replay_safe"], brain)
        state["queue_index"] += 1
        state["last_action_result"] = result
        memory.save(state)
        emit("action", local_id=state["id"], request_id=item["request_id"],
             error=result["error"], pending=result["pending"], metrics=result["metrics"])
        if result["error"] or result["done"] or (result["pending"] and item["wait_for_completion"]):
            break
    if stop is None or not stop.is_set():
        state["queue"], state["queue_index"] = [], 0
        memory.save(state)


def review_checkpoint(memory, state, reason, *, active=False):
    checkpoint = {"id": uuid.uuid4().hex, "status": "active" if active else "pending",
                  "reason": reason, "created_at": time.time()}
    state["review_checkpoint"] = checkpoint
    memory.save(state)
    return checkpoint


def finish_review(world, memory, state, brain):
    checkpoint = state["review_checkpoint"]
    if checkpoint["status"] == "active":
        checkpoint.update(status="pending", reason="process_interrupted")
        if state.get("last_observation", {}).get("done"):
            state["status"] = "finished"
        else:
            state.update(status="paused", pause_reason="process_interrupted")
        memory.save(state)
    # A stop signal ends execution, not its retrospective. Another interrupt
    # during Review.run pauses the review itself and preserves its receipts.
    retrospect(world, memory, state, brain, checkpoint=checkpoint, renew=True)
    if state.get("retrospective", {}).get("status") in TERMINAL:
        checkpoint["status"] = "reviewed"
        memory.save(state)


def reflect_run(world, memory, state, brain):
    """Review a saved run without entering its world loop or repeating actions."""
    with lock(folder_for(world, state) / "run.lock"):
        if "review_checkpoint" not in state:
            review_checkpoint(memory, state, state.get("pause_reason") or state.get("status", "manual"))
        if state["review_checkpoint"]["status"] != "reviewed":
            finish_review(world, memory, state, brain)
        return state


def run_agent(world, memory, state, brain, *, max_steps=250, strategy="", stop=None,
              adapt_only=False, time_budget_seconds=None):
    # One owner covers execution, recovery and its final read-only review.
    with lock(folder_for(world, state) / "run.lock"):
        checkpoint = state.get("review_checkpoint")
        if checkpoint and checkpoint["status"] != "reviewed":
            finish_review(world, memory, state, brain)
            if checkpoint["status"] != "reviewed":
                return state
        if state.get("reflected") or state.get("last_observation", {}).get("done"):
            state["status"] = "finished"
            memory.save(state)
            if not checkpoint:
                review_checkpoint(memory, state, "world_completed")
                finish_review(world, memory, state, brain)
            return state
        checkpoint = review_checkpoint(memory, state, "execution", active=True)
        previous_term = None
        if threading.current_thread() is threading.main_thread():
            previous_term = signal.getsignal(signal.SIGTERM)
            def terminate(signum, frame):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, terminate)
        try:
            return _execute_agent(world, memory, state, brain, max_steps=max_steps,
                strategy=strategy, stop=stop, adapt_only=adapt_only,
                time_budget_seconds=time_budget_seconds)
        except KeyboardInterrupt:
            state.update(status="paused", pause_reason="interrupted")
            raise
        except Exception as error:
            state.update(status="error", error=str(error) or type(error).__name__)
            raise
        finally:
            checkpoint = state["review_checkpoint"]  # Recovery can reload state.
            if state.get("last_observation", {}).get("done"):
                # A terminal observation remains authoritative even when the
                # execution deadline interrupted the inline final assessment.
                state["status"] = "finished"
            checkpoint.update(status="pending", reason=state.get("pause_reason") or
                ("world_completed" if state.get("last_observation", {}).get("done") else
                 "error" if state.get("error") else "adaptation_only" if adapt_only else "step_limit"))
            memory.save(state)
            try:
                finish_review(world, memory, state, brain)
            finally:
                if previous_term is not None:
                    signal.signal(signal.SIGTERM, previous_term)


def _execute_agent(world, memory, state, brain, *, max_steps=250, strategy="", stop=None, adapt_only=False, time_budget_seconds=None):
    set_budget(state, time_budget_seconds)
    memory.save(state)
    initial_decisions = state.get("decisions", 0)
    attempt = 0
    reconnect = False
    if state.get("reflected"):
        return state
    while True:
        try:
            check_budget(state)
            if stop is not None and stop.is_set():
                state.update(status="paused", pause_reason="stop_requested")
                memory.save(state)
                return state
            if reconnect:
                brain.reconnect()
                emit("recovery_resume", local_id=state["id"], attempt=attempt)
            remaining = max(0, max_steps - (state.get("decisions", 0)-initial_decisions))
            result = _run_attempt(world, memory, state, brain, max_steps=remaining,
                strategy=strategy, stop=stop, adapt_only=adapt_only)
            return result
        except TimeBudgetExpired:
            state.update(status="paused", pause_reason="time_budget_exhausted")
            state.pop("error", None)
            memory.save(state)
            emit("paused", local_id=state["id"], reason=state["pause_reason"])
            return state
        except KeyboardInterrupt:
            state.update(status="paused", pause_reason="interrupted")
            memory.save(state)
            raise
        except Exception as error:
            if not recoverable(error):
                raise
            # Only persisted progress is authoritative, including action receipts.
            saved = memory.run(state["id"])
            state.clear()
            state.update(saved)
            attempt += 1
            delay = min(60, 5 * 2**min(attempt-1, 4))
            remaining_time = budget_status(state)["remaining_seconds"]
            if remaining_time is not None:
                delay = min(delay, remaining_time)
            state.update(status="recovering", error=str(error),
                recovery={"attempt": attempt, "retry_at": time.time()+delay})
            memory.save(state)
            emit("recovery_wait", local_id=state["id"], attempt=attempt,
                delay_seconds=delay, error=str(error), model=state.get("model"), effort=state.get("effort"))
            try:
                wait_retry(delay, stop)
            except KeyboardInterrupt:
                state.update(status="paused", pause_reason="interrupted")
                memory.save(state)
                raise
            reconnect = True


def _run_attempt(world, memory, state, brain, *, max_steps=250, strategy="", stop=None, adapt_only=False, time_budget_seconds=None):
    folder = folder_for(world, state)
    try:
        if state.get("reflected"):
            return state
        set_budget(state, time_budget_seconds)
        memory.save(state)
        brain.deadline_at = state.get("deadline_at")
        check_budget(state)
        state.pop("pause_reason", None)
        versions = state.setdefault("runtime_versions", [])
        if not versions or versions[-1]["hash"] != LOADED_RUNTIME:
            versions.append({"hash": LOADED_RUNTIME, "started": time.time()})
            state["runtime_versions"] = versions[-20:]
            memory.save(state)
        bootstrap(world, memory, state)
        adapt(world, memory, state, brain)
        check_budget(state)
        if adapt_only:
            state["status"] = "adapted"
            memory.save(state)
            return state
        if digest(tree(folder / "knowledge")) != state["knowledge_hash"]:
            raise ValueError("Pinned knowledge changed; cannot safely resume this run")
        execute_queue(world, memory, state, stop, brain)
        for index in range(max_steps+1):
            check_budget(state)
            if stop is not None and stop.is_set():
                break
            if runtime_fingerprint() != LOADED_RUNTIME:
                state["pause_reason"] = "runtime_updated"
                emit("runtime_updated", local_id=state["id"], reason="Checkpoint at action boundary; resume loads the new core")
                break
            world.require_exists()
            observed_at = time.monotonic()
            try:
                observation = Observation.model_validate(sandbox_for(folder, state).adapter(payload(state, "observe"))).model_dump()
            except (RuntimeError, ValueError, OSError) as error:
                repair_adapter(world, memory, state, brain, error)
                observed_at = time.monotonic()
                observation = Observation.model_validate(sandbox_for(folder, state).adapter(payload(state, "observe"))).model_dump()
            state["observation_seconds"] = round(time.monotonic()-observed_at, 3)
            memory.observe(state["id"], observation)
            state.update(last_observation=observation, external_id=observation["external_id"], status="running")
            state.pop("error", None)
            memory.save(state)
            emit("state", local_id=state["id"], world=world.id, step=state["step"],
                 observation_seconds=state["observation_seconds"],
                 done=observation["done"], success=observation["success"], metrics=observation["metrics"])
            if index == max_steps and not observation["done"]:
                break
            check_budget(state)
            recalled = memory.recall(dumps([state["tags"], state["plan"], observation])) if state["recall"] else []
            facts = memory.facts(state["id"])
            knowledge = {p: c for p, c in tree(folder / "knowledge").items() if not p.startswith("scripts/")}
            # Keep stable material before changing diagnostic timestamps so
            # consecutive SDK requests can reuse a longer identical prefix.
            context = {"task": state["task"], "knowledge": knowledge,
                "observation": redact(observation), "observation_outline": observation_outline(observation),
                "remembered_mission": state["mission"], "working_memory": state["plan"],
                "recalled_memory": recalled, "evidence": facts,
                "recent_events": context_history(memory.recent(state["id"])),
                "accepted_lessons": state["accepted_lessons"], "final_reflection": observation["done"],
                "learning_feedback": state.get("learning_feedback", []),
                "review_mode": state["step"] % 8 == 0 or observation["done"],
                "can_adapt": not state.get("knowledge_frozen", False),
                "remaining_decision_budget": max_steps-index, "experiment_hint": strategy,
                "time_budget": budget_status(state),
                "execution_stats": {"decisions": state["decisions"], "llm_seconds": state["llm_seconds"],
                                    "last_observation_seconds": state["observation_seconds"],
                                    "last_decision_usage": state.get("last_decision_usage")}}
            decision = brain.decide(context)
            check_budget(state)
            state["last_decision_usage"] = dict(brain.usage)
            emit("brain_usage", local_id=state["id"], phase="decide", **brain.usage)
            accepted = []
            learning_feedback = []
            if state["learn"]:
                for lesson in decision["lessons"]:
                    key = learn_checked(memory, state, brain, lesson, [f["id"] for f in facts],
                        context={"recent_events": context_history(memory.recent(state["id"])),
                                 "observation": state.get("last_observation", {})})
                    learning_feedback.append(memory.learning_feedback)
                    if key:
                        accepted.append(key)
                memory.used(set(decision["used_memory_ids"]) & {x["id"] for x in recalled})
            state.update(mission=decision["mission"], plan=decision["plan"], tags=decision["tags"],
                         accepted_lessons=accepted, learning_feedback=learning_feedback)
            state["decisions"] += 1
            state["llm_seconds"] += state["last_decision_usage"].get("seconds", 0)
            memory.event(state["id"], "reflection" if observation["done"] else "decision",
                         {**decision, "accepted_lessons": accepted, "learning_feedback": learning_feedback})
            emit("decision", local_id=state["id"], assessment=decision["assessment"],
                 used_memory=decision["used_memory_ids"], accepted_lessons=accepted)
            if decision.get("pause_reason"):
                state.update(status="paused", pause_reason=decision["pause_reason"])
                memory.save(state)
                emit("paused", local_id=state["id"], world=world.id, reason=state["pause_reason"])
                return state
            if decision.get("adaptation_request"):
                if state.get("knowledge_frozen") or observation["done"]:
                    raise ValueError("This run cannot adapt its knowledge now")
                state["adapt_feedback"] = {"capability_request": decision["adaptation_request"],
                    "observation": redact(observation)}
                memory.save(state)
                memory.event(state["id"], "adaptation_requested", {"request": decision["adaptation_request"]})
                adapt(world, memory, state, brain, force=True)
                continue
            if observation["done"]:
                state.update(reflected=True, status="finished")
                memory.save(state)
                memory.maintain(run=state["id"]) if state["learn"] else memory.trim()
                emit("completed", **summary(state))
                return state
            state["step"] += 1
            state["queue"] = []
            for action in decision["actions"]:
                for _ in range(action["repeat_count"]):
                    state["queue"].append({"action": json.loads(action["payload_json"]),
                        "request_id": state["id"] + "-" + uuid.uuid4().hex,
                        "replay_safe": action["replay_safe"], "wait_for_completion": action["wait_for_completion"]})
            state["queue_index"] = 0
            memory.save(state)
            execute_queue(world, memory, state, stop, brain)
            forgotten = memory.maintain(run=state["id"]) if state["learn"] else []
            if forgotten:
                emit("forgotten", world=world.id, lessons=forgotten)
        state["status"] = "paused"
        memory.save(state)
        emit("paused", local_id=state["id"], world=world.id)
        return state
    except TimeBudgetExpired:
        state.update(status="paused", pause_reason="time_budget_exhausted")
        state.pop("error", None)
        memory.save(state)
        emit("paused", local_id=state["id"], world=world.id, reason="time_budget_exhausted",
             time_budget=budget_status(state))
        return state
    except AdaptationPaused as error:
        state.update(status="paused", pause_reason=str(error))
        memory.save(state)
        reflect_failure(world, memory, state, brain, error)
        emit("paused", local_id=state["id"], world=world.id, reason=str(error))
        return state
    except RuntimeUpdated:
        state.update(status="paused", pause_reason="runtime_updated")
        memory.save(state)
        emit("runtime_updated", local_id=state["id"], reason="Adaptation checkpoint saved; resume loads the new core")
        return state
    except BaseException as error:
        state["status"] = "paused" if isinstance(error, KeyboardInterrupt) else "error"
        state["error"] = str(error) or type(error).__name__
        if (world.path / "AGENTS.md").is_file():
            memory.save(state)
            if isinstance(error, AdaptationExhausted):
                reflect_failure(world, memory, state, brain, error)
        raise


def summary(state):
    obs = state.get("last_observation", {})
    return {"world": state["world"], "local_id": state["id"], "external_id": state.get("external_id"),
        "seed": state["seed"], "model": state["model"], "status": state["status"],
        "done": obs.get("done", False), "success": obs.get("success"), "metrics": obs.get("metrics", {}),
        "decisions": state["decisions"], "llm_seconds": round(state["llm_seconds"], 2),
        "time_budget": budget_status(state),
        "memory_enabled": state["recall"], "learning_enabled": state["learn"],
        "knowledge_hash": state.get("knowledge_hash"), "error": state.get("error"),
        "pause_reason": state.get("pause_reason"),
        "retrospective": {k: state.get("retrospective", {}).get(k) for k in
                          ("status", "reason", "reads", "decisions", "verified_history", "accepted_lessons")}}
