"""CLI entry points, bounded experiment workers and reproducible comparison."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event

from .agent import DEFAULT_ORIGIN, create_run, emit, result_summary, run_agent
from .brain import Brain
from .memory import Memory, dumps
from .simulator import redact

DEFAULT_MEMORY = Path(__file__).resolve().parents[2] / ".local" / "agent.sqlite3"


def parser():
    p = argparse.ArgumentParser(description="AK Agent — Codex принимает решения, SQLite сохраняет опыт.")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "train", "evaluate", "memory", "ask", "report", "models"):
        s = sub.add_parser(name)
        s.add_argument("--memory", type=Path, default=Path(os.getenv("AK_MEMORY", DEFAULT_MEMORY)))
        if name in ("run", "resume", "train", "evaluate", "ask", "models"):
            s.add_argument("--model", default=os.getenv("CODEX_MODEL", "gpt-5.6-sol"))
            s.add_argument("--effort", choices=("low", "medium", "high"), default="low")
        if name in ("run", "resume", "train", "evaluate"):
            s.add_argument("--max-steps", type=int, default=250, help="Decision budget; resume continues afterwards")
            s.add_argument("--strategy", default="", help="Experiment hint to the LLM, not a scripted policy")
        if name in ("run", "train", "evaluate"):
            s.add_argument("--origin", default=os.getenv("API_ORIGIN", DEFAULT_ORIGIN))
        if name == "run":
            s.add_argument("--seed", required=True, type=int)
            s.add_argument("--no-memory", action="store_true", help="Disable episodic recall and learning; checkpoint still persists")
        if name == "resume":
            s.add_argument("run_id", nargs="?", help="Local ID or simulator run_id; defaults to latest")
        if name in ("train", "evaluate"):
            s.add_argument("--seeds", required=True, help="Comma-separated positive seeds")
            s.add_argument("--jobs", type=int, default=1)
            s.add_argument("--models", help="Comma-separated model comparison; defaults to --model")
        if name == "memory":
            s.add_argument("--json", action="store_true")
            s.add_argument("--maintain", action="store_true", help="One consolidation/forgetting cycle")
        if name == "ask":
            s.add_argument("question", nargs="?", default="Что ты запомнил, на каком опыте и как это изменило твои действия?")
        if name == "report":
            s.add_argument("--json", action="store_true")
    return p


def worker(path, seed, origin, model, effort, max_steps, strategy, experiment, recall=True, learn=True, stop=None):
    if stop is not None and stop.is_set():
        return {"seed":seed,"model":model,"status":"cancelled","memory_enabled":recall}
    memory = Memory(path)
    brain = None
    state = None
    try:
        # Authenticate and initialize model runtime before starting the simulation clock.
        brain = Brain(model, effort)
        if stop is not None and stop.is_set():
            return {"seed":seed,"model":model,"status":"cancelled","memory_enabled":recall}
        state = create_run(memory, seed, origin, model, experiment, recall, learn)
        state = run_agent(memory, state, brain, max_steps, strategy, stop)
        return result_summary(state)
    except Exception as e:
        emit("worker_error", seed=seed, model=model, error=str(e), local_id=state.get("local_id") if state else None)
        return {**(result_summary(state) if state else {}), "seed": seed, "model": model, "error": str(e), "memory_enabled": recall,
                "local_id": state.get("local_id") if state else None}
    finally:
        if brain:
            brain.close()
        memory.close()


def experiments(args, memory):
    seeds = [int(s) for s in args.seeds.split(",")]
    if not seeds or any(s <= 0 for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Seeds must be distinct positive integers")
    if args.jobs < 1 or args.jobs > 8:
        raise ValueError("jobs must be 1..8")
    models = args.models.split(",") if args.models else [args.model]
    experiment = args.command + "-" + str(time.time_ns())
    folder = memory.path.parent / experiment
    folder.mkdir()
    tasks = []
    if args.command == "evaluate":
        trained_seeds = {s["seed"] for s in memory.runs() if s.get("learn")} | {
            r[0] for r in memory.db.execute("SELECT seed FROM training_seeds")}
        if set(seeds) & trained_seeds:
            raise ValueError("Use held-out seeds, absent from training: " + str(sorted(trained_seeds)))
        # Freeze lessons BEFORE any worker starts. Each arm has an isolated notebook.
        frozen = memory.lesson_rows()
        for model in models:
            for seed in seeds:
                for enabled in (False, True):
                    path = folder / f"{model}-{seed}-{'memory' if enabled else 'baseline'}.sqlite3"
                    target = Memory(path)
                    if enabled:
                        with target.db:
                            for x in frozen:
                                target.db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?)", (
                                    x["id"],x["title"],x["body"],dumps(x["tags"]),dumps(x["procedure"]),
                                    x["support"],x["against"],x["importance"],x["touched"],0))
                                for e in x["evidence"]:
                                    target.db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)", (
                                        x["id"],e["event"],e["run"],e["verdict"],e["explanation"]))
                        target.put("epoch", memory.get("epoch", 0))
                    target.close()
                    tasks.append((path, seed, model, enabled, False))
    else:
        for model in models:
            for seed in seeds:
                tasks.append((memory.path, seed, model, True, True))
    emit("experiment", id=experiment, runs=len(tasks), jobs=args.jobs, report=str(folder / "results.json"))
    results = []
    stop = Event()
    def collect(future):
        results.append(future.result())
        temp = folder / "results.tmp"
        temp.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        temp.replace(folder / "results.json")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(worker, path, seed, args.origin, model, args.effort,
                    args.max_steps, args.strategy, experiment, enabled, learn, stop)
                   for path,seed,model,enabled,learn in tasks]
        try:
            for future in as_completed(futures):
                collect(future)
        except KeyboardInterrupt:
            stop.set()
            emit("pausing", reason="Ctrl+C; current calls finish, remaining actions are checkpointed")
            # Rebuild from every future, including any interrupted report write.
            results.clear()
            for future in as_completed(futures):
                collect(future)
    print_report(results)
    return 130 if stop.is_set() else 1 if any(r.get("error") for r in results) else 0


def print_report(rows):
    print("seed  model                 memory  status     SLO  uptime       cost_minor   decisions  LLM sec  run_id")
    for r in rows:
        uptime = f"{100*r['uptime']:.5f}%" if r.get("uptime") is not None else "—"
        slo = "yes" if r.get("slo_passed") is True else "no" if r.get("slo_passed") is False else "—"
        status = r.get("runner_status") if r.get("runner_status") in ("blocked", "error", "paused") else r.get("status", "error")
        print(f"{r['seed']:<5} {r['model']:<21} {str(r.get('memory_enabled')):<7} "
              f"{status:<10} {slo:<4} {uptime:<12} {str(r.get('cost_minor','—')):<12} "
              f"{r.get('decisions','—')!s:<10} {r.get('llm_seconds','—')}  {r.get('run_id') or r.get('local_id','—')}")


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv[1:]
    supplied = {arg.split("=", 1)[0] for arg in argv}
    args = parser().parse_args(argv)
    if getattr(args, "max_steps", 1) < 1:
        raise ValueError("max-steps must be positive")
    memory = Memory(args.memory)
    brain = None
    try:
        if args.command == "memory":
            if args.maintain:
                memory.maintain()
            snap = memory.snapshot()
            if args.json:
                print(json.dumps(snap, ensure_ascii=False, indent=2))
            else:
                print(f"Память: {memory.path}\nУроков: {len(snap['lessons'])}; забыто: {snap['forgetting'].get('total',0)}")
                for x in snap["lessons"]:
                    print(f"\n[{x['id']}] {x['title']} ({x['level']})\n{x['body']}\n"
                          f"Подтверждений: {x['support']}; противоречий: {x['against']}; "
                          f"применений: {x['uses']}; доверие: {x['confidence']:.0%}; сила: {x['strength']:.2f}")
                    if x["procedure"]:
                        print("Навык: " + " → ".join(x["procedure"]))
                print("\nТеги: " + dumps(snap["tags"]))
            return 0
        if args.command == "report":
            rows = [result_summary(s) for s in memory.runs()]
            print(json.dumps(rows, ensure_ascii=False, indent=2)) if args.json else print_report(rows)
            return 0
        if args.command in ("train", "evaluate"):
            return experiments(args, memory)
        brain = Brain(args.model, args.effort)
        if args.command == "models":
            for model in brain.codex.models().data:
                print(model.model, "—", model.description)
            return 0
        if args.command == "ask":
            states = memory.runs()
            reports = [result_summary(s) for s in states]
            successes = [r for r in reports if r["status"] == "completed" and r["slo_passed"] is True]
            reflections = [{"run_id":s.get("run_id"), **e} for s in states[:10]
                           for e in memory.recent(s["local_id"], 5) if e["kind"] == "reflection"]
            evidence = {**memory.snapshot(), "recent_runs": reports[:10], "successful_runs": successes[:5],
                "completion_reflections": reflections,
                "history_totals": {"stored_runs":len(reports),"slo_passed":len(successes),
                                   "completed":sum(r["status"] == "completed" for r in reports)},
                "working_memory": [{"run_id":s.get("run_id"), "mission":s.get("mission"), "plan":s.get("plan")} for s in states[:3]]}
            print(brain.explain(args.question, evidence))
            return 0
        if args.command == "run":
            state = create_run(memory, args.seed, args.origin, args.model,
                               recall=not args.no_memory, learn=not args.no_memory)
        else:
            if args.run_id:
                matches = [s for s in memory.runs() if args.run_id in (s["local_id"],s.get("run_id"))]
                if not matches:
                    raise ValueError("Run not found in this memory database")
                state = matches[0]
            else:
                state = memory.run()
            # Explicit --model selects the resumption model; otherwise keep original.
            if "--model" not in supplied and not os.getenv("CODEX_MODEL"):
                brain.model = state["model"]
            else:
                state["model"] = args.model
            if "--effort" not in supplied:
                brain.effort = state.get("effort", args.effort)
        run_agent(memory, state, brain, args.max_steps, args.strategy)
        return 0
    finally:
        if brain:
            brain.close()
        memory.close()
