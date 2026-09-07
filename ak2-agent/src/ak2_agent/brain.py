from __future__ import annotations

import json
from functools import wraps
import os
import shutil
import tempfile
import time

from openai_codex import ApprovalMode, Codex, CodexConfig, CodexError, Sandbox
from openai_codex.types import ReasoningEffort

from .files import ROOT, atomic, dumps, emit
from .budget import check_deadline, budget_status
from .models import Adaptation, Decision, FailureReflection, HistoryPreparation, RetrospectiveDecision, LessonReview, Route, schema
from .context import ContextView, deduplicate_context, wire_size
from .recovery import ModelResponseError

OVERRIDES = ('model_provider="openai"', 'web_search="disabled"', "mcp_servers={}",
    *tuple("features." + f + "=false" for f in (
        "shell_tool", "apps", "plugins", "remote_plugin", "browser_use", "computer_use",
        "multi_agent", "hooks", "apply_patch_freeform", "js_repl", "memory_tool",
        "search_tool", "standalone_web_search", "tool_suggest", "goals", "unified_exec", "shell_snapshot")))


def isolated_codex(workspace):
    """Empty MCP tables merge with inherited config; explicitly disable every server."""
    def start(overrides):
        return Codex(CodexConfig(codex_bin=os.getenv("CODEX_BIN") or shutil.which("codex"),
                                cwd=workspace, config_overrides=overrides))
    def servers(client):
        # SDK 0.147 exposes this app-server method through its generic RPC transport.
        response = client._client._request_raw("config/read", {"includeLayers": False})
        if not isinstance(response, dict) or not isinstance(response.get("config"), dict):
            raise RuntimeError("Cannot verify Codex tool isolation")
        entries = response["config"].get("mcp_servers", {})
        if not isinstance(entries, dict):
            raise RuntimeError("Unexpected Codex MCP configuration")
        return entries
    client = start(OVERRIDES)
    try:
        configured = servers(client)
        if configured:
            # Quote server IDs inside TOML, not in the CLI's dotted key parser.
            disabled = ",".join(f"{json.dumps(name)}={{enabled=false}}" for name in configured)
            overrides = OVERRIDES + ("mcp_servers={" + disabled + "}",)
            client.close()
            client = start(overrides)
        if any(not isinstance(value, dict) or value.get("enabled", True) for value in servers(client).values()):
            raise RuntimeError("Codex still exposes inherited MCP servers")
        return client
    except BaseException:
        client.close()
        raise


def validated_response(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ModelResponseError:
            raise
        except ValueError as error:
            raise ModelResponseError(f"Invalid model response: {error}") from error
    return wrapped


class Brain:
    def __init__(self, model="gpt-5.6-sol", effort="low", *, scratch=None, authenticate=True):
        if os.getenv("OPENAI_API_KEY") or os.getenv("CODEX_API_KEY"):
            raise ValueError("Unset OPENAI_API_KEY and CODEX_API_KEY; use ./run.sh")
        if scratch:
            scratch.mkdir(parents=True, exist_ok=True)
        self.workspace = tempfile.TemporaryDirectory(prefix="ak2-brain-", dir=scratch)
        self.model, self.effort, self.usage = model, effort, {}
        self.codex = None
        try:
            self.codex = isolated_codex(self.workspace.name)
            if authenticate:
                self.check_auth()
        except BaseException:
            self.close()
            raise

    def check_auth(self):
        account = self.codex.account().account
        if account is None or account.root.type != "chatgpt":
            raise RuntimeError("ChatGPT subscription login required: ./run.sh --login")

    def close(self):
        try:
            if self.codex:
                self.codex.close()
        finally:
            self.workspace.cleanup()

    def reconnect(self):
        """Replace a broken SDK transport without changing model or scratch space."""
        previous, self.codex = self.codex, None
        if previous:
            try:
                previous.close()
            except Exception:
                pass
        try:
            self.codex = isolated_codex(self.workspace.name)
            self.check_auth()
        except (ConnectionError, TimeoutError) as error:
            raise CodexError(f"SDK connection error: {error}") from error

    @validated_response
    def call(self, prompt, context, output=None, *, review=False):
        for attempt in range(3):
            check_deadline(getattr(self, "deadline_at", None))
            try:
                return self._call_once(prompt, context, output, review=review)
            except (ConnectionError, TimeoutError, BrokenPipeError) as error:
                raise CodexError(f"SDK connection error: {error}") from error
            except (CodexError, RuntimeError) as error:
                temporary = any(message in str(error).lower() for message in
                    ("at capacity", "temporarily unavailable", "server overloaded", "server busy"))
                if str(error).startswith("Context reading budget exhausted"):
                    raise ModelResponseError(str(error)) from error
                if not temporary or attempt == 2:
                    raise
                emit("model_retry", model=self.model, attempt=attempt+2, reason="temporary_capacity")
                time.sleep(2**attempt)

    def _call_once(self, prompt, context, output=None, *, review=False):
        instructions = (ROOT / "prompts" / (prompt + ".md")).read_text()
        if prompt == "adapt":
            instructions += "\n\n" + (ROOT / "prompts/contract.md").read_text()
        if prompt == "history_prepare":
            instructions += "\n\n" + (ROOT / "prompts/history-contract.md").read_text()
        prepared = deduplicate_context(context) if prompt == "decide" else context
        # Keep reference documents available on demand even for small observations.
        references = any(p.startswith("references/") for p in context.get("knowledge", {}))
        paged = wire_size(prepared) > 120000 or references
        view = ContextView(context)
        preview = view.decision_preview()
        if paged:
            atomic(os.path.join(self.workspace.name, "context.json"), dumps(context))
            instructions += "\n\n" + (ROOT / "prompts/context-read.md").read_text()
        result_schema = schema(output) if output else {"type": "string"}
        response_schema = result_schema
        if paged:
            result_schema = dict(result_schema)
            definitions = result_schema.pop("$defs", {})
            response_schema = {"type": "object", "additionalProperties": False,
                "properties": {
                    "context_read": {"anyOf": [{"type": "null"}, {"type": "object",
                        "additionalProperties": False, "properties": {
                            "pointer": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                            "notes": {"type": "string", "maxLength": 8000}},
                        "required": ["pointer", "offset", "notes"]}]},
                    "result": {"anyOf": [{"type": "null"}, result_schema]}},
                "required": ["context_read", "result"], **({"$defs": definitions} if definitions else {})}
        if paged:
            variants = response_schema["properties"]["context_read"]["anyOf"]
            variants.append({"type": "array", "items": variants[1], "minItems": 1, "maxItems": 6})
        effort = self.effort
        started, counts, page, notes = time.monotonic(), {}, None, ""
        total_chars, pages = 0, []
        for read_number in range(25):
            check_deadline(getattr(self, "deadline_at", None))
            request = ({"context_preview": preview, "context_page": page,
                        "retained_pages": pages[:-1], "reading_notes": notes, "remaining_reads": 24-read_number}
                       if paged else prepared)
            deadline = getattr(self, "deadline_at", None)
            if deadline is not None:
                request = {**request, "time_budget": budget_status({"deadline_at": deadline})}
            # Validate before ANY SDK call; includes schema and instructions, with
            # a generous margin below the server's 1,048,576-character limit.
            if wire_size([request, instructions, response_schema]) > 800000:
                raise RuntimeError("Context envelope exceeds safe input budget; checkpoint preserved")
            emit("brain_request", phase=prompt, model=self.model, effort=effort,
                 context_chars=len(dumps(request)), context_read=read_number, paged=paged)
            # Fresh contexts prevent both cross-world leakage and accumulation of pages.
            thread = self.codex.thread_start(model=self.model, model_provider="openai",
                cwd=self.workspace.name, ephemeral=True, sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all, developer_instructions=instructions)
            result = thread.run(dumps(request), effort=ReasoningEffort(effort),
                **({"output_schema": response_schema} if output or paged else {}))
            total_chars += len(dumps(request))
            if result.usage:
                for k,v in result.usage.total.model_dump(mode="json").items():
                    if isinstance(v, (int, float)):
                        key = k.removesuffix("_tokens")
                        counts[key] = counts.get(key, 0) + v
            self.usage = {"seconds": round(time.monotonic()-started, 2), "model": self.model,
                "effort": effort, "context_chars": len(dumps(request)),
                "original_context_chars": len(dumps(context)), "total_context_chars": total_chars,
                "context_reads": read_number, "counts": counts or None}
            if str(getattr(result.status, "value", result.status)) != "completed":
                raise RuntimeError(f"Codex: {result.status}; {result.error}")
            for item in result.items:
                kind = getattr(getattr(item, "root", item), "type", None)
                if kind not in ("userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"):
                    raise RuntimeError(f"Unexpected Codex tool event: {kind}")
            if not result.final_response or not result.final_response.strip():
                raise ModelResponseError("Empty Codex response")
            if not paged:
                return output.model_validate_json(result.final_response).model_dump() if output else result.final_response
            reply = json.loads(result.final_response)
            if not isinstance(reply, dict) or set(reply) != {"context_read", "result"}:
                raise ValueError("Expected context_read/result envelope")
            read = reply["context_read"]
            if read is None:
                if reply["result"] is None:
                    raise ValueError("Expected a final result or a context read")
                if output:
                    return output.model_validate(reply["result"]).model_dump()
                if not isinstance(reply["result"], str):
                    raise ValueError("Expected a text answer")
                return reply["result"]
            reads = read if isinstance(read, list) else [read]
            if reply["result"] is not None or not 1 <= len(reads) <= 6:
                raise ValueError("Context reads must be separate from decisions; at most six per batch")
            if read_number == 24:
                raise RuntimeError("Context reading budget exhausted; checkpoint preserved")
            for read in reads:
                if not isinstance(read, dict) or set(read) != {"pointer", "offset", "notes"} or not isinstance(read["notes"], str) or len(read["notes"]) > 8000:
                    raise ValueError("Invalid context read or oversized reading notes")
                notes = read["notes"]
                try:
                    page = view.read(read["pointer"], read["offset"])
                except (ValueError, KeyError, IndexError) as error:
                    page = {"error": str(error)[:500], "complete": False}
                pages = [p for p in pages if (p.get("pointer"), p.get("offset")) != (page.get("pointer"), page.get("offset"))]
                pages.append(page)
                while len(pages) > 1 and wire_size(pages) > 160000:
                    pages.pop(0)
                emit("context_read", phase=prompt, pointer=read["pointer"], offset=read["offset"],
                     next_offset=page.get("next_offset"), error=page.get("error"))
        raise RuntimeError("Context reading budget exhausted; checkpoint preserved")

    def route(self, task, catalog):
        return self.call("route", {"task": task, "worlds": catalog}, Route)

    @validated_response
    def adapt(self, context):
        result = self.call("adapt", context, Adaptation)
        reason = result.get("pause_reason")
        if reason is not None and (not reason.strip() or result["ready"] or result["files"] or result.get("patches")):
            raise ValueError("Adaptation pause needs a reason and must be separate from edits or publication")
        return result

    def verify_lesson(self, context):
        return self.call("verify_lesson", context, LessonReview, review=True)

    def reflect_failure(self, context):
        return self.call("failure_reflection", context, FailureReflection, review=True)

    def prepare_history(self, context):
        return self.call("history_prepare", context, HistoryPreparation)

    @validated_response
    def retrospect(self, context):
        result = self.call("retrospect", context, RetrospectiveDecision, review=True)
        if result["query"] and not context["can_read"]:
            raise ValueError("Historical read budget exhausted; finish the retrospective")
        if result["query"] and result["query"]["source_id"] not in ({s["id"] for s in context["catalog"]["sources"]} | {"__local_events__"}):
            raise ValueError("Unknown historical source")
        return result

    @validated_response
    def decide(self, context):
        result = self.call("decide", context, Decision, review=context.get("review_mode", False))
        if result.get("adaptation_request") and not context.get("can_adapt", False):
            raise ValueError("Knowledge is frozen; adaptation is unavailable in this run")
        if result.get("adaptation_request") and result["actions"]:
            raise ValueError("Request interface adaptation separately from world actions")
        pause = result.get("pause_reason")
        if pause is not None and not pause.strip():
            raise ValueError("Pause needs a concrete reason")
        if pause and (result["actions"] or result.get("adaptation_request") or context["final_reflection"]):
            raise ValueError("Pause must be separate from actions, adaptation and final reflection")
        if not result["actions"] and not context["final_reflection"] and not result.get("adaptation_request") and not pause:
            raise ValueError("Expected actions unless world is complete")
        if context["final_reflection"] and result["actions"]:
            raise ValueError("Completed world must receive reflection only")
        if sum(a["repeat_count"] for a in result["actions"]) > 64:
            raise ValueError("At most 64 executions per decision")
        for a in result["actions"]:
            if not isinstance(json.loads(a["payload_json"]), dict):
                raise ValueError("payload_json must be an object")
            if a["repeat_count"] > 1 and a["wait_for_completion"]:
                raise ValueError("Repeated actions must be independent")
        return result

    def explain(self, question, context):
        return self.call("explain", {"question": question, **context})
