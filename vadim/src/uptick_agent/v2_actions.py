"""Schema-constrained actions exposed by simulator API v2."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class V2ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ResourceId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


class EmptyParams(V2ActionModel):
    """A command parameter object that intentionally has no fields."""


class UserAgentMatch(V2ActionModel):
    operator: Literal["equals", "contains"]
    value: str = Field(min_length=1, max_length=2048)


class FirewallMatch(V2ActionModel):
    source_cidr: str | None = Field(
        default=None,
        min_length=3,
        max_length=49,
        pattern=r"^[0-9A-Fa-f:.]+/[0-9]{1,3}$",
    )
    region_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    user_agent: UserAgentMatch | None = None

    @model_validator(mode="after")
    def validate_match(self) -> FirewallMatch:
        if self.source_cidr is None and self.region_code is None and self.user_agent is None:
            raise ValueError("at least one firewall match condition is required")
        if self.source_cidr is not None:
            try:
                ipaddress.ip_network(self.source_cidr, strict=True)
            except ValueError as error:
                raise ValueError("source_cidr must be a network with zero host bits") from error
        return self


class FirewallRule(V2ActionModel):
    rule_id: ResourceId
    priority: int = Field(ge=0)
    action: Literal["allow", "deny"]
    match: FirewallMatch
    enabled: bool
    expires_at: datetime | None = None


class FirewallRuleIdParams(V2ActionModel):
    rule_id: ResourceId


class ServerTypesListParams(V2ActionModel):
    role: Literal["backend", "database"] | None = None


class ServerCreateParams(V2ActionModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"\S")
    role: Literal["backend", "database"]
    instance_type: ResourceId


class ServerIdParams(V2ActionModel):
    server_id: ResourceId


class DatabaseCreateParams(V2ActionModel):
    server_id: ResourceId
    name: str = Field(min_length=1, max_length=128, pattern=r"\S")


class DatabaseIdParams(V2ActionModel):
    database_id: ResourceId


class DatabaseBackupsListParams(V2ActionModel):
    database_id: ResourceId | None = None


class DatabaseRestoreParams(V2ActionModel):
    database_id: ResourceId
    backup_id: ResourceId


class SiteDatabaseSetParams(V2ActionModel):
    database_id: ResourceId
    expected_current_database_id: ResourceId


class FirewallRulesListRequest(V2ActionModel):
    command: Literal["firewall.rules.list"]
    params: EmptyParams


class FirewallRulesUpsertRequest(V2ActionModel):
    command: Literal["firewall.rules.upsert"]
    params: FirewallRule


class FirewallRulesDeleteRequest(V2ActionModel):
    command: Literal["firewall.rules.delete"]
    params: FirewallRuleIdParams


class ServerTypesListRequest(V2ActionModel):
    command: Literal["server.types.list"]
    params: ServerTypesListParams


class ServerCreateRequest(V2ActionModel):
    command: Literal["server.create"]
    params: ServerCreateParams


class ServerInspectRequest(V2ActionModel):
    command: Literal["server.inspect"]
    params: ServerIdParams


class ServerDeleteRequest(V2ActionModel):
    command: Literal["server.delete"]
    params: ServerIdParams


class DatabaseCreateRequest(V2ActionModel):
    command: Literal["database.create"]
    params: DatabaseCreateParams


class DatabaseInspectRequest(V2ActionModel):
    command: Literal["database.inspect"]
    params: DatabaseIdParams


class DatabaseBackupRequest(V2ActionModel):
    command: Literal["database.backup"]
    params: DatabaseIdParams


class DatabaseBackupsListRequest(V2ActionModel):
    command: Literal["database.backups.list"]
    params: DatabaseBackupsListParams


class DatabaseRestoreRequest(V2ActionModel):
    command: Literal["database.restore"]
    params: DatabaseRestoreParams


class SiteConfigGetRequest(V2ActionModel):
    command: Literal["site.config.get"]
    params: EmptyParams


class SiteStopRequest(V2ActionModel):
    command: Literal["site.stop"]
    params: EmptyParams


class SiteStartRequest(V2ActionModel):
    command: Literal["site.start"]
    params: EmptyParams


class SiteDatabaseSetRequest(V2ActionModel):
    command: Literal["site.database.set"]
    params: SiteDatabaseSetParams


class DiskUsageRequest(V2ActionModel):
    command: Literal["disk.usage"]
    params: ServerIdParams


class DiskCleanupRequest(V2ActionModel):
    command: Literal["disk.cleanup"]
    params: ServerIdParams


ControlCommandRequest = Annotated[
    FirewallRulesListRequest
    | FirewallRulesUpsertRequest
    | FirewallRulesDeleteRequest
    | ServerTypesListRequest
    | ServerCreateRequest
    | ServerInspectRequest
    | ServerDeleteRequest
    | DatabaseCreateRequest
    | DatabaseInspectRequest
    | DatabaseBackupRequest
    | DatabaseBackupsListRequest
    | DatabaseRestoreRequest
    | SiteConfigGetRequest
    | SiteStopRequest
    | SiteStartRequest
    | SiteDatabaseSetRequest
    | DiskUsageRequest
    | DiskCleanupRequest,
    Field(discriminator="command"),
]


class ControlCommand(V2ActionModel):
    kind: Literal["control_command"] = "control_command"
    request: ControlCommandRequest


class GetInbox(V2ActionModel):
    kind: Literal["get_inbox"] = "get_inbox"


class GetControlCommands(V2ActionModel):
    kind: Literal["get_control_commands"] = "get_control_commands"
