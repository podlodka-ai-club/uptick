from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from contextlib import suppress
from pathlib import Path
from openai_codex import CodexError

from .agent import create_run, run_agent, reflect_run, summary
from .brain import Brain
from .experiments import experiments
from .files import ROOT, dumps, emit, inside, tree
from .memory import Memory
from .worlds import discover, register, select

COMMANDS = {"run", "resume", "reflect", "adapt", "train", "evaluate", "memory", "memory-history", "ask", "report", "models", "worlds", "add-world", "stream"}


def positive_seconds(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected positive integer seconds")
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected positive integer seconds")
    return number


def parser():
    p = argparse.ArgumentParser(description="AK2 — универсальный агент, знания и опыт отдельно для каждого мира")
    sub = p.add_subparsers(dest="command", required=True)
    for name in sorted(COMMANDS):
        s = sub.add_parser(name)
        s.add_argument("--root", type=Path, default=ROOT)
        if name == "add-world":
            s.add_argument("world_id")
            s.add_argument("--description", required=True)
            s.add_argument("--bootstrap", required=True)
            s.add_argument("--replay-safe", action="store_true")
            continue
        s.add_argument("--world")
        s.add_argument("--memory", type=Path)
        s.add_argument("--model", default=os.getenv("CODEX_MODEL", "gpt-5.6-sol"))
        s.add_argument("--effort", choices=("low", "medium", "high"), default="low")
        s.add_argument("--json", action="store_true")
        if name in {"run", "adapt", "train", "evaluate", "resume", "stream"}:
            s.add_argument("--task", default="")
            s.add_argument("--max-steps", type=int, default=250)
            s.add_argument("--time-budget-seconds", type=positive_seconds, default=None,
                           help="Execution budget, excluding final review; resume with this flag starts a new execution budget")
            s.add_argument("--strategy", default="")
        if name in {"run", "adapt", "train", "evaluate", "stream"}:
            s.add_argument("--origin")
            s.add_argument("--options", default="{}", help="World-specific JSON parameters")
        if name in {"run", "adapt"}:
            s.add_argument("task_text", nargs="?")
            s.add_argument("--seed", type=int, default=42)
            s.add_argument("--no-memory", action="store_true")
        if name in {"resume", "reflect"}:
            s.add_argument("run_id", nargs="?")
        if name in {"train", "evaluate"}:
            s.add_argument("--seeds", required=True)
            s.add_argument("--jobs", type=int, default=1)
            s.add_argument("--models")
        if name == "memory":
            s.add_argument("--maintain", action="store_true")
            s.add_argument("--knowledge", action="store_true")
        if name == "memory-history":
            s.add_argument("--lesson")
            s.add_argument("--run-id")
            s.add_argument("--change", choices=("added", "reinforced", "contradicted", "rejected", "deleted", "snapshot", "imported"))
            s.add_argument("--after-id", type=int, default=0)
            s.add_argument("--limit", type=int, default=50)
        if name == "ask":
            s.add_argument("question", nargs="?", default="Что ты запомнил, почему и как изменил поведение?")
        if name == "stream":
            s.add_argument("source", type=Path, help="JSONL: task, seed, optional world/options/no_memory")
    return p


def memory_path(world, supplied):
    if supplied is None:
        return world.memory
    # Even experiment notebooks must remain inside the world being queried.
    absolute = supplied.resolve() if supplied.is_absolute() else (Path.cwd() / supplied).resolve()
    if not absolute.is_relative_to(world.local.resolve()):
        raise ValueError("--memory must be inside selected world's .local directory")
    inside(world.local, absolute.relative_to(world.local.resolve()))
    return absolute


def print_memory(snap, world, knowledge=False):
    print(f"Мир: {world.id}; уроков: {len(snap['lessons'])}; забыто: {snap['forgetting'].get('total', 0)}")
    for x in snap["lessons"]:
        print(f"\n[{x['id']}] {x['title']} ({x['level']})\n{x['body']}\n"
              f"Подтверждений: {x['support']}; противоречий: {x['against']}; применений: {x['uses']}; сила: {x['strength']}")
        if x.get("legacy_support") or x.get("legacy_against"):
            print(f"История до привязки к формулировке: +{x['legacy_support']} / -{x['legacy_against']}; "
                  "в текущую уверенность не входит.")
        if x["procedure"]:
            print("Навык: " + " → ".join(x["procedure"]))
    print("Теги: " + dumps(snap["tags"]))
    if knowledge:
        print("Знание мира: " + dumps(sorted(tree(world.knowledge))))


def stream(args):
    # Stable source+line identities make restarting the same stream resume its pending run.
    import hashlib
    source = args.source.resolve()
    for line_number, text in enumerate(source.read_text().splitlines(), 1):
        if not text.strip():
            continue
        task = json.loads(text)
        if not isinstance(task, dict) or not isinstance(task.get("task"), str):
            raise ValueError(f"Invalid task at line {line_number}")
        brain = Brain(args.model, args.effort)
        memory = None
        try:
            world = select(args.root, task.get("world"), task["task"], brain)
            memory = Memory(world.memory, world_local=world.local)
            key = "stream-" + hashlib.sha256((str(source)+":"+str(line_number)).encode()).hexdigest()
            previous = memory.get(key)
            task_hash = hashlib.sha256(dumps(task).encode()).hexdigest()
            if previous:
                if previous["hash"] != task_hash:
                    raise ValueError("Stream line changed after execution; use a new source file")
                state = memory.run(previous["run"])
            else:
                enabled = not task.get("no_memory", False)
                state = create_run(world, memory, task.get("seed", 42), args.model, args.effort,
                    task=task["task"], options=task.get("options", {}), recall=enabled, learn=enabled,
                    time_budget_seconds=task.get("time_budget_seconds", getattr(args, "time_budget_seconds", None)))
                memory.put(key, {"hash": task_hash, "run": state["id"]})
            run_agent(world, memory, state, brain, max_steps=args.max_steps, strategy=args.strategy)
            if state["status"] != "finished":
                return 0  # Preserve order; the next call continues this task first.
        finally:
            brain.close()
            if memory:
                memory.close()
    return 0


def execute(argv):
    args = parser().parse_args(argv)
    if getattr(args, "max_steps", 1) < 1:
        raise ValueError("max-steps must be positive")
    if args.command == "add-world":
        print(register(args.root, args.world_id, args.description, args.bootstrap, args.replay_safe))
        return 0
    if args.command == "worlds":
        print(json.dumps([w.descriptor() | {"adapted": w.ready()} for w in discover(args.root)], ensure_ascii=False, indent=2))
        return 0
    if args.command == "stream":
        return stream(args)
    if args.command == "models":
        brain = Brain(args.model, args.effort)
        try:
            for model in brain.codex.models().data:
                print(model.model, "—", model.description)
        finally:
            brain.close()
        return 0
    if hasattr(args, "options"):
        args.options = json.loads(args.options)
        if not isinstance(args.options, dict):
            raise ValueError("options must be a JSON object")
        if args.origin:
            args.options["origin"] = args.origin
        args.task = getattr(args, "task_text", None) or args.task
    brain, memory = None, None
    try:
        if getattr(args, "task", "") and not args.world:
            brain = Brain(args.model, args.effort)
        world = select(args.root, args.world, getattr(args, "task", ""), brain)
        memory = Memory(memory_path(world, args.memory), world_local=world.local)
        if args.command == "memory-history":
            page = memory.lesson_history(after_id=args.after_id, limit=args.limit, lesson=args.lesson,
                                         run=args.run_id, change=args.change)
            if args.json:
                print(json.dumps(page, ensure_ascii=False, indent=2))
            else:
                for entry in page["entries"]:
                    item = entry["after"] or entry["before"] or entry["details"].get("proposal", {})
                    print(f"[{entry['id']}] {entry['created_at']} {entry['change']} {entry['lesson']} "
                          f"run={entry['run'] or '—'}\nПричина: {entry['reason']}\n{item.get('body', '')}")
                    for label in ("before", "after"):
                        value = entry[label]
                        if value is not None:
                            print(f"{label}: +{value['support']} / -{value['against']}; confidence={value['confidence']:.4f}")
                    detail = entry["details"]
                    explanation = (detail.get("feedback", {}).get("review", {}).get("reason") or
                                   detail.get("feedback", {}).get("detail") or
                                   detail.get("proposal", {}).get("explanation"))
                    if explanation:
                        print("Пояснение: " + explanation)
                if not page["entries"]:
                    print("Подходящих записей нет.")
                if page["next_after_id"] is not None:
                    print(f"Следующая страница: --after-id {page['next_after_id']} (с теми же фильтрами)")
            return 0
        if args.command == "memory":
            if args.maintain:
                memory.maintain()
            snap = memory.snapshot()
            if args.knowledge:
                snap["knowledge"] = tree(world.knowledge)
            print(json.dumps(snap, ensure_ascii=False, indent=2)) if args.json else print_memory(snap, world, args.knowledge)
            return 0
        if args.command == "report":
            print(json.dumps([summary(s) for s in memory.runs()], ensure_ascii=False, indent=2))
            return 0
        if args.command in {"train", "evaluate"}:
            return experiments(world, memory, args)
        state = None
        if args.command in {"resume", "reflect"}:
            state = memory.run(args.run_id)
            if not any(a == "--model" or a.startswith("--model=") for a in argv) and not os.getenv("CODEX_MODEL"):
                args.model = state["model"]
            if not any(a == "--effort" or a.startswith("--effort=") for a in argv):
                args.effort = state["effort"]
            state.update(model=args.model, effort=args.effort)
        if brain:
            brain.close()
        brain = Brain(args.model, args.effort, scratch=world.local / "brain")
        if args.command == "reflect":
            reflect_run(world, memory, state, brain)
            return 0
        if args.command == "ask":
            states = memory.runs()
            print(brain.explain(args.question, {**memory.snapshot(), "world": world.descriptor(),
                "knowledge": {p: c for p, c in tree(world.knowledge).items() if not p.startswith("scripts/")},
                "recent_runs": [summary(s) for s in states[:10]],
                "reflections": [e for s in states[:5] for e in memory.recent(s["id"], 10)
                                if e["kind"] in {"reflection", "failure_reflection", "retrospective_decision"}],
                "retrospectives": [{"run": s["id"], **s["retrospective"]}
                                   for s in states[:5] if "retrospective" in s]}))
            return 0
        if state is None:
            enabled = not args.no_memory and args.command != "adapt"
            state = create_run(world, memory, args.seed, args.model, args.effort, task=args.task,
                options=args.options, recall=enabled, learn=enabled, time_budget_seconds=args.time_budget_seconds)
        run_agent(world, memory, state, brain, max_steps=args.max_steps, strategy=args.strategy, adapt_only=args.command == "adapt",
                  time_budget_seconds=args.time_budget_seconds if args.command == "resume" else None)
        return 0
    finally:
        if brain:
            brain.close()
        if memory:
            memory.close()


def legacy(argv):
    p = argparse.ArgumentParser(description="Вход в Codex и вопросы к опыту выбранного мира")
    p.add_argument("question", nargs="*")
    p.add_argument("--world")
    p.add_argument("--model", default=os.getenv("CODEX_MODEL", "gpt-5.6-sol"))
    group = p.add_mutually_exclusive_group()
    group.add_argument("--login", action="store_true")
    group.add_argument("--check-auth", action="store_true")
    args = p.parse_args(argv)
    if args.login or args.check_auth:
        brain = Brain(args.model, authenticate=not args.login)
        try:
            if args.login:
                handle = brain.codex.login_chatgpt()
                try:
                    print(handle.auth_url)
                    webbrowser.open(handle.auth_url)
                    result = handle.wait()
                except BaseException:
                    with suppress(Exception):
                        handle.cancel()
                    raise
                if not result.success:
                    raise RuntimeError(result.error or "Login failed")
                brain.check_auth()
            print("Авторизация: ChatGPT / Codex по подписке")
        finally:
            brain.close()
        return 0
    suffix = ["--model", args.model] + (["--world", args.world] if args.world else [])
    if args.question:
        return execute(["ask", " ".join(args.question), *suffix])
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        return execute(["ask", text, *suffix]) if text else 0
    while True:
        try:
            text = input("\nВы (/exit): ").strip()
        except EOFError:
            return 0
        if text in {"/exit", "/quit"}:
            return 0
        if text and text != "/new":
            execute(["ask", text, *suffix])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in {"-h", "--help"}:
            parser().print_help()
            return 0
        return execute(argv) if argv and argv[0] in COMMANDS else legacy(argv)
    except KeyboardInterrupt:
        print("\nОстановлено; продолжить: ./run.sh resume --world WORLD", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, CodexError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
