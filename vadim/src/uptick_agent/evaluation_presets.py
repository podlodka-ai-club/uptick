"""Experimental memory conditions used by the offline and v2 matrix.

The presets are declarations only.  They do not run a simulator, promote a
module, or manufacture approval evidence.  ``experimental_runtime`` is the
composition boundary for turning a supported declaration into real memory
objects.
"""

from __future__ import annotations

from pydantic import Field

from uptick_agent.memory.config import (
    AdvancedRetrievalConfig,
    ContextBudgetConfig,
    ForgettingSettings,
    MemoryConfiguration,
    ModuleConfig,
    RetrievalConfig,
)
from uptick_agent.memory.consolidation import ConsolidationSettings
from uptick_agent.memory.contracts import ContractModel
from uptick_agent.memory.lesson_contracts import LessonSettings
from uptick_agent.memory.patterns import PatternQuerySettings
from uptick_agent.memory.playbooks import PlaybookQuerySettings
from uptick_agent.memory.tool_knowledge import ToolKnowledgeQuerySettings

_SCOPE_PATHS = ("observation.action_kind", "observation.ok")
_ACTION_PATH = "action.kind"
_RESULT_PATH = "result.ok"


class EvaluationPreset(ContractModel):
    """One fully resolved, still-experimental matrix condition."""

    condition_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    configuration: MemoryConfiguration
    supported: bool = True
    unsupported_reasons: tuple[str, ...] = ()
    ablation_of: str | None = None
    disabled_feature: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def enabled_features(self) -> tuple[str, ...]:
        modules = self.configuration.modules
        return tuple(name for name, declaration in modules.items() if declaration.enabled)


def default_pattern_query_settings() -> PatternQuerySettings:
    """Return the explicit generic transition projection used by world memory."""

    return PatternQuerySettings(
        scope_paths=_SCOPE_PATHS,
        action_path=_ACTION_PATH,
        result_path=_RESULT_PATH,
    )


def default_playbook_query_settings() -> PlaybookQuerySettings:
    """Return the generic action-sequence projection used by playbooks."""

    return PlaybookQuerySettings(
        scope_paths=_SCOPE_PATHS,
        action_path=_ACTION_PATH,
        sequence_length=2,
        guard_path=_RESULT_PATH,
        guard_value=True,
    )


def default_lesson_settings() -> LessonSettings:
    """Use a declared public objective field without encoding world rules."""

    return LessonSettings(
        metric_name="uptime_ratio",
        metric_unit="ratio",
        direction="maximize",
        condition_keys=("action_kind", "ok"),
    )


def default_tool_knowledge_query_settings() -> ToolKnowledgeQuerySettings:
    """Return generic adapter projections; adapter identity is explicit data."""

    return ToolKnowledgeQuerySettings(
        adapter_identity="generic-tool-result-v1",
        scope_paths=_SCOPE_PATHS,
        action_path=_ACTION_PATH,
        input_paths=("action.kind",),
        response_path=_RESULT_PATH,
    )


def _module(enabled: bool, *, version: str = "1.0") -> ModuleConfig:
    return ModuleConfig(
        enabled=enabled,
        version=version,
        status="experimental",
        max_context_items=32,
        max_context_tokens=4_000,
    )


def _configuration(
    condition_id: str,
    *,
    legacy: bool = False,
    episodic: bool = False,
    lessons: bool = False,
    world: bool = False,
    consolidation: bool = False,
    retrieval: bool = False,
    advanced_retrieval: bool | None = None,
    playbooks: bool = False,
    tool_knowledge: bool = False,
    forgetting: bool = False,
) -> MemoryConfiguration:
    pattern = default_pattern_query_settings() if world else None
    playbook = default_playbook_query_settings() if playbooks else None
    tool = default_tool_knowledge_query_settings() if tool_knowledge else None
    if advanced_retrieval is None:
        advanced_retrieval = retrieval
    return MemoryConfiguration(
        profile_id=condition_id,
        profile_kind="experiment",
        compatibility_legacy=_module(legacy, version="legacy-1.0"),
        episodic=_module(episodic),
        lessons=_module(lessons),
        lesson_settings=default_lesson_settings() if lessons else None,
        world_model=_module(world),
        world_query_settings=pattern,
        playbooks=_module(playbooks),
        playbook_query_settings=playbook,
        tool_knowledge=_module(tool_knowledge),
        tool_knowledge_query_settings=tool,
        consolidation=_module(consolidation),
        consolidation_settings=(
            ConsolidationSettings(
                lesson_settings=default_lesson_settings() if lessons else None,
                pattern_settings=pattern,
                max_replay_records=200,
                max_contrast_pairs=100,
            )
            if consolidation
            else None
        ),
        forgetting=_module(forgetting),
        forgetting_settings=ForgettingSettings(apply_decay=forgetting),
        context_budget=ContextBudgetConfig(total_items=128, total_tokens=16_000),
        retrieval=RetrievalConfig(
            lexical=True,
            structured=retrieval,
            semantic=False,
            advanced=AdvancedRetrievalConfig(enabled=advanced_retrieval),
        ),
    )


def _cumulative(
    condition_id: str,
    *,
    tool_knowledge: bool = False,
    forgetting: bool = False,
    retrieval: bool = False,
    consolidation: bool = False,
    world: bool = False,
    playbooks: bool = False,
    advanced_retrieval: bool | None = None,
) -> MemoryConfiguration:
    return _configuration(
        condition_id,
        episodic=True,
        lessons=True,
        world=world,
        consolidation=consolidation,
        retrieval=retrieval,
        advanced_retrieval=advanced_retrieval,
        playbooks=playbooks,
        tool_knowledge=tool_knowledge,
        forgetting=forgetting,
    )


def experimental_presets() -> tuple[EvaluationPreset, ...]:
    """Return A0--A9 in normative order.

    A8 and A9 use the separate generic tool-knowledge module.  All conditions
    remain experimental and require no approval record.
    """

    presets = [
        EvaluationPreset(condition_id="A0", configuration=_configuration("A0")),
        EvaluationPreset(
            condition_id="A1",
            configuration=_configuration("A1", legacy=True),
        ),
        EvaluationPreset(
            condition_id="A2",
            configuration=_configuration("A2", episodic=True),
        ),
        EvaluationPreset(
            condition_id="A3",
            configuration=_configuration("A3", episodic=True, lessons=True),
        ),
        EvaluationPreset(
            condition_id="A4",
            configuration=_cumulative("A4", world=True),
        ),
        EvaluationPreset(
            condition_id="A5",
            configuration=_cumulative("A5", world=True, consolidation=True),
            notes=("Consolidation is explicit and must run after training before freeze.",),
        ),
        EvaluationPreset(
            condition_id="A6",
            configuration=_cumulative("A6", world=True, consolidation=True, retrieval=True),
        ),
        EvaluationPreset(
            condition_id="A7",
            configuration=_cumulative(
                "A7", world=True, consolidation=True, retrieval=True, playbooks=True
            ),
        ),
        EvaluationPreset(
            condition_id="A8",
            configuration=_cumulative(
                "A8",
                world=True,
                consolidation=True,
                retrieval=True,
                playbooks=True,
                tool_knowledge=True,
            ),
        ),
        EvaluationPreset(
            condition_id="A9",
            configuration=_cumulative(
                "A9",
                world=True,
                consolidation=True,
                retrieval=True,
                playbooks=True,
                tool_knowledge=True,
                forgetting=True,
            ),
            notes=("Forgetting is an operational view; it does not delete retained evidence.",),
        ),
    ]
    return tuple(presets)


def targeted_ablations() -> tuple[EvaluationPreset, ...]:
    """Return the declared one-factor follow-ups from the evaluation plan."""

    a6 = _cumulative("A6-minus-world-model", consolidation=True, retrieval=True, world=False)
    no_consolidation = _cumulative(
        "A6-minus-consolidation", consolidation=False, retrieval=True, world=True
    )
    no_structured = _cumulative(
        "A6-minus-structured-retrieval",
        consolidation=True,
        retrieval=False,
        advanced_retrieval=True,
        world=True,
    )
    no_tool = _cumulative(
        "A8-minus-tool-knowledge",
        consolidation=True,
        retrieval=True,
        world=True,
        playbooks=True,
        tool_knowledge=False,
    )
    return (
        EvaluationPreset(
            condition_id="A6-minus-world-model",
            configuration=a6,
            ablation_of="A6",
            disabled_feature="world_model",
        ),
        EvaluationPreset(
            condition_id="A6-minus-contradiction-tracking",
            configuration=_cumulative(
                "A6-minus-contradiction-tracking",
                consolidation=True,
                retrieval=True,
                world=True,
            ),
            supported=False,
            unsupported_reasons=(
                "contradiction handling is part of the mandatory activation gate "
                "and has no safe opt-out",
            ),
            ablation_of="A6",
            disabled_feature="contradiction_tracking",
        ),
        EvaluationPreset(
            condition_id="A6-minus-consolidation",
            configuration=no_consolidation,
            ablation_of="A6",
            disabled_feature="consolidation",
        ),
        EvaluationPreset(
            condition_id="A6-minus-structured-retrieval",
            configuration=no_structured,
            ablation_of="A6",
            disabled_feature="structured_retrieval",
        ),
        EvaluationPreset(
            condition_id="A8-minus-tool-knowledge",
            configuration=no_tool,
            ablation_of="A8",
            disabled_feature="tool_knowledge",
        ),
    )


def all_experimental_presets() -> tuple[EvaluationPreset, ...]:
    return experimental_presets() + targeted_ablations()


__all__ = [
    "EvaluationPreset",
    "all_experimental_presets",
    "default_lesson_settings",
    "default_pattern_query_settings",
    "default_playbook_query_settings",
    "default_tool_knowledge_query_settings",
    "experimental_presets",
    "targeted_ablations",
]
