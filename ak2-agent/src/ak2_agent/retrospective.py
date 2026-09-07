"""Bounded review of every execution stop, with optional historical enrichment."""
from __future__ import annotations

import json
import time

from .budget import check_deadline, TimeBudgetExpired
from .context import context_history
from .files import atomic, digest, dumps, emit, inside, redact, tree
from .models import HistoryCatalog, HistoryPage, HistoryPreparation, RetrospectiveDecision
from .learning import learn_checked
from .sandbox import Sandbox
from .worlds import snapshot, validate_artifact

LOCAL_SOURCE = "__local_events__"
MAX_SECONDS = 300
MAX_DECISIONS = 8
MAX_READS = 6
MAX_PREPARATIONS = 2
MAX_DISCOVERIES = 3
TERMINAL = {"completed", "unsupported", "exhausted", "disabled"}


class ReviewPaused(Exception):
    pass


class Review:
    def __init__(self, world, memory, state, brain, *, stop=None, current_runtime=None, renew=False, checkpoint=None):
        self.world, self.memory, self.state, self.brain = world, memory, state, brain
        self.stop, self.current_runtime = stop, current_runtime
        self.folder = inside(world.local, "runs/" + state["id"])
        self.root = self.folder / "retrospective"
        checkpoint = checkpoint or state.get("review_checkpoint")
        checkpoint_id = checkpoint["id"] if checkpoint else None
        previous = state.get("retrospective")
        if previous and previous.get("checkpoint_id") is None and previous.get("status") not in TERMINAL:
            # Upgrade an unfinished review from before per-stop checkpoints.
            previous["checkpoint_id"] = checkpoint_id
            previous.setdefault("report_path", "retrospective/report.json")
        if previous and previous.get("checkpoint_id") != checkpoint_id:
            state.setdefault("retrospective_history", []).append({k: previous.get(k) for k in
                ("checkpoint_id", "report_path", "status", "reason", "accepted_lessons")})
            state.pop("retrospective")
        if checkpoint_id:
            retained = state.get("retrospective", {}).get("report_path")
            self.root = inside(self.folder, retained).parent if retained else inside(self.root, checkpoint_id)
        self.knowledge = self.root / "knowledge"
        self.review = state.setdefault("retrospective", {
            "status": "pending", "deadline_at": time.time() + MAX_SECONDS,
            "decisions": 0, "reads": 0, "preparations": 0, "discoveries": 0,
            "notes": "", "hypotheses": [], "pages": [], "verified_history": False,
            "accepted_lessons": [], "learning_feedback": []})
        self.review.setdefault("archive_through_id", self.memory.archive_info(state["id"])["through_id"])
        self.review.setdefault("checkpoint_id", checkpoint_id)
        self.review.setdefault("report_path", str(self.root.relative_to(self.folder) / "report.json"))
        self.review.setdefault("termination", dict(checkpoint or {
            "reason": state.get("pause_reason") or state.get("status", "unknown")}))
        self.review.setdefault("phase", "analysis" if self.review.get("catalog") else "preparation")
        if renew and self.review["status"] == "paused":
            self.review["deadline_at"] = time.time() + MAX_SECONDS

    def save(self):
        self.world.require_exists()
        self.memory.save(self.state)
        atomic(self.root / "report.json", dumps(redact(self.review)))

    def boundary(self):
        self.world.require_exists()
        if self.stop is not None and self.stop.is_set():
            raise ReviewPaused("stop_requested")
        if self.current_runtime is not None and not self.current_runtime():
            raise ReviewPaused("runtime_updated")
        check_deadline(self.deadline)

    @property
    def deadline(self):
        return self.review["deadline_at"]

    def think(self, method, context):
        self.boundary()
        self.brain.deadline_at = self.deadline
        result = getattr(self.brain, method)(context)
        usage = dict(self.brain.usage)
        self.state["llm_seconds"] += usage.get("seconds", 0)
        self.review["last_usage"] = usage
        emit("brain_usage", local_id=self.state["id"], phase=method, **usage)
        return result

    def history(self, operation, source_id="", parameters=None):
        self.boundary()
        if digest(tree(self.knowledge)) != self.review["knowledge_hash"]:
            raise ValueError("Pinned historical reader changed")
        seconds = max(.1, min(60, self.deadline-time.time()))
        sandbox = Sandbox(self.knowledge, self.folder / "io", timeout=seconds)
        result = sandbox.history({"operation": operation, "source_id": source_id,
            "parameters": parameters or {}, "seed": self.state["seed"],
            "task": self.state["task"], "options": self.state["options"],
            "budget_seconds": max(.05, seconds-1)})
        if len(dumps(result).encode()) > 500000:
            raise ValueError("Historical page exceeds 500000 bytes")
        return redact(result)

    def prepare(self):
        r = self.review
        if r.get("catalog", {}).get("status") == "supported":
            return
        if not r.get("knowledge_hash"):
            if not self.state.get("knowledge_hash") or not (self.folder / "io/bootstrap.json").is_file():
                raise ValueError("No verified world integration/bootstrap; review local records only")
            if digest(tree(self.folder / "knowledge")) != self.state["knowledge_hash"]:
                raise ValueError("Pinned world knowledge changed")
            r["knowledge_hash"] = snapshot(self.folder / "knowledge", self.knowledge)
            self.save()
        while r["discoveries"] < MAX_DISCOVERIES:
            self.boundary()
            if not (self.knowledge / "scripts/history.py").is_file() or r.get("prepare_feedback"):
                if r["preparations"] >= MAX_PREPARATIONS:
                    r.update(status="exhausted", reason="history_preparation_limit")
                    return
                r["preparations"] += 1
                self.save()  # A crashed/incomplete model call still consumes its allowance.
                proposal = self.think("prepare_history", {
                    "world": self.world.manifest,
                    "bootstrap": redact(json.loads((self.folder / "io/bootstrap.json").read_text())),
                    "world_done": self.state.get("last_observation", {}).get("done", False),
                    "knowledge": tree(self.knowledge), "feedback": r.get("prepare_feedback"),
                    "time_budget": {"remaining_seconds": max(0, self.deadline-time.time())}})
                try:
                    proposal = HistoryPreparation.model_validate(proposal).model_dump()
                    files = {f["path"]: f["content"] for f in proposal["files"]}
                    if "scripts/history.py" not in files or set(files) - {"scripts/history.py", "references/history.md"}:
                        raise ValueError("Only scripts/history.py and references/history.md may be prepared")
                    for name, content in files.items():
                        validate_artifact(name, content)
                    self.boundary()
                    for name, content in files.items():
                        atomic(inside(self.knowledge, name), content)
                    r["knowledge_hash"] = digest(tree(self.knowledge))
                    r.pop("prepare_feedback", None)
                    self.save()
                except ValueError as error:
                    r["prepare_feedback"] = str(error)[:2000]
                    self.save()
                    continue
            self.boundary()
            r["discoveries"] += 1
            self.save()
            try:
                catalog = HistoryCatalog.model_validate(self.history("discover")).model_dump()
                if any(x["id"] == LOCAL_SOURCE for x in catalog["sources"]):
                    raise ValueError("Historical source uses reserved local archive ID")
            except (ValueError, RuntimeError, OSError) as error:
                r["prepare_feedback"] = str(error)[:2000]
                self.save()
                continue
            r["catalog"] = catalog
            self.save()
            if catalog["status"] == "unsupported":
                r.update(status="unsupported", reason=catalog["reason"])
            elif catalog["status"] == "unavailable":
                raise ReviewPaused(catalog["reason"])
            return
        r.update(status="exhausted", reason="history_discovery_limit")

    def read_pending(self):
        r = self.review
        pending = r["pending_read"]
        query = pending["query"]
        sources = {s["id"] for s in r["catalog"]["sources"]} | {LOCAL_SOURCE}
        if query["source_id"] not in sources:
            raise ValueError("Unknown historical source")
        path = inside(self.root, pending["path"])
        if path.exists():
            page = HistoryPage.model_validate_json(path.read_text()).model_dump()
        else:
            self.boundary()
            if r["reads"] >= MAX_READS:
                # A killed read uses a slot too; let the model reflect on the gap.
                r.pop("pending_read")
                r["notes"] += "\nHistorical read budget exhausted before pending page was received."
                self.save()
                return
            r["reads"] += 1
            self.save()
            try:
                if query["source_id"] == LOCAL_SOURCE:
                    data = self.memory.archive_page(self.state["id"], json.loads(query["parameters_json"]), r["archive_through_id"])
                    page = {"source_id": LOCAL_SOURCE, "data": data, "evidence": [], "complete": True,
                            "next_parameters": data["next_parameters"], "error": None}
                else:
                    page = HistoryPage.model_validate(self.history("read", query["source_id"],
                        json.loads(query["parameters_json"]))).model_dump()
                if page["source_id"] != query["source_id"]:
                    raise ValueError("Historical response source differs from request")
            except (ValueError, RuntimeError, OSError) as error:
                page = {"source_id": query["source_id"], "data": {}, "evidence": [],
                        "complete": False, "next_parameters": None, "error": str(error)[:3000]}
            # Persist the receipt before importing facts or consuming the pending query.
            atomic(path, dumps(page))
        if query["source_id"] != LOCAL_SOURCE and page["complete"] and not page["error"]:
            r["verified_history"] = True
        facts = [{**f, "detail": {"historical_record": f["detail"],
                   "provenance": {"source_id": query["source_id"],
                                  "parameters": json.loads(query["parameters_json"]),
                                  "page": pending["path"]}}} for f in page["evidence"]]
        if query["source_id"] != LOCAL_SOURCE:
            self.memory.observe(self.state["id"], {"data": {"historical_page": pending["path"]}, "evidence": facts})
        r["pages"].append({"path": pending["path"], "query": query})
        r.pop("pending_read")
        self.save()

    def context(self):
        r = self.review
        recalled = self.memory.recall(dumps([self.state["tags"], self.state["plan"], r["notes"]])) if self.state["recall"] else []
        pages = [{**p, "response": json.loads(inside(self.root, p["path"]).read_text())} for p in r["pages"]]
        evidence = {f["id"]: f for f in self.memory.facts(self.state["id"])}
        for page in pages:
            if page["response"]["source_id"] == LOCAL_SOURCE:
                for fact in page["response"]["data"].get("facts", []):
                    evidence[fact["id"]] = fact
        return {"task": self.state["task"], "mission": self.state["mission"],
            "working_memory": self.state["plan"], "observation": self.state.get("last_observation", {}),
            "termination": r["termination"], "world_done": self.state.get("last_observation", {}).get("done", False),
            "learning_enabled": self.state.get("learn", False),
            "catalog": r["catalog"], "verified_history": r["verified_history"],
            "knowledge": {p: c for p, c in tree(self.knowledge).items() if not p.startswith("scripts/")},
            "recent_events": context_history(self.memory.recent(self.state["id"], 120)),
            "historical_pages": pages, "local_history": self.memory.archive_info(self.state["id"], r["archive_through_id"]),
            "evidence": list(evidence.values()), "recalled_memory": recalled,
            "known_lessons": [{k: x[k] for k in ("id", "body", "procedure")} for x in self.memory.lessons()]
                             if self.state["recall"] else [],
            "notes": r["notes"], "hypotheses": r["hypotheses"],
            "learning_feedback": r["learning_feedback"],
            "can_read": r["reads"] < MAX_READS and r["decisions"] < MAX_DECISIONS-1,
            "can_read_external": r["catalog"]["status"] == "supported" and r["reads"] < MAX_READS and r["decisions"] < MAX_DECISIONS-1,
            "time_budget": {"remaining_seconds": max(0, self.deadline-time.time())},
            "remaining_decisions": MAX_DECISIONS-r["decisions"]}

    def apply_decision(self):
        r = self.review
        pending = r["pending_decision"]
        decision = RetrospectiveDecision.model_validate(pending["result"]).model_dump()
        query = decision["query"]
        if query and query["source_id"] not in ({s["id"] for s in r["catalog"]["sources"]} | {LOCAL_SOURCE}):
            raise ValueError("Unknown historical source")
        feedback, accepted = [], []
        for lesson in decision["lessons"]:
            if not self.state.get("learn"):
                feedback.append({"key": lesson["key"], "accepted": False, "reason": "learning_disabled"})
                continue
            self.brain.deadline_at = self.deadline
            key = learn_checked(self.memory, self.state, self.brain, lesson, pending["evidence_ids"],
                context=pending.get("verification_context", {"recent_events": context_history(self.memory.recent(self.state["id"], 120))}))
            feedback.append(self.memory.learning_feedback)
            if key:
                accepted.append(key)
        if self.state.get("learn"):
            self.memory.used(set(decision["used_memory_ids"]) & set(pending["recalled_ids"]))
        r.update(notes=decision["notes"], hypotheses=decision["hypotheses"],
                 assessment=decision["assessment"], learning_feedback=feedback)
        r["accepted_lessons"] = sorted(set(r["accepted_lessons"]) | set(accepted))
        self.memory.event(self.state["id"], "retrospective_decision",
                          {**decision, "accepted_lessons": accepted, "learning_feedback": feedback})
        if decision["done"]:
            r.update(status="completed", reason="review_finished")
        elif r["reads"] < MAX_READS and r["decisions"] < MAX_DECISIONS:
            r["pending_read"] = {"query": query, "path": f"pages/{len(r['pages'])+1}.json"}
        else:
            r.update(status="exhausted", reason="retrospective_call_limit")
        r.pop("pending_decision")
        self.save()

    def run(self):
        r = self.review
        if r["status"] in TERMINAL:
            return
        old_deadline = getattr(self.brain, "deadline_at", None)
        try:
            self.boundary()
            r.update(status="running", reason=None)
            r.pop("error", None)
            self.save()
            if r["phase"] == "preparation":
                try:
                    self.prepare()
                except Exception as error:
                    if isinstance(error, ReviewPaused) and str(error) in {"stop_requested", "runtime_updated"}:
                        raise
                    r["history_error"] = str(error)[:2000]
                if r.get("catalog", {}).get("status") != "supported":
                    r["catalog"] = r.get("catalog") or {"status": "unavailable", "sources": [],
                        "reason": r.get("history_error") or r.get("reason") or "Historical interface unavailable"}
                r.update(status="running", reason=None, phase="analysis", deadline_at=time.time()+MAX_SECONDS)
                self.save()
            while r["status"] == "running":
                self.boundary()
                if r.get("pending_decision"):
                    self.apply_decision()
                    continue
                if r.get("pending_read"):
                    self.read_pending()
                    continue
                if r["decisions"] >= MAX_DECISIONS:
                    r.update(status="exhausted", reason="retrospective_call_limit")
                    break
                context = self.context()
                r["decisions"] += 1
                self.save()
                decision = self.think("retrospect", context)
                # Keep the response even if the call crossed its deadline.
                r["pending_decision"] = {"result": RetrospectiveDecision.model_validate(decision).model_dump(),
                    "evidence_ids": [f["id"] for f in context["evidence"]],
                    "recalled_ids": [x["id"] for x in context["recalled_memory"]],
                    "verification_context": {k: context[k] for k in
                        ("recent_events", "historical_pages", "observation", "local_history")}}
                self.save()
                # Commit the already computed response even if its call used the
                # last second. This only writes local memory; another model call
                # or historical read still requires the next budget boundary.
                self.apply_decision()
        except (TimeBudgetExpired, ReviewPaused, KeyboardInterrupt) as error:
            r.update(status="paused", reason=str(error) or "interrupted")
        except Exception as error:
            # A failed review never changes the completed world's result or replays its queue.
            r.update(status="paused", reason="review_error", error=str(error)[:3000])
        finally:
            self.brain.deadline_at = old_deadline
            if (self.world.path / "AGENTS.md").is_file():
                self.save()
            emit("retrospective", local_id=self.state["id"], status=r["status"],
                 reason=r.get("reason"), reads=r["reads"], decisions=r["decisions"],
                 accepted_lessons=r["accepted_lessons"])


def retrospect(world, memory, state, brain, **kwargs):
    Review(world, memory, state, brain, **kwargs).run()
