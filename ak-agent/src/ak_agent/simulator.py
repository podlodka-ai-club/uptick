"""A small transport adapter. The agent supplies every control action."""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urlencode, urlparse

from .memory import dumps


def redact(value):
    if isinstance(value, dict):
        return {k: ("[private]" if k.lower() in ("password", "target_auth", "control_panel_auth", "authorization") else redact(v)) for k,v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)((?:password|пароль)\s*[:=]\s*)[^\s,;]+", r"\1[private]", value)
    return value


class Simulator:
    def __init__(self, origin, memory, state):
        self.origin, self.memory, self.state = origin.rstrip("/"), memory, state
        parsed = urlparse(self.origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            raise ValueError("API origin must be http(s) without credentials")

    def request(self, method, path, payload=None, query=None, auth=False, request_id=None):
        if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Invalid API path")
        if method == "POST":
            if not request_id:
                raise ValueError("Mutations require a persistent request_id")
            payload = {**(payload or {}), "request_id": request_id}
            # Journal excludes transient target passwords; idempotency excludes them too.
            saved = self.memory.prepare(request_id, self.state["local_id"],
                {"method": method, "path": path, "payload": redact(payload)})
            if saved is not None:
                return saved
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            pair = self.state["auth"]
            headers["Authorization"] = "Basic " + base64.b64encode(
                f"{pair['username']}:{pair['password']}".encode()).decode()
        url = self.origin + path + ("?" + urlencode(query) if query else "")
        data = dumps(payload).encode() if payload is not None else None
        target = urlparse(url)
        for attempt in range(3):
            cls = HTTPSConnection if target.scheme == "https" else HTTPConnection
            connection = cls(target.hostname, target.port, timeout=4 if method == "GET" else 60)
            try:
                # Read one complete JSON object; never treat a partial response as success.
                connection.request(method, target.path + ("?" + target.query if target.query else ""), body=data, headers=headers)
                r = connection.getresponse()
                status, body = r.status, self.read_json(r)
                if (status in (429, 502, 504) or body.get("error") == "CONCURRENT_RUN_REQUEST") and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                result = {**body, "http_status": status}
                break
            except (OSError, HTTPException) as e:
                if attempt == 2:
                    raise RuntimeError(f"Simulator network error: {method} {path}: {e}. Checkpoint saved; use resume.") from e
                time.sleep(1 + attempt)
            finally:
                connection.close()
        if method == "POST":
            self.memory.received(request_id, result)
        return result

    @staticmethod
    def read_json(response):
        """Read one complete API JSON object without waiting for an idle connection tail."""
        raw = bytearray()
        while len(raw) <= 8 * 1024 * 1024:
            chunk = response.read1(65536)
            if not chunk:
                break
            raw.extend(chunk)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(body, dict):
                raise HTTPException("API response must be a JSON object")
            return body
        raise HTTPException("Incomplete or oversized API JSON response")

    def get(self, path, **query):
        try:
            return self.request("GET", self.base + "/" + path, query=query or None,
                                auth=path == "control/commands" or path.startswith("credentials/"))
        except RuntimeError as e:
            # Missing telemetry is explicit evidence, never an empty healthy snapshot.
            return {"http_status":599, "error":"TRANSPORT_UNAVAILABLE", "message":str(e)}

    @property
    def base(self):
        return "/v2/runs/" + self.state["run_id"]

    def execute(self, action, request_id):
        kind, name, params = action["kind"], action["name"], json.loads(action["params_json"])
        if kind == "read":
            if not re.fullmatch(r"(?:overview|metrics|logs|resources|inbox|control/commands|operations/[A-Za-z0-9]+)", name):
                raise ValueError("Read endpoint not allowed")
            return self.get(name, **params)
        if kind == "command":
            catalog = {x["command"]: x for x in self.state["catalog"]["commands"]}
            if name not in catalog:
                raise ValueError("Unknown command: " + name)
            schema = catalog[name]["params_schema"]
            if not set(schema.get("required", [])) <= params.keys():
                raise ValueError("Missing required command params: " + dumps(schema.get("required")))
            if schema.get("additionalProperties") is False and params.keys() - schema.get("properties", {}).keys():
                raise ValueError("Unknown command params")
            payload = {"command": name, "params": params}
            if catalog[name]["target_auth_required"]:
                cid = action.get("credential_id")
                if not cid or not re.fullmatch(r"[\w.:-]{1,128}", cid):
                    raise ValueError("Command requires credential_id of target server")
                secret = self.get("credentials/" + cid)
                if secret["http_status"] >= 400:
                    return redact(secret)
                credential = secret["credential"]
                now = secret.get("clock", {}).get("simulation_time")
                expires = credential.get("expires_at")
                if now and expires and datetime.fromisoformat(now.replace("Z", "+00:00")) >= datetime.fromisoformat(expires.replace("Z", "+00:00")):
                    # Refresh only the SAME target's transport credential. This also lets
                    # an accepted-but-unacknowledged command replay after a long restart.
                    resources = self.get("resources")
                    current = next((s for s in resources.get("servers", [])
                                    if s["server_id"] == credential["resource_id"]), None)
                    if current and current["credential_id"] != cid:
                        fresh = self.get("credentials/" + current["credential_id"])
                        if fresh["http_status"] < 400:
                            credential = fresh["credential"]
                payload["target_auth"] = {k: credential[k] for k in ("username", "password")}
            elif action.get("credential_id"):
                raise ValueError("This command does not accept target credentials")
            return self.request("POST", self.base + "/control/commands", payload,
                                auth=True, request_id=request_id)
        if kind == "advance":
            return self.request("POST", self.base + "/time/advance", params, request_id=request_id)
        if kind == "probe":
            return self.request("POST", self.base + "/probes", params, request_id=request_id)
        raise ValueError("Unknown action kind")
