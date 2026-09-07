from __future__ import annotations

from uptick_agent.evaluation_presets import (
    all_experimental_presets,
    experimental_presets,
    targeted_ablations,
)


def test_normative_presets_are_cumulative_and_experimental() -> None:
    presets = experimental_presets()
    assert tuple(item.condition_id for item in presets) == tuple(f"A{index}" for index in range(10))
    for preset in presets:
        assert preset.configuration.profile_kind == "experiment"
        assert all(
            module.status == "experimental" for module in preset.configuration.modules.values()
        )

    flags = [
        tuple(name for name, module in item.configuration.modules.items() if module.enabled)
        for item in presets
    ]
    assert flags[0] == ()
    assert flags[1] == ("compatibility.legacy",)
    assert flags[2] == ("episodic",)
    assert "world_model" in flags[4]
    assert "consolidation" in flags[5]
    assert "playbooks" in flags[7]
    assert "tool_knowledge" in flags[8]
    assert "forgetting" in flags[9]


def test_targeted_matrix_marks_mandatory_contradiction_gate_unsupported() -> None:
    ablations = targeted_ablations()
    assert tuple(item.condition_id for item in ablations) == (
        "A6-minus-world-model",
        "A6-minus-contradiction-tracking",
        "A6-minus-consolidation",
        "A6-minus-structured-retrieval",
        "A8-minus-tool-knowledge",
    )
    contradiction = ablations[1]
    assert not contradiction.supported
    assert "activation gate" in contradiction.unsupported_reasons[0]
    assert all(item.supported for item in (ablations[0], *ablations[2:]))
    assert len(all_experimental_presets()) == 15
