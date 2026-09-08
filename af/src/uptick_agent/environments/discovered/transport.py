from __future__ import annotations

import json
from time import monotonic
from typing import Any
from urllib.parse import quote

import httpx

from uptick_agent.core.models import EnvironmentTelemetry
from uptick_agent.environments.discovered.schema import safe_path


class TransportError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"simulator HTTP {status}: {code}")


class SessionTransport:
    def __init__(self, endpoint: str, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            base_url=endpoint.rstrip("/") + "/", timeout=30, follow_redirects=False
        )
        self._owns_client = client is None
        self.remote_run_id: str | None = None
        self.panel_auth: httpx.BasicAuth | None = None
        self.secret_values: set[str] = set()
        self.calls = 0
        self.duration = 0.0

    async def request(
        self,
        method: str,
        path: str,
        *,
        panel: bool = False,
        body: dict | None = None,
        params: Any = None,
        text: bool = False,
    ) -> Any:
        safe_path(path)
        if "{run_id}" in path:
            if self.remote_run_id is None:
                raise ValueError("no active simulator session")
            path = path.replace("{run_id}", quote(self.remote_run_id, safe=""))
        if "{" in path or "}" in path:
            raise ValueError("unbound path parameter")
        if panel and self.panel_auth is None:
            raise ValueError("panel access is unavailable")
        self.calls += 1
        started = monotonic()
        try:
            async with self.client.stream(
                method,
                path.lstrip("/"),
                json=body,
                params=params,
                auth=self.panel_auth if panel else None,
                follow_redirects=False,
            ) as response:
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 4_000_000:
                        raise TransportError(response.status_code, "RESPONSE_TOO_LARGE")
                if not 200 <= response.status_code < 300:
                    # Error text may echo request secrets; retain only a bounded code.
                    try:
                        code = json.loads(payload).get("error", "HTTP_ERROR")
                    except (ValueError, AttributeError):
                        code = "HTTP_ERROR"
                    if not isinstance(code, str) or not code.replace("_", "").isalnum():
                        code = "HTTP_ERROR"
                    raise TransportError(response.status_code, code[:100])
                return payload.decode() if text else json.loads(payload)
        except httpx.HTTPError:
            raise TransportError(0, "TRANSPORT_FAILURE") from None
        finally:
            self.duration += monotonic() - started

    def telemetry(self) -> EnvironmentTelemetry:
        return EnvironmentTelemetry(
            external_calls=self.calls, transport_duration_seconds=self.duration
        )

    async def aclose(self) -> None:
        self.panel_auth = None
        self.secret_values.clear()
        if self._owns_client:
            await self.client.aclose()
