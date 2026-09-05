import ast
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

MEMORY_ROOT = Path(__file__).parents[1] / "src" / "uptick_agent" / "memory"
PACKAGE_ROOT = MEMORY_ROOT.parent
PROJECT_ROOT = MEMORY_ROOT.parents[2]
SCHEMA_BASELINE = Path(__file__).parent / "fixtures" / "historical_schema_fingerprints.json"

# Generic layers may name only these stable memory contracts.  Every other
# module under ``uptick_agent.memory`` is an implementation reachable through
# explicit composition, so a newly added implementation fails this check by
# default instead of requiring another deny-list entry.
_GENERIC_MEMORY_CONTRACTS = frozenset(
    {
        "uptick_agent.memory.audit_contracts",
        "uptick_agent.memory.compatibility.contracts",
        "uptick_agent.memory.contracts",
    }
)
_RETRIEVAL_MEMORY_PORTS = _GENERIC_MEMORY_CONTRACTS | {
    "uptick_agent.memory.retrieval_ports",
}
_XMEMORY_BRIDGE_MODULES = _GENERIC_MEMORY_CONTRACTS | {
    "uptick_agent.memory.config",
    "uptick_agent.memory.orchestrator",
    "uptick_agent.memory.stores.contracts",
}


def _module_name(path: Path) -> str:
    relative_path = path.relative_to(PACKAGE_ROOT).with_suffix("")
    if relative_path.name == "__init__":
        relative_path = relative_path.parent
    return ".".join(("uptick_agent", *relative_path.parts))


def _module_exists(module_name: str) -> bool:
    """Return whether a project module exists for from-import alias expansion."""

    if module_name == "uptick_agent":
        return (PACKAGE_ROOT / "__init__.py").is_file()
    if not module_name.startswith("uptick_agent."):
        return False
    relative = PACKAGE_ROOT.joinpath(*module_name.split(".")[1:])
    return relative.with_suffix(".py").is_file() or (relative / "__init__.py").is_file()


def _resolved_from_imports(node: ast.ImportFrom, package_parts: list[str]) -> set[str]:
    """Resolve a from-import, including aliases that name project submodules."""

    if node.module:
        if node.level == 0:
            module_name = node.module
        else:
            base = package_parts[: len(package_parts) - (node.level - 1)]
            module_name = ".".join((*base, *node.module.split(".")))
        imports = {module_name}
        if _module_exists(module_name):
            imports.update(
                f"{module_name}.{alias.name}"
                for alias in node.names
                if alias.name != "*" and _module_exists(f"{module_name}.{alias.name}")
            )
        return imports
    if node.level:
        base = package_parts[: len(package_parts) - (node.level - 1)]
        return {
            ".".join((*base, alias.name))
            for alias in node.names
            if alias.name != "*"
        }
    return set()


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_parts = _module_name(path).split(".")
    package_parts = module_parts if path.name == "__init__.py" else module_parts[:-1]
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(_resolved_from_imports(node, package_parts))
    return imports


def _runtime_imports(path: Path) -> set[str]:
    """Resolve imports used at runtime, excluding TYPE_CHECKING-only edges."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_parts = _module_name(path).split(".")
    package_parts = module_parts if path.name == "__init__.py" else module_parts[:-1]
    imports: set[str] = set()

    def add(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(_resolved_from_imports(node, package_parts))

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                for child in node.orelse:
                    self.visit(child)
                return
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            add(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            add(node)

    Visitor().visit(tree)
    return imports


def test_package_import_graph_has_no_cycles() -> None:
    """Keep store/contracts and module implementations in one direction."""

    paths = tuple(sorted(PACKAGE_ROOT.rglob("*.py")))
    modules = {_module_name(path): path for path in paths}
    graph = {
        module: sorted(imported for imported in _runtime_imports(path) if imported in modules)
        for module, path in modules.items()
    }
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        state[module] = 1
        stack.append(module)
        for imported in graph[module]:
            if state.get(imported) == 1:
                start = stack.index(imported)
                cycles.append(tuple((*stack[start:], imported)))
            elif state.get(imported, 0) == 0:
                visit(imported)
        stack.pop()
        state[module] = 2

    for module in sorted(graph):
        if state.get(module, 0) == 0:
            visit(module)

    assert cycles == []


def test_stage_one_memory_boundary_does_not_import_simulator_or_provider_implementations() -> None:
    files = sorted(MEMORY_ROOT.rglob("*.py"))
    forbidden_prefixes = (
        "uptick_agent.simulator",
        "uptick_agent.llm",
        "uptick_agent.environment",
        "uptick_agent.integrations",
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


def test_canonical_runtime_layers_do_not_import_concrete_adapters_or_memory_modules() -> None:
    """Keep the generic execution path independent of optional implementations."""

    generic_boundary_files = (
        PACKAGE_ROOT / "runs" / "execute.py",
        PACKAGE_ROOT / "ports.py",
        PACKAGE_ROOT / "decisions" / "runtime.py",
        PACKAGE_ROOT / "llm" / "contracts.py",
        PACKAGE_ROOT / "llm" / "registry.py",
        PACKAGE_ROOT / "llm" / "decision_model.py",
        PACKAGE_ROOT / "environment" / "contracts.py",
        PACKAGE_ROOT / "environment" / "prestarted.py",
    )

    violations = _forbidden_imports(
        generic_boundary_files,
        (
            "uptick_agent.memory",
            "uptick_agent.simulator",
            "uptick_agent.llm.openai",
            "uptick_agent.llm.codex",
        ),
        allowed=_allowlisted_edges(generic_boundary_files, _GENERIC_MEMORY_CONTRACTS),
    )
    assert violations == {name: [] for name in violations}


def _forbidden_imports(
    paths: tuple[Path, ...],
    forbidden: tuple[str, ...],
    *,
    allowed: set[tuple[Path, str]] | None = None,
) -> dict[str, list[str]]:
    allowed = allowed or set()
    return {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            imported
            for imported in _imports(path)
            if any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in forbidden
            )
            and (path, imported) not in allowed
        )
        for path in paths
    }


def _allowlisted_edges(
    paths: tuple[Path, ...], modules: frozenset[str] | set[str]
) -> set[tuple[Path, str]]:
    """Build exact file/module edges accepted at a layer boundary."""

    return {(path, module) for path in paths for module in modules}


def test_all_non_composition_layers_keep_their_dependency_direction() -> None:
    """Check every source file in the stable execution and adapter layers."""

    provider_adapters = ("uptick_agent.llm.openai", "uptick_agent.llm.codex")
    stable_runtime_files = tuple(
        path
        for directory in (PACKAGE_ROOT / "runs", PACKAGE_ROOT / "environment")
        for path in sorted(directory.rglob("*.py"))
    ) + (
        PACKAGE_ROOT / "decisions" / "runtime.py",
        PACKAGE_ROOT / "decisions" / "instructions.py",
    )
    llm_files = tuple(sorted((PACKAGE_ROOT / "llm").rglob("*.py")))
    simulator_files = tuple(sorted((PACKAGE_ROOT / "simulator").rglob("*.py")))
    retrieval_integration_files = tuple(
        sorted((PACKAGE_ROOT / "integrations" / "retrieval").rglob("*.py"))
    )
    xmemory_integration_files = tuple(
        sorted((PACKAGE_ROOT / "integrations" / "xmemory").rglob("*.py"))
    )

    violations = {}
    violations.update(
        _forbidden_imports(
            stable_runtime_files,
            ("uptick_agent.memory", *provider_adapters, "uptick_agent.simulator"),
            allowed=_allowlisted_edges(stable_runtime_files, _GENERIC_MEMORY_CONTRACTS),
        )
    )
    violations.update(
        _forbidden_imports(
            llm_files,
            (
                "uptick_agent.memory",
                "uptick_agent.runner",
                "uptick_agent.runs",
                "uptick_agent.ports",
                "uptick_agent.simulator",
            ),
            # The old prompt module is a compatibility facade.  It resolves
            # the historical v2 briefing only when that legacy name is used.
            allowed={
                (PACKAGE_ROOT / "llm" / "prompts.py", "uptick_agent.simulator"),
                (PACKAGE_ROOT / "llm" / "prompts.py", "uptick_agent.simulator.briefings"),
            },
        )
    )
    violations.update(
        _forbidden_imports(
            simulator_files,
            (
                "uptick_agent.memory",
                "uptick_agent.llm",
                "uptick_agent.runner",
                "openai",
                "openai_codex",
            ),
            allowed=_allowlisted_edges(simulator_files, _GENERIC_MEMORY_CONTRACTS),
        )
    )
    violations.update(
        _forbidden_imports(
            retrieval_integration_files,
            (
                "uptick_agent.simulator",
                "uptick_agent.llm.openai",
                "uptick_agent.llm.codex",
                "uptick_agent.runner",
                "uptick_agent.memory",
                "openai",
                "openai_codex",
            ),
            allowed=_allowlisted_edges(retrieval_integration_files, _RETRIEVAL_MEMORY_PORTS),
        )
    )
    violations.update(
        _forbidden_imports(
            xmemory_integration_files,
            (
                "uptick_agent.memory",
                "uptick_agent.simulator",
                "uptick_agent.llm.openai",
                "uptick_agent.llm.codex",
                "uptick_agent.runner",
                "openai",
                "openai_codex",
            ),
            # xMemory is an outer integration and may register itself with
            # the orchestrator; its bridge is explicit and exact.
            allowed=_allowlisted_edges(xmemory_integration_files, _XMEMORY_BRIDGE_MODULES),
        )
    )

    assert violations == {name: [] for name in violations}

    sdk_violations = _forbidden_imports(
        tuple(sorted(PACKAGE_ROOT.rglob("*.py"))),
        ("openai", "openai_codex"),
        allowed={
            (PACKAGE_ROOT / "llm" / "openai.py", "openai"),
            (PACKAGE_ROOT / "llm" / "codex.py", "openai_codex"),
        },
    )
    assert sdk_violations == {name: [] for name in sdk_violations}


def test_non_composition_memory_boundary_rejects_new_implementation_modules() -> None:
    """A new memory implementation cannot become a hidden generic dependency."""

    execute = PACKAGE_ROOT / "runs" / "execute.py"
    bypass_import = ast.parse(
        "from uptick_agent.memory import deletion, future_module"
    ).body[0]
    assert isinstance(bypass_import, ast.ImportFrom)
    imports_after_unreviewed_change = _imports(execute) | _resolved_from_imports(
        bypass_import, ["uptick_agent", "runs", "execute"]
    ) | {"uptick_agent.memory.future_module"}
    violations = sorted(
        imported
        for imported in imports_after_unreviewed_change
        if imported.startswith("uptick_agent.memory.")
        and imported not in _GENERIC_MEMORY_CONTRACTS
    )

    assert violations == [
        "uptick_agent.memory.deletion",
        "uptick_agent.memory.future_module",
    ]


def test_relative_imports_are_resolved_to_their_real_package() -> None:
    """A relative import must not evade the dependency fitness check."""

    assert "uptick_agent.memory.candidate_validation" in _imports(
        MEMORY_ROOT / "patterns.py"
    )
    assert "uptick_agent.decisions.runtime" in _imports(
        PACKAGE_ROOT / "decisions" / "contracts.py"
    )
    assert "uptick_agent.environment.contracts" in _imports(
        PACKAGE_ROOT / "environment" / "prestarted.py"
    )


def test_memory_contract_import_is_transitively_adapter_free() -> None:
    """Importing a stable contract must not import optional package trees."""

    loaded = _fresh_process_modules("from uptick_agent.memory import ContextItem")
    forbidden_prefixes = (
        "uptick_agent.runner",
        "uptick_agent.runs.execute",
        "uptick_agent.simulator",
        "uptick_agent.llm",
        "uptick_agent.integrations.xmemory",
        "uptick_agent.memory.audit",
        "uptick_agent.memory.consolidation",
        "uptick_agent.memory.episodic",
        "uptick_agent.memory.lessons",
        "uptick_agent.memory.orchestrator",
        "uptick_agent.memory.stores.in_memory",
        "uptick_agent.memory.stores.sqlite",
    )

    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in loaded
        for prefix in forbidden_prefixes
    )


def test_optional_package_trees_are_lazy_from_all_stable_facades() -> None:
    stable_imports = (
        "import uptick_agent.memory",
        "import uptick_agent.memory.config",
        "import uptick_agent.llm",
        "import uptick_agent.llm.contracts",
        "import uptick_agent.environment",
        "import uptick_agent.integrations",
        "import uptick_agent.simulator",
    )
    optional_prefixes = (
        "uptick_agent.memory.audit",
        "uptick_agent.memory.consolidation",
        "uptick_agent.memory.episodic",
        "uptick_agent.memory.lessons",
        "uptick_agent.memory.orchestrator",
        "uptick_agent.memory.stores.in_memory",
        "uptick_agent.memory.stores.sqlite",
        "uptick_agent.llm.openai",
        "uptick_agent.llm.codex",
        "uptick_agent.simulator.client",
        "uptick_agent.simulator.environment",
        "uptick_agent.integrations.xmemory",
        "uptick_agent.integrations.retrieval.fastembed",
        "uptick_agent.integrations.retrieval.reasoned_query",
    )

    for statement in stable_imports:
        loaded = _fresh_process_modules(statement)
        assert not any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in loaded
            for prefix in optional_prefixes
        ), statement


def test_historical_contract_schemas_and_qualified_names_are_unchanged() -> None:
    """The architecture hardening must not rewrite persisted model identities."""

    baseline = json.loads(SCHEMA_BASELINE.read_text(encoding="utf-8"))
    assert len(baseline) == 56

    for qualified_name, expected in baseline.items():
        module_name, class_name = qualified_name.split(":", maxsplit=1)
        model = getattr(importlib.import_module(module_name), class_name)
        assert model.__module__ == expected["module"]
        assert model.__qualname__ == expected["qualname"]
        schema = json.dumps(
            model.model_json_schema(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        assert hashlib.sha256(schema.encode("utf-8")).hexdigest() == expected["schema_sha256"]


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
