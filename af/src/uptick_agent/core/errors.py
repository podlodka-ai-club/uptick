from __future__ import annotations

from uptick_agent.core.models import ReasoningTelemetry


class ReasonerFailure(RuntimeError):
    """Provider failure with safe, reportable telemetry for all completed attempts."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        telemetry: ReasoningTelemetry,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.telemetry = telemetry


class RunExecutionError(RuntimeError):
    """A run failed after an environment run ID had been allocated."""

    def __init__(self, run_id: str, stage: str, error: Exception) -> None:
        super().__init__(f"run {run_id!r} failed during {stage}: {error}")
        self.run_id = run_id
        self.stage = stage
        self.error = error


class RunStoreFailure(RuntimeError):
    """A durable event acknowledgement failed and must not be recursively recorded."""
