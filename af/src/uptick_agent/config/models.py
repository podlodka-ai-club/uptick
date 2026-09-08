from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from uptick_agent.core.models import ReasoningEffortName


class FrozenConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecretRef(FrozenConfigModel):
    from_env: StrictStr = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class AgentIdentityConfig(FrozenConfigModel):
    id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    version: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


class CodexDecisionReasonerConfig(FrozenConfigModel):
    provider: Literal["codex"]
    model: StrictStr = Field(min_length=1)
    effort: ReasoningEffortName | None
    thread_mode: Literal["ephemeral"]
    timeout_seconds: float | None = Field(gt=0)
    retries: StrictInt = Field(ge=0)


class OpenAIDecisionReasonerConfig(FrozenConfigModel):
    provider: Literal["openai"]
    model: StrictStr = Field(min_length=1)
    effort: ReasoningEffortName | None
    thread_mode: Literal["stateless"]
    timeout_seconds: float | None = Field(gt=0)
    retries: StrictInt = Field(ge=0)
    api_key: SecretRef
    base_url: StrictStr | None = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return None if value is None else validate_http_url(value)


type DecisionReasonerConfig = Annotated[
    CodexDecisionReasonerConfig | OpenAIDecisionReasonerConfig,
    Field(discriminator="provider"),
]


class ReasonersConfig(FrozenConfigModel):
    decision: DecisionReasonerConfig
    learner: DecisionReasonerConfig | None = None


class NoMemoryConfig(FrozenConfigModel):
    backend: Literal["none"]


class SQLiteMemoryConfig(FrozenConfigModel):
    backend: Literal["sqlite"]
    path: Path


type MemoryConfig = Annotated[
    NoMemoryConfig | SQLiteMemoryConfig,
    Field(discriminator="backend"),
]


class LearningConfig(FrozenConfigModel):
    trigger: Literal["after_run", "after_closed_episode"]
    min_evidence_groups: StrictInt = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_inline_evidence_groups(self) -> LearningConfig:
        if self.trigger == "after_closed_episode" and self.min_evidence_groups != 1:
            raise ValueError("after_closed_episode requires min_evidence_groups=1 in schema v2")
        return self


class JsonlRunStoreConfig(FrozenConfigModel):
    backend: Literal["jsonl"]
    path: Path


class UptickV2EnvironmentConfig(FrozenConfigModel):
    adapter: Literal["uptickv2"]
    endpoint: StrictStr
    seed: StrictInt
    step_limit: StrictInt | None = Field(default=None, ge=1)
    prompt_file: Path | None = None
    participant_token: SecretRef | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return validate_http_url(value)

    @field_validator("seed")
    @classmethod
    def validate_seed(cls, value: int) -> int:
        if value == 0:
            raise ValueError("seed must be a non-zero integer")
        return value


type EnvironmentConfig = UptickV2EnvironmentConfig


class AgentConfig(FrozenConfigModel):
    schema_version: Literal[2]
    agent: AgentIdentityConfig
    reasoners: ReasonersConfig
    memory: MemoryConfig
    learning: LearningConfig | None = None
    run_store: JsonlRunStoreConfig
    environment: EnvironmentConfig

    @model_validator(mode="after")
    def validate_learning_composition(self) -> AgentConfig:
        learner_configured = self.reasoners.learner is not None
        learning_configured = self.learning is not None
        if learner_configured != learning_configured:
            raise ValueError("reasoners.learner must be configured exactly when learning is")
        if learning_configured and isinstance(self.memory, NoMemoryConfig):
            raise ValueError("learning requires a persistent memory backend")
        return self


def validate_http_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("must be an absolute HTTP(S) URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    if parsed.query:
        raise ValueError("URL query is not allowed")
    if parsed.fragment:
        raise ValueError("URL fragment is not allowed")
    return value
