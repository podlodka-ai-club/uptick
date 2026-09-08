import ast
from pathlib import Path


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_and_runtime_do_not_import_concrete_adapters() -> None:
    package = Path("src/uptick_agent")
    forbidden = (
        "uptick_agent.reasoners",
        "uptick_agent.environments",
        "uptick_agent.memory",
        "uptick_agent.store",
        "uptick_agent.composition",
    )
    for directory in (package / "core", package / "runtime"):
        for path in directory.glob("*.py"):
            imports = _imports(path)
            assert not any(
                imported.startswith(prefix) for imported in imports for prefix in forbidden
            ), f"{path} imports a concrete adapter: {sorted(imports)}"


def test_config_models_and_loader_do_not_import_concrete_adapters() -> None:
    directory = Path("src/uptick_agent/config")
    forbidden = (
        "uptick_agent.reasoners",
        "uptick_agent.environments",
        "uptick_agent.memory",
        "uptick_agent.store",
        "uptick_agent.composition",
    )
    for path in directory.glob("*.py"):
        imports = _imports(path)
        assert not any(
            imported.startswith(prefix) for imported in imports for prefix in forbidden
        ), f"{path} imports a concrete adapter: {sorted(imports)}"


def test_only_composition_imports_multiple_concrete_adapter_families() -> None:
    package = Path("src/uptick_agent")
    families = (
        "uptick_agent.reasoners",
        "uptick_agent.environments",
        "uptick_agent.memory",
        "uptick_agent.store",
    )
    for path in package.rglob("*.py"):
        if path.name == "composition.py":
            continue
        imports = _imports(path)
        used = {
            family
            for family in families
            if any(imported.startswith(family) for imported in imports)
        }
        assert len(used) <= 1, f"{path} composes concrete adapter families: {sorted(used)}"


def test_concrete_adapter_families_do_not_import_each_other() -> None:
    package = Path("src/uptick_agent")
    families = {
        "reasoners": "uptick_agent.reasoners",
        "environments": "uptick_agent.environments",
        "memory": "uptick_agent.memory",
        "store": "uptick_agent.store",
    }
    for directory_name, own_family in families.items():
        forbidden = set(families.values()) - {own_family}
        for path in (package / directory_name).rglob("*.py"):
            imports = _imports(path)
            assert not any(
                imported.startswith(prefix) for imported in imports for prefix in forbidden
            ), f"{path} imports another concrete adapter family: {sorted(imports)}"


def test_uptick_transport_is_part_of_the_environment_adapter() -> None:
    package = Path("src/uptick_agent")

    assert not (package / "simulator").exists()
    assert not (package / "environments" / "uptick").exists()
    assert (package / "environments" / "uptickv2" / "__init__.py").is_file()
    assert (package / "environments" / "discovered" / "transport.py").is_file()


def test_learning_pipeline_depends_on_ports_not_concrete_adapters() -> None:
    directory = Path("src/uptick_agent/learning")
    forbidden = (
        "uptick_agent.reasoners",
        "uptick_agent.environments",
        "uptick_agent.memory",
        "uptick_agent.store",
        "uptick_agent.composition",
    )
    for path in directory.glob("*.py"):
        imports = _imports(path)
        assert not any(
            imported.startswith(prefix) for imported in imports for prefix in forbidden
        ), f"{path} imports a concrete adapter: {sorted(imports)}"
