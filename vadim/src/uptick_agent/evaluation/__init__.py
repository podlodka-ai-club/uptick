"""Lazy compatibility facade for the public evaluation contract module."""

from importlib import import_module

__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "V2_API_VERSION",
    "V2_METRICS",
    "V2_MEMORY_METRICS",
    "V2_PROVIDER_METRICS",
    "V2_OBJECTIVE_KIND",
    "EvaluationModel",
    "V2EnvironmentPin",
    "V2ProviderPin",
    "V2SourcePin",
    "V2Budget",
    "V2FailurePolicy",
    "V2Condition",
    "V2RunMatrixBlock",
    "V2PlannedContrast",
    "V2EvaluationProfile",
    "V2Manifest",
    "V2SnapshotRef",
    "FrozenEvaluationBinding",
    "ProviderTelemetry",
    "MemoryTelemetry",
    "V2OutcomeMetrics",
    "V2AttemptRecord",
    "V2Coverage",
    "V2MetricDistribution",
    "V2ConditionReport",
    "V2PairwiseReport",
    "V2Report",
    "profile_hash",
    "manifest_hash",
    "frozen_binding_hash",
    "attempt_hash",
    "build_run_matrix",
    "resolved_manifest",
    "freeze_evaluation_binding",
    "select_first_attempts",
    "aggregate_report",
    "verify_report",
    "sha256_json",
]


def __getattr__(name: str):
    if name in __all__:
        return getattr(import_module("uptick_agent.evaluation.contracts"), name)
    raise AttributeError(name)
