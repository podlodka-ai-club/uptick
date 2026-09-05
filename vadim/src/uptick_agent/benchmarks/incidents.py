"""Opaque controlled incident benchmark used by the learning-cycle experiment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from pydantic import Field, model_validator

from uptick_agent._model_base import StrictModel
from uptick_agent.decisions.runtime import ToolResult
from uptick_agent.environment.contracts import EnvironmentDecisionSpec
from uptick_agent.evaluation.learning_cycle import content_hash
from uptick_agent.memory.contracts import ObjectiveMetric
from uptick_agent.ports import EnvironmentSession
from uptick_agent.runs.runtime_results import RuntimeRunResult
from uptick_agent.simulator.actions import ApplyFix

INCIDENT_CODES = ("q7m", "k2p", "r4x", "v9n")
REPAIR_IDS = ("lumen", "ivory")
TRAINING_SEEDS = tuple(range(11, 19))
EVALUATION_CASES = tuple(
    (code, f"evaluation-{variant}") for code in INCIDENT_CODES for variant in ("1", "2")
)

# This mapping is an evaluator input. No call below serializes it or exposes
# it through a ToolResult; only the fixture-spec digest is preregistered.
DEFAULT_REPAIR_MAPPING = {
    INCIDENT_CODES[0]: REPAIR_IDS[0],
    INCIDENT_CODES[1]: REPAIR_IDS[0],
    INCIDENT_CODES[2]: REPAIR_IDS[1],
    INCIDENT_CODES[3]: REPAIR_IDS[1],
}


@dataclass(frozen=True, slots=True)
class IncidentCase:
    incident_code: str
    variant: str


@dataclass(slots=True)
class IncidentSession:
    run_id: str
    seed: int
    incident_code: str
    variant: str
    environment_id: str = "controlled-incident-fixture"
    scenario_id: str = ""
    recovered: bool = False
    action_count: int = 0


class IncidentDecision(StrictModel):
    """The benchmark's complete public decision schema.

    It deliberately advertises one adapter-owned remediation tool.  The
    evaluator's private mapping is never part of this model or its schema.
    """

    current_situation: str = Field(max_length=1000)
    hypothesis: str = Field(max_length=500)
    remaining_steps: list[str] = Field(min_length=0, max_length=5)
    task_completed: bool = False
    action: ApplyFix

    @model_validator(mode="after")
    def cannot_finish_before_recovery(self) -> IncidentDecision:
        if self.task_completed:
            raise ValueError("incident decisions cannot finish without an environment outcome")
        return self


def training_case_for_seed(seed: int) -> IncidentCase:
    if seed not in TRAINING_SEEDS:
        raise ValueError(f"seed {seed} is not a training seed")
    offset = seed - TRAINING_SEEDS[0]
    return IncidentCase(INCIDENT_CODES[offset % 4], f"training-{offset // 4 + 1}")


def evaluation_case_for_seed(seed: int) -> IncidentCase:
    if not 101 <= seed <= 108:
        raise ValueError(f"seed {seed} is not an evaluation seed")
    offset = seed - 101
    return IncidentCase(INCIDENT_CODES[offset % 4], f"evaluation-{offset // 4 + 1}")


def fixture_spec(mapping: Mapping[str, str] = DEFAULT_REPAIR_MAPPING) -> dict[str, object]:
    validate_mapping(mapping)
    return {
        "fixture_id": "controlled-incident-v1",
        "incident_codes": list(INCIDENT_CODES),
        "repair_ids": list(REPAIR_IDS),
        "training_cases": [asdict(training_case_for_seed(seed)) for seed in TRAINING_SEEDS],
        "evaluation_cases": [asdict(evaluation_case_for_seed(seed)) for seed in range(101, 109)],
        "mapping_digest": content_hash(dict(sorted(mapping.items()))),
    }


def validate_mapping(mapping: Mapping[str, str]) -> None:
    if set(mapping) != set(INCIDENT_CODES) or set(mapping.values()) != set(REPAIR_IDS):
        raise ValueError("fixture mapping must cover the four codes and both repairs")
    counts = {repair: sum(value == repair for value in mapping.values()) for repair in REPAIR_IDS}
    if counts != {repair: 2 for repair in REPAIR_IDS}:
        raise ValueError("fixture mapping must be balanced two-to-two")


class ControlledIncidentEnvironment:
    """Public adapter; the correct repair remains private to this instance."""

    def __init__(
        self, case: IncidentCase, mapping: Mapping[str, str], *, run_id_suffix: str = ""
    ) -> None:
        validate_mapping(mapping)
        self.case = case
        self._mapping = dict(mapping)
        self._run_id_suffix = run_id_suffix
        self.last_session: IncidentSession | None = None

    @property
    def decision_spec(self) -> EnvironmentDecisionSpec:
        return EnvironmentDecisionSpec(
            response_model=IncidentDecision,
            environment_briefing=(
                "The public incident interface exposes one typed ApplyFix action. "
                "Use only observed incident evidence and the listed repair choices."
            ),
            objective="Recover the incident through public evidence and typed remediation.",
        )

    def public_state(self, session: IncidentSession) -> dict[str, object]:
        # Recovery is already returned in the public ToolResult.  No hidden
        # fixture state is projected into the model context here.
        return {}

    async def start(self, *, seed: int, agent_id: str, agent_version: str):
        expected = (
            training_case_for_seed(seed)
            if seed in TRAINING_SEEDS
            else evaluation_case_for_seed(seed)
        )
        if expected != self.case:
            raise ValueError("fixture case does not match the declared seed")
        session = IncidentSession(
            run_id=f"controlled:{self.case.variant}:{self.case.incident_code}:{seed}",
            seed=seed,
            incident_code=self.case.incident_code,
            variant=self.case.variant,
            scenario_id=f"{self.case.variant}:{self.case.incident_code}",
        )
        if self._run_id_suffix:
            session.run_id = f"{session.run_id}:{self._run_id_suffix}"
        self.last_session = session
        return session, self._observation(session, action_kind="start")

    async def execute(self, session: EnvironmentSession, action: object) -> ToolResult:
        if not isinstance(session, IncidentSession):
            raise TypeError("controlled fixture received another session")
        session.action_count += 1
        if isinstance(action, ApplyFix):
            session.recovered = self._mapping[session.incident_code] == action.message
            return ToolResult(
                action_kind=action.kind,
                ok=session.recovered,
                summary=(
                    "Public remediation recovered the incident."
                    if session.recovered
                    else "Public remediation result recorded; the incident remains active."
                ),
                data={
                    "incident_code": session.incident_code,
                    "repair_id": action.message,
                    "recovered": session.recovered,
                    "available_repairs": list(REPAIR_IDS),
                },
                objective_metrics=[self._metric(float(session.recovered))],
                terminal=session.recovered,
            )
        return ToolResult(
            action_kind=getattr(action, "kind", "unknown"),
            ok=False,
            summary="The public remediation interface requires ApplyFix.",
            data={
                "incident_code": session.incident_code,
                "recovered": False,
                "available_repairs": list(REPAIR_IDS),
            },
            objective_metrics=[self._metric(0.0)],
            terminal=False,
        )

    async def finish(
        self,
        session: EnvironmentSession,
        *,
        steps: int,
        duration_seconds: float,
        stop_reason: str,
    ) -> RuntimeRunResult:
        if not isinstance(session, IncidentSession):
            raise TypeError("controlled fixture received another session")
        return RuntimeRunResult(
            run_id=session.run_id,
            seed=session.seed,
            agent_id="controlled-incident-learning",
            agent_version="1.0",
            status="completed" if session.recovered else "failed",
            steps=steps,
            duration_seconds=max(0.0, duration_seconds),
            objective_metrics=[self._metric(float(session.recovered))],
            stop_reason=stop_reason,
        )

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _metric(value: float) -> ObjectiveMetric:
        return ObjectiveMetric(name="incident_recovered", value=value, unit="boolean")

    @staticmethod
    def _observation(session: IncidentSession, *, action_kind: str) -> ToolResult:
        return ToolResult(
            action_kind=action_kind,
            summary="An opaque incident is active and requires remediation.",
            data={
                "incident_code": session.incident_code,
                "variant": session.variant,
                "available_repairs": list(REPAIR_IDS),
            },
            objective_metrics=[ControlledIncidentEnvironment._metric(0.0)],
        )


__all__ = [
    "ControlledIncidentEnvironment",
    "DEFAULT_REPAIR_MAPPING",
    "EVALUATION_CASES",
    "INCIDENT_CODES",
    "IncidentCase",
    "IncidentDecision",
    "REPAIR_IDS",
    "TRAINING_SEEDS",
    "evaluation_case_for_seed",
    "fixture_spec",
    "training_case_for_seed",
    "validate_mapping",
]
