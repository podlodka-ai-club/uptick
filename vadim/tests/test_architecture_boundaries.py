from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"


def _fresh_process(script: str) -> None:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(item for item in (str(SRC), existing) if item)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_models_facade_loads_contract_families_on_demand() -> None:
    _fresh_process(
        """
import sys
import uptick_agent.models

assert 'uptick_agent.decisions.actions' not in sys.modules
assert 'uptick_agent.decisions.contracts' not in sys.modules
assert 'uptick_agent.runs.results' not in sys.modules
assert 'uptick_agent.memory.compatibility.contracts' not in sys.modules
from uptick_agent.models import NextStep
assert NextStep.__module__ == 'uptick_agent.models'
assert 'uptick_agent.decisions.contracts' in sys.modules
"""
    )


def test_evaluation_and_memory_compatibility_facades_are_lazy() -> None:
    _fresh_process(
        """
import sys
import uptick_agent.evaluation
import uptick_agent.memory.compatibility

assert 'uptick_agent.evaluation.contracts' not in sys.modules
assert 'uptick_agent.memory.compatibility.legacy' not in sys.modules
from uptick_agent.evaluation import V2Manifest
assert V2Manifest.__module__ == 'uptick_agent.evaluation'
assert 'uptick_agent.evaluation.contracts' in sys.modules
assert 'uptick_agent.memory.compatibility.legacy' not in sys.modules
"""
    )


def test_execution_accepts_ports_without_wiring_default_composition() -> None:
    _fresh_process(
        """
import sys
import uptick_agent.evaluation.execution

assert 'uptick_agent.composition.evaluation_memory' not in sys.modules
"""
    )


def test_contract_and_sink_boundaries_do_not_load_audit_implementation() -> None:
    _fresh_process(
        """
import sys
import uptick_agent.ports
import uptick_agent.runs.execute

assert 'uptick_agent.memory.audit_contracts' in sys.modules
assert 'uptick_agent.memory.audit' not in sys.modules
"""
    )


def test_legacy_imports_preserve_identity_and_serialized_module_names() -> None:
    from uptick_agent.decisions.actions import GetOverview
    from uptick_agent.decisions.contracts import NextStep
    from uptick_agent.evaluation import V2Manifest
    from uptick_agent.evaluation.contracts import V2Manifest as CanonicalManifest
    from uptick_agent.memory.audit import AuditTraceEvent
    from uptick_agent.memory.audit_contracts import AuditTraceEvent as ContractEvent
    from uptick_agent.models import GetOverview as LegacyGetOverview
    from uptick_agent.models import NextStep as LegacyNextStep

    assert LegacyGetOverview is GetOverview
    assert LegacyNextStep is NextStep
    assert V2Manifest is CanonicalManifest
    assert AuditTraceEvent is ContractEvent
    assert GetOverview.__module__ == "uptick_agent.models"
    assert NextStep.__module__ == "uptick_agent.models"
    assert V2Manifest.__module__ == "uptick_agent.evaluation"
    assert AuditTraceEvent.__module__ == "uptick_agent.memory.audit"


def test_runner_uses_canonical_contract_modules() -> None:
    from uptick_agent.decisions.contracts import DecisionContext
    from uptick_agent.runs.execute import AgentRunner
    from uptick_agent.runs.results import RunResult

    assert AgentRunner.__module__ == "uptick_agent.runs.execute"
    assert DecisionContext.__module__ == "uptick_agent.models"
    assert RunResult.__module__ == "uptick_agent.models"


def test_no_contract_owner_imports_compatibility_facades() -> None:
    execution = (SRC / "uptick_agent/evaluation/execution.py").read_text(encoding="utf-8")
    runner = (SRC / "uptick_agent/runs/execute.py").read_text(encoding="utf-8")
    ports = (SRC / "uptick_agent/ports.py").read_text(encoding="utf-8")

    assert "composition.evaluation_memory" not in execution
    assert "from uptick_agent.models" not in runner
    assert "from uptick_agent.runner" not in runner
    assert "from uptick_agent.memory.audit import" not in ports
    assert "from uptick_agent.memory.audit_contracts import" in ports


def test_structured_decision_bridge_has_a_provider_neutral_import_boundary() -> None:
    _fresh_process(
        """
import sys
from pydantic import BaseModel
from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT
from uptick_agent.llm.decision_model import StructuredDecisionModel

assert StructuredDecisionModel.__module__ == 'uptick_agent.llm.decision_model'
assert 'uptick_agent.cli' not in sys.modules
assert 'uptick_agent.llm.openai' not in sys.modules
assert 'uptick_agent.simulator' not in sys.modules
class Client:
    model = 'test-model'
class Response(BaseModel):
    value: int
assert (
    StructuredDecisionModel(Client(), response_model=Response).system_prompt
    == CORE_SYSTEM_PROMPT
)
"""
    )


def test_prompt_composition_uses_one_neutral_core_and_legacy_exports() -> None:
    import hashlib

    from uptick_agent.decisions.contracts import NextStep
    from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT, compose_system_prompt
    from uptick_agent.llm.decision_model import StructuredDecisionModel
    from uptick_agent.llm.prompts import DEFAULT_SYSTEM_PROMPT
    from uptick_agent.llm.prompts import V2_SYSTEM_PROMPT as LegacyV2
    from uptick_agent.simulator.briefings import V2_ENVIRONMENT_BRIEFING, V2_SYSTEM_PROMPT

    assert compose_system_prompt(CORE_SYSTEM_PROMPT) == CORE_SYSTEM_PROMPT
    first = compose_system_prompt(CORE_SYSTEM_PROMPT, "first environment briefing")
    second = compose_system_prompt(CORE_SYSTEM_PROMPT, "second environment briefing")
    assert first != second
    assert CORE_SYSTEM_PROMPT in first
    assert CORE_SYSTEM_PROMPT in second
    assert "SRE" not in CORE_SYSTEM_PROMPT
    assert "e-commerce" not in CORE_SYSTEM_PROMPT
    assert "API" not in CORE_SYSTEM_PROMPT

    assert hashlib.sha256(DEFAULT_SYSTEM_PROMPT.encode()).hexdigest() == (
        "b2812ef681ea17daf710178388bf8e585af6451505a6badc00ed9cb2517f4a93"
    )

    class Client:
        model = "legacy-model"

    assert (
        StructuredDecisionModel(
            Client(), response_model=NextStep, system_prompt=DEFAULT_SYSTEM_PROMPT
        ).system_prompt
        == DEFAULT_SYSTEM_PROMPT
    )
    assert LegacyV2 == V2_SYSTEM_PROMPT
    assert compose_system_prompt(CORE_SYSTEM_PROMPT, V2_ENVIRONMENT_BRIEFING) == V2_SYSTEM_PROMPT


def test_cli_and_manifest_builder_use_the_composed_v2_prompt() -> None:
    import hashlib
    import runpy

    from uptick_agent.decisions.instructions import CORE_SYSTEM_PROMPT, compose_system_prompt

    manifest_module = runpy.run_path(str(ROOT / "scripts/build_v2_integration_manifest.py"))
    external_briefing = "externally preregistered startup commands"
    manifest = manifest_module["build_manifest"](
        ROOT,
        environment_briefing=external_briefing,
        smoke=True,
    )
    assert (
        manifest.profile.provider.prompt_fingerprint
        == hashlib.sha256(
            compose_system_prompt(CORE_SYSTEM_PROMPT, external_briefing).encode()
        ).hexdigest()
    )
