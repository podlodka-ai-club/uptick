from __future__ import annotations

from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from uptick_agent.simulator.models import (
    AdvanceTimeResponse,
    ApplyFixResponse,
    DeploymentsResponse,
    EconomyResponse,
    ErrorBody,
    LogsResponse,
    MetricsResponse,
    OperationAcceptedResponse,
    OperationResponse,
    OverviewResponse,
    ProbeResponse,
    ResourcesResponse,
    StartRunResponse,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class SimulatorApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class SimulatorClient:
    """Typed async client for AigizK/HackerSprint2_sim's public API."""

    def __init__(
        self,
        base_url: str = "http://81.176.229.58:8080",
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout),
            headers={"Accept": "application/json"},
        )

    async def _request(
        self,
        method: str,
        path: str,
        response_type: type[ResponseT],
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> ResponseT:
        response = await self.client.request(method, path.lstrip("/"), json=json, params=params)
        if response.is_error:
            try:
                body = ErrorBody.model_validate(response.json())
                code, message = body.error, body.message
            except (ValueError, TypeError):
                code, message = "HTTP_ERROR", response.text[:1000]
            raise SimulatorApiError(response.status_code, code, message)
        return response_type.model_validate(response.json())

    @staticmethod
    def _run_path(run_id: str, suffix: str) -> str:
        return f"v1/runs/{quote(run_id, safe='')}{suffix}"

    async def start(
        self, *, seed: int, agent_id: str, agent_version: str, request_id: str
    ) -> StartRunResponse:
        return await self._request(
            "POST",
            "v1/start",
            StartRunResponse,
            json={
                "seed": seed,
                "agent_id": agent_id,
                "agent_version": agent_version,
                "request_id": request_id,
            },
        )

    async def overview(self, run_id: str) -> OverviewResponse:
        return await self._request("GET", self._run_path(run_id, "/overview"), OverviewResponse)

    async def metrics(self, run_id: str) -> MetricsResponse:
        return await self._request("GET", self._run_path(run_id, "/metrics"), MetricsResponse)

    async def logs(
        self,
        run_id: str,
        *,
        from_time: str | None = None,
        status: int | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> LogsResponse:
        params = {"limit": limit}
        if from_time is not None:
            params["from"] = from_time
        if status is not None:
            params["status"] = status
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request(
            "GET", self._run_path(run_id, "/logs"), LogsResponse, params=params
        )

    async def resources(self, run_id: str) -> ResourcesResponse:
        return await self._request("GET", self._run_path(run_id, "/resources"), ResourcesResponse)

    async def scale(
        self, run_id: str, *, request_id: str, desired_instances: int
    ) -> OperationAcceptedResponse:
        return await self._request(
            "PUT",
            self._run_path(run_id, "/resources/backend"),
            OperationAcceptedResponse,
            json={"request_id": request_id, "desired_instances": desired_instances},
        )

    async def apply_fix(self, run_id: str, *, request_id: str, message: str) -> ApplyFixResponse:
        return await self._request(
            "POST",
            self._run_path(run_id, "/fixes"),
            ApplyFixResponse,
            json={"request_id": request_id, "message": message},
        )

    async def deployments(self, run_id: str) -> DeploymentsResponse:
        return await self._request(
            "GET", self._run_path(run_id, "/deployments"), DeploymentsResponse
        )

    async def start_deployment(
        self, run_id: str, *, request_id: str, deployment_id: str
    ) -> OperationAcceptedResponse:
        return await self._request(
            "POST",
            self._run_path(run_id, "/deployments"),
            OperationAcceptedResponse,
            json={"request_id": request_id, "deployment_id": deployment_id},
        )

    async def operation(self, run_id: str, operation_id: str) -> OperationResponse:
        suffix = f"/operations/{quote(operation_id, safe='')}"
        return await self._request("GET", self._run_path(run_id, suffix), OperationResponse)

    async def probe(
        self,
        run_id: str,
        *,
        request_id: str,
        page: str,
        product_id: str | None,
    ) -> ProbeResponse:
        payload = {"request_id": request_id, "page": page}
        if product_id is not None:
            payload["product_id"] = product_id
        return await self._request(
            "POST", self._run_path(run_id, "/probes"), ProbeResponse, json=payload
        )

    async def advance(
        self, run_id: str, *, request_id: str, duration_seconds: int
    ) -> AdvanceTimeResponse:
        return await self._request(
            "POST",
            self._run_path(run_id, "/time/advance"),
            AdvanceTimeResponse,
            json={"request_id": request_id, "duration_seconds": duration_seconds},
        )

    async def economy(self, run_id: str) -> EconomyResponse:
        return await self._request("GET", self._run_path(run_id, "/economy"), EconomyResponse)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
