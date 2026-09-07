from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event

from .agent import create_run, run_agent, summary
from .brain import Brain
from .files import atomic, dumps, emit, lock
from .memory import Memory
from .worlds import snapshot


def worker(world, path, seed, model, args, *, recall=True, learn=True, experiment="", source=None, stop=None):
    if stop and stop.is_set():
        return {"world": world.id, "seed": seed, "model": model, "status": "cancelled"}
    memory, brain, state = Memory(path, world_local=world.local), None, None
    try:
        brain = Brain(model, args.effort, scratch=world.local / "brain")
        state = create_run(world, memory, seed, model, args.effort, task=args.task,
            options=args.options, recall=recall, learn=learn, experiment=experiment, knowledge_source=source, time_budget_seconds=getattr(args, "time_budget_seconds", None))
        run_agent(world, memory, state, brain, max_steps=args.max_steps, strategy=args.strategy, stop=stop)
        return summary(state)
    except Exception as error:
        return {**(summary(state) if state else {}), "world": world.id, "seed": seed,
                "model": model, "status": "error", "error": str(error), "memory_enabled": recall}
    finally:
        if brain:
            brain.close()
        memory.close()


def experiments(world, memory, args):
    seeds = [int(s) for s in args.seeds.split(",")]
    if not seeds or any(s <= 0 for s in seeds) or len(set(seeds)) != len(seeds):
        raise ValueError("Seeds must be distinct positive integers")
    if not 1 <= args.jobs <= 8:
        raise ValueError("jobs must be 1..8")
    models = (args.models or args.model).split(",")
    if any(not m.strip() for m in models):
        raise ValueError("Model names must not be empty")
    if args.command == "evaluate":
        trained = {r[0] for r in memory.db.execute("SELECT seed FROM trained")}
        if set(seeds) & trained:
            raise ValueError("Evaluation requires held-out seeds: " + dumps(sorted(set(seeds) & trained)))
    if not world.ready():
        raise ValueError("Adapt the world first: ./run.sh adapt --world " + world.id + " --seed 42")
    name = args.command + "-" + str(time.time_ns())
    folder = world.local / name
    folder.mkdir()
    with lock(world.local / "adapt.lock"):
        knowledge_hash = snapshot(world.knowledge, folder / "knowledge")
    tasks = []
    if args.command == "evaluate":
        frozen = Memory(folder / "frozen.sqlite3")
        try:
            # Consistent SQLite read transaction while training may be running elsewhere.
            memory.db.execute("BEGIN")
            try:
                memory.freeze_into(frozen)
            finally:
                memory.db.rollback()
            for i, model in enumerate(models):
                for seed in seeds:
                    for enabled in (False, True):
                        path = folder / f"m{i}-{seed}-{'experience' if enabled else 'baseline'}.sqlite3"
                        arm = Memory(path)
                        try:
                            if enabled:
                                frozen.freeze_into(arm)
                        finally:
                            arm.close()
                        tasks.append((path, seed, model, enabled, False))
        finally:
            frozen.close()
    else:
        tasks = [(memory.path, seed, model, True, True) for model in models for seed in seeds]
    metadata = {"id": name, "world": world.id, "knowledge_hash": knowledge_hash,
        "seeds": seeds, "models": models, "jobs": args.jobs, "max_steps": args.max_steps,
        "time_budget_seconds": getattr(args, "time_budget_seconds", None),
        "experience_frozen": args.command == "evaluate", "learning": args.command == "train"}
    atomic(folder / "experiment.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    emit("experiment", **metadata, report=str(folder / "results.json"))
    stop, results = Event(), []
    def collect(future):
        results.append(future.result())
        atomic(folder / "results.json", json.dumps(results, ensure_ascii=False, indent=2))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(worker, world, path, seed, model, args, recall=recall, learn=learn,
            experiment=name, source=folder / "knowledge", stop=stop) for path, seed, model, recall, learn in tasks]
        try:
            for future in as_completed(futures):
                collect(future)
        except KeyboardInterrupt:
            stop.set()
            emit("pausing", world=world.id, reason="Current calls finish; unfinished queues remain resumable")
            results.clear()
            for future in as_completed(futures):
                collect(future)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 130 if stop.is_set() else int(any(r.get("error") for r in results))
