import pytest
from pydantic import ValidationError

from uptick_agent.models import FinishRun, GetOverview, NextStep, ProbePage


def test_next_step_requires_finish_to_match_completion() -> None:
    with pytest.raises(ValidationError):
        NextStep(
            current_situation="done",
            hypothesis="none",
            remaining_steps=[],
            task_completed=True,
            action=GetOverview(),
        )

    step = NextStep(
        current_situation="done",
        hypothesis="none",
        remaining_steps=[],
        task_completed=True,
        action=FinishRun(reason="simulation completed"),
    )
    assert step.action.kind == "finish"


def test_probe_requires_product_only_for_product_pages() -> None:
    assert ProbePage(page="product_list").product_id is None
    assert ProbePage(page="product_page", product_id="product-1").product_id == "product-1"
    with pytest.raises(ValidationError):
        ProbePage(page="purchase")
