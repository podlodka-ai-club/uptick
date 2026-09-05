"""Neutral Pydantic base shared by the canonical contract families."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

LEGACY_MODELS_MODULE = "uptick_agent.models"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def preserve_legacy_identity(*models: type[BaseModel]) -> None:
    """Keep historical qualified modules in persisted request metadata."""

    for model in models:
        model.__module__ = LEGACY_MODELS_MODULE


preserve_legacy_identity(StrictModel)
