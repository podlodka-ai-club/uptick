from __future__ import annotations

import os
from pathlib import Path

import httpx

from uptick_agent.core.contracts import Environment, Reasoner
from uptick_agent.core.models import RunSpec
from uptick_agent.environments.discovered.discovery import _save, discover
from uptick_agent.environments.discovered.session import DiscoveredSession
from uptick_agent.environments.discovered.transport import SessionTransport


class UptickV2Environment:
    """The only fixed world operation is start; all gameplay APIs are discovered."""

    def __init__(
        self,
        endpoint: str,
        *,
        reasoner: Reasoner,
        cache_directory: Path,
        participant_token_env: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.transport = SessionTransport(endpoint, client)
        self.reasoner = reasoner
        self.cache_directory = cache_directory
        self.participant_token_env = participant_token_env
        self._started = False

    async def run(self, spec: RunSpec) -> Environment:
        if self._started:
            raise RuntimeError("environment already launched a world")
        body = {
            "seed": spec.parameters["seed"],
            "agent_id": spec.agent_id,
            "agent_version": spec.agent_version,
            "request_id": spec.run_id,
        }
        if self.participant_token_env is not None:
            token = os.environ.get(self.participant_token_env)
            if not token or not token.strip():
                raise ValueError(
                    f"Uptick participant token environment variable "
                    f"{self.participant_token_env} is not set"
                )
            self.transport.secret_values.add(token)
            body["participant_token"] = token
        self._started = True
        started = await self.transport.request(
            "POST",
            "/v2/start",
            body=body,
        )
        self.transport.remote_run_id = started["run_id"]
        receipt_path = self.cache_directory / "launches" / f"{spec.run_id}.json"
        receipt = {
            "schema_version": 1,
            "run_id": spec.run_id,
            "simulator_run_id": started["run_id"],
            "status": "discovering",
        }
        _save(receipt_path, receipt)
        auth = started["control_panel_auth"]
        if auth["scheme"] != "basic":
            raise ValueError("unsupported panel authentication")
        self.transport.panel_auth = httpx.BasicAuth(auth["username"], auth["password"])
        self.transport.secret_values.update((auth["username"], auth["password"]))
        source = started["commands_markdown"]
        # Bootstrap documents contain stable API templates, never the auth envelope.
        for secret in self.transport.secret_values:
            if secret:
                source = source.replace(secret, "[private]")
        try:
            tools, meaning, source, contract_sha256 = await discover(
                source=source,
                transport=self.transport,
                reasoner=self.reasoner,
                cache_directory=self.cache_directory,
                operational_run_id=spec.run_id,
            )
        except Exception as error:
            _save(
                receipt_path,
                receipt | {"status": "discovery_failed", "error_type": type(error).__name__},
            )
            raise
        _save(receipt_path, receipt | {"status": "ready", "tool_count": len(tools)})
        return DiscoveredSession(
            transport=self.transport,
            tools=tools,
            meaning=meaning,
            source=source,
            spec=spec,
            contract_sha256=contract_sha256,
        )

    async def aclose(self) -> None:
        await self.transport.aclose()
