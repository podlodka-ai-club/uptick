import ast
from pathlib import Path

MEMORY_ROOT = Path(__file__).parents[1] / "src" / "uptick_agent" / "memory"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_stage_one_memory_boundary_does_not_import_simulator_or_provider_implementations() -> None:
    files = [
        MEMORY_ROOT / "contracts.py",
        MEMORY_ROOT / "config.py",
        *sorted((MEMORY_ROOT / "stores").glob("*.py")),
        *sorted((MEMORY_ROOT / "compatibility").glob("*.py")),
    ]
    forbidden_prefixes = ("uptick_agent.simulator", "uptick_agent.llm", "openai", "openai_codex")

    violations = {
        str(path.relative_to(MEMORY_ROOT)): sorted(
            imported for imported in _imports(path) if imported.startswith(forbidden_prefixes)
        )
        for path in files
    }
    assert violations == {name: [] for name in violations}


def test_structured_stores_do_not_use_jsonl_as_a_backing_store() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (MEMORY_ROOT / "stores").glob("*.py")
    )
    assert "JsonlMemory" not in source
    assert "jsonl" not in source.casefold()
