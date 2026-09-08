from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from uptick_agent.core.bootstrap_models import ToolRegistry
from uptick_agent.core.contracts import Reasoner
from uptick_agent.core.memory_models import (
    ConsolidationBatch,
    ConsolidationOutput,
    LessonProposal,
    NoLesson,
)
from uptick_agent.core.models import JsonObject, ReasoningRequest, ReasoningTelemetry

LEARNER_SYSTEM_PROMPT = """
Return either one reusable, evidence-grounded lesson from the supplied closed episodes
and active lessons, or no_lesson when the input contains no novel durable principle.
Evidence groups are independent worlds; repeated runs of one world share one group and
do not independently confirm a lesson. Generalize only mechanisms supported across the
required groups and within the declared environment profile.

Among evidence-grounded candidates, choose the lesson with the highest expected future
value. Prefer one that would materially improve repeated decisions, prevent substantial
outcome loss, or correct a recurring failed strategy. Prefer corrective lessons over
descriptions of behavior the agent already follows reliably. Return no_lesson instead of
restating an active lesson or inventing a weak distinction. Do not weaken the evidence
requirements to select a higher-impact lesson.

Write a conditional decision principle that recomputes its choice from current evidence.
Do not turn observed server counts, thresholds, timestamps, seeds, run/world/episode IDs,
operation/deployment/product IDs, or remediation messages into universal rules. Keep such
details only in the dedicated evidence ID fields. Episode verification evaluates whether
the original action's expected result occurred; it does not determine whether that episode
supports or contradicts a new lesson. Confirmed, contradicted, and inconclusive episodes
may support or contradict a lesson according to their actual content.

Use evidence_episode_ids and contradicting_episode_ids only from batch.episodes. Use
supersedes only for IDs from batch.active_lessons. Do not cite evidence IDs embedded in an
active lesson as new evidence. Keep contradicting evidence explicit and name only
capabilities in the supplied registry. Do not emit credentials, credential values, hidden
state, evaluator data, executable code, or instructions unsupported by the supplied
evidence. The output object must set exactly one of proposal and no_lesson and set the
other field to null.
""".strip()


@dataclass(frozen=True, slots=True)
class ConsolidationDecision:
    proposal: LessonProposal | None
    no_lesson: NoLesson | None
    telemetry: ReasoningTelemetry


class MemoryConsolidator:
    """Provider-neutral projection from a bounded memory batch to one proposal."""

    def __init__(self, *, reasoner: Reasoner) -> None:
        self._reasoner = reasoner

    async def consolidate(
        self,
        batch: ConsolidationBatch,
        registry: ToolRegistry,
    ) -> ConsolidationDecision:
        request = ReasoningRequest(
            system_prompt=LEARNER_SYSTEM_PROMPT,
            user_prompt=(
                "Select a durable lesson or no_lesson from this consolidation input. "
                "JSON follows:\n" + _input_json(batch, registry)
            ),
            output_model=ConsolidationOutput,
            output_schema=cast(JsonObject, ConsolidationOutput.model_json_schema()),
        )
        result = await self._reasoner.reason(request)
        output = ConsolidationOutput.model_validate(result.output)
        return ConsolidationDecision(
            proposal=output.proposal,
            no_lesson=output.no_lesson,
            telemetry=result.telemetry,
        )


def _input_json(batch: ConsolidationBatch, registry: ToolRegistry) -> str:
    return json.dumps(
        {
            # run_outcomes is retained only for reading historical v5 traces.
            "batch": batch.model_dump(mode="json", exclude={"run_outcomes"}),
            "tool_registry": registry.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
