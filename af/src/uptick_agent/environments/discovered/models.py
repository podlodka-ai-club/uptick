from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from uptick_agent.core.models import JsonObject, StrictModel


class DocumentationPlan(StrictModel):
    openapi_path: str | None
    command_catalog_path: str | None
    command_catalog_requires_panel_auth: bool


class ToolMeaning(StrictModel):
    name: str
    mutates_state: bool
    role: Literal["observation", "overview", "inbox", "credentials", "advance"]
    notes: str = Field(max_length=1500)


class WorldMeaning(StrictModel):
    objective: str = Field(min_length=1, max_length=1500)
    tools: list[ToolMeaning] = Field(min_length=1, max_length=128)
    status_field: str
    completed_status: str
    clock_field: str
    time_field: str
    end_time_field: str


@dataclass
class HttpTool:
    name: str
    description: str
    method: str
    path: str
    schema: JsonObject
    parameters: list[JsonObject] = field(default_factory=list)
    body_schema: JsonObject | None = None
    panel_auth: bool = False
    command: str | None = None
    target_auth: bool = False
    result_schema: JsonObject = field(default_factory=dict)
    asynchronous: bool = False
    meaning: ToolMeaning | None = None
