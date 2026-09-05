import ast
import json
import subprocess
import sys
from pathlib import Path

MEMORY_ROOT = Path(__file__).parents[1] / "src" / "uptick_agent" / "memory"
PROJECT_ROOT = MEMORY_ROOT.parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    relative_path = path.relative_to(MEMORY_ROOT).with_suffix("")
    module_parts = ("uptick_agent", "memory", *relative_path.parts)
    package_parts = module_parts[:-1]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level == 0:
                imports.add(node.module)
            else:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                imports.add(".".join((*base, *node.module.split("."))))
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = package_parts[: len(package_parts) - (node.level - 1)]
            imports.update(".".join((*base, alias.name)) for alias in node.names)
    return imports


def test_stage_one_memory_boundary_does_not_import_simulator_or_provider_implementations() -> None:
    files = sorted(MEMORY_ROOT.rglob("*.py"))
    forbidden_prefixes = (
        "uptick_agent.simulator",
        "uptick_agent.llm",
        "uptick_agent.integrations.xmemory",
        "openai",
        "openai_codex",
    )

    violations = {
        str(path.relative_to(MEMORY_ROOT)): sorted(
            imported for imported in _imports(path) if imported.startswith(forbidden_prefixes)
        )
        for path in files
    }
    assert violations == {name: [] for name in violations}


def _fresh_process_modules(import_statement: str) -> set[str]:
    probe = f"""
import json
import sys
{import_statement}
print(json.dumps(sorted(name for name in sys.modules if name.startswith('uptick_agent'))))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_contract_import_does_not_eagerly_load_runner_or_memory_implementations() -> None:
    loaded = _fresh_process_modules("import uptick_agent.memory.contracts")

    assert loaded <= {"uptick_agent", "uptick_agent.memory", "uptick_agent.memory.contracts"}


def test_store_contract_import_does_not_eagerly_load_store_implementations() -> None:
    loaded = _fresh_process_modules("import uptick_agent.memory.stores.contracts")

    assert loaded <= {
        "uptick_agent",
        "uptick_agent.memory",
        "uptick_agent.memory.contracts",
        "uptick_agent.memory.stores",
        "uptick_agent.memory.stores.contracts",
        "uptick_agent.redaction",
    }


def test_configuration_import_does_not_load_concrete_memory_modules() -> None:
    loaded = _fresh_process_modules("import uptick_agent.memory.config")
    concrete = {
        "uptick_agent.memory.audit",
        "uptick_agent.memory.consolidation",
        "uptick_agent.memory.episodic",
        "uptick_agent.memory.in_memory",
        "uptick_agent.memory.jsonl",
        "uptick_agent.memory.lesson_runtime",
        "uptick_agent.memory.lessons",
        "uptick_agent.memory.orchestrator",
        "uptick_agent.memory.patterns",
        "uptick_agent.memory.playbooks",
        "uptick_agent.memory.tool_knowledge",
        "uptick_agent.memory.world_model",
        "uptick_agent.memory.stores.in_memory",
        "uptick_agent.memory.stores.sqlite",
    }

    assert loaded.isdisjoint(concrete)
    assert "uptick_agent.runner" not in loaded
    assert "uptick_agent.models" not in loaded


def test_structured_stores_do_not_use_jsonl_as_a_backing_store() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (MEMORY_ROOT / "stores").glob("*.py")
    )
    assert "JsonlMemory" not in source
    assert "jsonl" not in source.casefold()
