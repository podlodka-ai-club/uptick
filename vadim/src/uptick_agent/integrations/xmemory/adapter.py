"""Bounded integration for the public HU-xiaobai/xMemory facade.

The research library is deliberately injected through a small protocol.  This
module owns the translation to Vadim's memory contracts, but it does not
import the research library, read credentials, or claim snapshot/export
support.  The external service's own natural-language extraction is not a
replacement for Vadim's evidence validation.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic_core import PydanticSerializationError

from uptick_agent.memory.audit_contracts import AuditTraceEvent, AuditTraceSink, AuditTraceWrite
from uptick_agent.memory.compatibility.contracts import MemoryEntry
from uptick_agent.memory.config import MemoryConfiguration
from uptick_agent.memory.contracts import (
    ContextItem,
    CreatedMemoryItem,
    DecisionMemoryContext,
    ExperienceTransition,
    MemoryConflictError,
    MemoryContextRequest,
    MemoryContribution,
    MemoryPermanentError,
    MemoryTransientError,
    MemoryValidationError,
    ProvenanceRef,
    RunOutcome,
    UntrustedMemoryEnvelope,
)
from uptick_agent.memory.orchestrator import MemoryModuleRegistration, MemoryOrchestrator
from uptick_agent.memory.stores.contracts import (
    RecordWrite,
    StoredRecord,
    StructuredMemoryStore,
    canonical_json,
    sha256_json,
    validate_identifier,
    validate_namespace,
)
from uptick_agent.redaction import redact_text, sanitize_json

XMEMORY_MODULE_ID = "xmemory"
XMEMORY_MODULE_VERSION = "1.0"
_INTENT_RECORD_TYPE = "xmemory-write-intent"
_RECEIPT_RECORD_TYPE = "xmemory-write-receipt"
_FINALIZE_INTENT_RECORD_TYPE = "xmemory-finalize-intent"
_FINALIZE_RECEIPT_RECORD_TYPE = "xmemory-finalize-receipt"
_JOURNAL_OPERATION = "xmemory-write-journal"
_MAX_IDENTIFIER_LENGTH = 256
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,255}$")
_MISSING = object()


@runtime_checkable
class XMemoryFacade(Protocol):
    """The dependency-free subset of the HU public ``xMemory`` facade."""

    def add_messages(self, user_id: str, messages: Sequence[dict[str, Any]]) -> dict[str, Any]: ...

    def flush(self, user_id: str) -> dict[str, Any] | None: ...

    def wait_for_semantic(self, user_id: str, timeout: float = 30.0) -> bool: ...

    def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k_episodes: int | None = None,
        top_k_semantic: int | None = None,
        search_method: str = "hybrid",
    ) -> dict[str, list[dict[str, Any]]]: ...


class XMemoryModule:
    """Implement the native memory-module capabilities for one xMemory scope.

    ``namespace`` identifies the local journal and ``user_id`` identifies the
    xMemory owner.  Both are fixed for the module lifetime; they are never
    derived from a request or transition.  The module intentionally has no
    clear/delete operation because the public facade does not provide one.
    """

    def __init__(
        self,
        facade: XMemoryFacade,
        store: StructuredMemoryStore,
        *,
        namespace: str,
        user_id: str,
        module_version: str = XMEMORY_MODULE_VERSION,
        top_k: int = 8,
        semantic_timeout: float = 30.0,
        ownership: Literal["owned", "borrowed"] = "borrowed",
        phase: Literal["training", "evaluation"] = "training",
        read_only: bool = False,
    ) -> None:
        if not isinstance(facade, XMemoryFacade):
            raise MemoryValidationError("xmemory facade does not implement the public protocol")
        if not all(callable(getattr(store, name, None)) for name in ("append", "get", "list")):
            raise MemoryValidationError("xmemory integration requires a structured memory store")
        self._namespace = validate_namespace(namespace)
        self._user_id = validate_identifier(
            user_id,
            name="xmemory user_id",
            max_length=_MAX_IDENTIFIER_LENGTH,
        )
        if _SAFE_USER_ID.fullmatch(self._user_id) is None:
            raise MemoryValidationError("xmemory user_id must be a safe path-free identifier")
        if not module_version or len(module_version) > 64:
            raise MemoryValidationError("xmemory module_version must contain 1-64 characters")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 0 <= top_k <= 1_000:
            raise MemoryValidationError("xmemory top_k must be an integer between 0 and 1000")
        if not isinstance(semantic_timeout, (int, float)) or isinstance(semantic_timeout, bool):
            raise MemoryValidationError("xmemory semantic_timeout must be a finite number")
        if not math.isfinite(float(semantic_timeout)) or semantic_timeout < 0:
            raise MemoryValidationError("xmemory semantic_timeout must be non-negative")
        if ownership not in ("owned", "borrowed"):
            raise MemoryValidationError("xmemory ownership must be 'owned' or 'borrowed'")
        if phase == "evaluation" or read_only:
            raise MemoryValidationError(
                "xmemory integration does not support evaluation or read-only phases"
            )
        if ownership == "owned" and not callable(getattr(facade, "close", None)):
            raise MemoryValidationError("owned xmemory facade must provide close()")
        self._facade = facade
        self._store = store
        self._module_version = module_version
        self._top_k = top_k
        self._semantic_timeout = float(semantic_timeout)
        self._ownership = ownership
        self._closed = False

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def module_version(self) -> str:
        return self._module_version

    async def retrieve(self, request: MemoryContextRequest) -> MemoryContribution:
        """Search with a bounded per-kind top-k and normalize hits as untrusted."""

        self._ensure_open()
        if not isinstance(request, MemoryContextRequest):
            raise MemoryValidationError("xmemory retrieval requires MemoryContextRequest")
        limit = self._top_k
        if request.max_items is not None:
            limit = min(limit, request.max_items)
        query = redact_text(request.query)
        if limit == 0 or not query.strip():
            return MemoryContribution(
                module_id=XMEMORY_MODULE_ID,
                module_version=self._module_version,
                warnings=["xmemory search was skipped because the query or item budget was empty"],
            )

        try:
            raw = await asyncio.to_thread(
                self._facade.search,
                self._user_id,
                query,
                top_k_episodes=limit,
                top_k_semantic=limit,
                search_method="hybrid",
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionError, OSError) as error:
            raise MemoryTransientError("xmemory search failed transiently") from error
        except Exception as error:
            raise MemoryPermanentError("xmemory search failed") from error

        try:
            safe = sanitize_json(raw)
            if not isinstance(safe, Mapping):
                raise TypeError("search response must be an object")
            items: list[ContextItem] = []
            for kind in ("episodic", "semantic"):
                hits = safe.get(kind, [])
                if not isinstance(hits, list):
                    raise TypeError(f"search response field {kind!r} must be a list")
                for index, hit in enumerate(hits[:limit]):
                    if not isinstance(hit, Mapping):
                        raise TypeError("search hits must be objects")
                    safe_hit = dict(cast(Mapping[str, Any], hit))
                    items.append(self._context_item(kind, index, safe_hit))
            items.sort(key=lambda item: (-item.score, item.envelope.item_id))
            items = items[:limit]
        except (PydanticSerializationError, TypeError, ValueError) as error:
            raise MemoryPermanentError("xmemory search returned invalid data") from error

        warnings: list[str] = []
        if not items:
            # The HU implementation catches upstream errors and returns empty
            # collections.  Empty search is therefore never a health signal.
            warnings.append("xmemory empty search is not authoritative health evidence")
        return MemoryContribution(
            module_id=XMEMORY_MODULE_ID,
            module_version=self._module_version,
            items=items,
            warnings=warnings,
        )

    async def record(
        self,
        transition: ExperienceTransition,
        *,
        idempotency_key: str,
    ) -> list[CreatedMemoryItem]:
        """Persist intent, perform one unsafe write, then persist its receipt.

        A pending intent is treated as potentially submitted after any process
        interruption or facade exception.  It is never retried automatically.
        """

        self._ensure_open()
        owned = self._validate_transition(transition)
        key = validate_identifier(
            idempotency_key,
            name="xmemory idempotency_key",
            max_length=_MAX_IDENTIFIER_LENGTH,
        )
        payload = sanitize_json(owned.model_dump(mode="json"))
        if not isinstance(payload, dict):
            raise MemoryValidationError("xmemory transition payload must be an object")
        message = self._message_for_transition(payload, owned.occurred_at)
        input_hash = sha256_json(
            {
                "namespace": self._namespace,
                "user_id": self._user_id,
                "message": message,
            }
        )
        intent_id = self._journal_id("intent", key, scope="write")
        receipt_id = self._journal_id("receipt", key, scope="write")
        owner_token = uuid.uuid4().hex

        existing_receipt = await self._store.get(
            namespace=self._namespace,
            record_id=receipt_id,
        )
        if existing_receipt is not None:
            return [self._read_receipt(existing_receipt, key=key, input_hash=input_hash)]

        existing_intent = await self._store.get(
            namespace=self._namespace,
            record_id=intent_id,
        )
        if existing_intent is not None:
            self._check_intent(existing_intent, key=key, input_hash=input_hash)
            raise MemoryConflictError("xmemory write intent is pending; refusing an unsafe retry")

        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=intent_id,
                    record_type=_INTENT_RECORD_TYPE,
                    payload={
                        "idempotency_key": key,
                        "input_hash": input_hash,
                        "user_id": self._user_id,
                        "state": "pending",
                        "owner_token": owner_token,
                    },
                    created_at=datetime.now(UTC),
                ),
                operation=_JOURNAL_OPERATION,
                idempotency_key=intent_id,
            )
        except MemoryConflictError as error:
            # A concurrent caller may have won the intent race.  Re-read and
            # refuse rather than risking a second external submission.
            existing_intent = await self._store.get(
                namespace=self._namespace,
                record_id=intent_id,
            )
            if existing_intent is None:
                raise
            self._check_intent(existing_intent, key=key, input_hash=input_hash)
            raise MemoryConflictError(
                "xmemory write intent is pending; refusing an unsafe retry"
            ) from error

        persisted_intent = await self._store.get(
            namespace=self._namespace,
            record_id=intent_id,
        )
        if persisted_intent is None:
            raise MemoryPermanentError("xmemory write intent disappeared before submission")
        self._check_intent(
            persisted_intent,
            key=key,
            input_hash=input_hash,
            owner_token=owner_token,
        )

        messages = [message]
        try:
            raw_response = await asyncio.to_thread(
                self._facade.add_messages,
                self._user_id,
                messages,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The call may have reached the external writer.  This error is
            # deliberately non-transient so MemoryOrchestrator cannot retry.
            raise MemoryConflictError(
                "xmemory write outcome is ambiguous; retry is refused"
            ) from error

        try:
            if not isinstance(raw_response, Mapping):
                raise TypeError("write response must be an object")
            status = raw_response.get("status")
            messages_added = raw_response.get("messages_added")
            if status != "success":
                raise ValueError("write response did not report success")
            if messages_added != len(messages) or isinstance(messages_added, bool):
                raise ValueError("write response did not confirm the submitted message")
            safe_response = self._normalize_write_response(raw_response)
        except (PydanticSerializationError, TypeError, ValueError) as error:
            raise MemoryPermanentError("xmemory write returned invalid data") from error

        receipt_payload = {
            "idempotency_key": key,
            "input_hash": input_hash,
            "user_id": self._user_id,
            "response": safe_response,
            "item_id": receipt_id,
        }
        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=receipt_id,
                    record_type=_RECEIPT_RECORD_TYPE,
                    payload=receipt_payload,
                    created_at=datetime.now(UTC),
                ),
                operation=_JOURNAL_OPERATION,
                idempotency_key=receipt_id,
            )
        except MemoryConflictError:
            existing_receipt = await self._store.get(
                namespace=self._namespace,
                record_id=receipt_id,
            )
            if existing_receipt is None:
                raise
            return [self._read_receipt(existing_receipt, key=key, input_hash=input_hash)]
        return [self._created_item(receipt_id, safe_response)]

    async def finalize(self, outcome: RunOutcome, *, idempotency_key: str) -> None:
        """Flush buffered messages and require semantic generation to finish.

        Finalization has the same durable intent barrier as writes.  A failed
        or interrupted flush remains pending and is never repeated
        automatically because the facade does not expose idempotent flush
        semantics.
        """

        self._ensure_open()
        if not isinstance(outcome, RunOutcome):
            raise MemoryValidationError("xmemory finalization requires RunOutcome")
        key = validate_identifier(
            idempotency_key,
            name="xmemory finalization idempotency_key",
            max_length=_MAX_IDENTIFIER_LENGTH,
        )
        outcome_payload = sanitize_json(outcome.model_dump(mode="json"))
        if not isinstance(outcome_payload, dict):
            raise MemoryValidationError("xmemory finalization payload must be an object")
        input_hash = sha256_json(
            {
                "namespace": self._namespace,
                "user_id": self._user_id,
                "outcome": outcome_payload,
                "semantic_timeout": self._semantic_timeout,
            }
        )
        intent_id = self._journal_id("intent", key, scope="finalize")
        receipt_id = self._journal_id("receipt", key, scope="finalize")
        owner_token = uuid.uuid4().hex

        existing_receipt = await self._store.get(
            namespace=self._namespace,
            record_id=receipt_id,
        )
        if existing_receipt is not None:
            self._read_finalize_receipt(existing_receipt, key=key, input_hash=input_hash)
            return
        existing_intent = await self._store.get(
            namespace=self._namespace,
            record_id=intent_id,
        )
        if existing_intent is not None:
            self._check_intent(
                existing_intent,
                key=key,
                input_hash=input_hash,
                record_type=_FINALIZE_INTENT_RECORD_TYPE,
            )
            raise MemoryConflictError("xmemory finalization intent is pending; retry refused")

        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=intent_id,
                    record_type=_FINALIZE_INTENT_RECORD_TYPE,
                    payload={
                        "idempotency_key": key,
                        "input_hash": input_hash,
                        "user_id": self._user_id,
                        "state": "pending",
                        "owner_token": owner_token,
                    },
                    created_at=datetime.now(UTC),
                ),
                operation=_JOURNAL_OPERATION,
                idempotency_key=intent_id,
            )
        except MemoryConflictError as error:
            existing_intent = await self._store.get(
                namespace=self._namespace,
                record_id=intent_id,
            )
            if existing_intent is None:
                raise
            self._check_intent(
                existing_intent,
                key=key,
                input_hash=input_hash,
                record_type=_FINALIZE_INTENT_RECORD_TYPE,
            )
            raise MemoryConflictError(
                "xmemory finalization intent is pending; retry refused"
            ) from error

        persisted_intent = await self._store.get(
            namespace=self._namespace,
            record_id=intent_id,
        )
        if persisted_intent is None:
            raise MemoryPermanentError("xmemory finalization intent disappeared before flush")
        self._check_intent(
            persisted_intent,
            key=key,
            input_hash=input_hash,
            owner_token=owner_token,
            record_type=_FINALIZE_INTENT_RECORD_TYPE,
        )

        try:
            await asyncio.to_thread(self._facade.flush, self._user_id)
            completed = await asyncio.to_thread(
                self._facade.wait_for_semantic,
                self._user_id,
                self._semantic_timeout,
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, ConnectionError, OSError) as error:
            raise MemoryPermanentError("xmemory finalization outcome is ambiguous") from error
        except Exception as error:
            raise MemoryPermanentError("xmemory finalization failed") from error
        if completed is not True:
            raise MemoryPermanentError(
                "xmemory semantic generation did not complete before the observed timeout"
            )

        try:
            await self._store.append(
                RecordWrite(
                    namespace=self._namespace,
                    record_id=receipt_id,
                    record_type=_FINALIZE_RECEIPT_RECORD_TYPE,
                    payload={
                        "idempotency_key": key,
                        "input_hash": input_hash,
                        "user_id": self._user_id,
                        "status": "completed",
                        "observed_flush": True,
                        "observed_semantic_completion": True,
                    },
                    created_at=datetime.now(UTC),
                ),
                operation=_JOURNAL_OPERATION,
                idempotency_key=receipt_id,
            )
        except MemoryConflictError as error:
            existing_receipt = await self._store.get(
                namespace=self._namespace,
                record_id=receipt_id,
            )
            if existing_receipt is None:
                raise MemoryPermanentError(
                    "xmemory finalization completed but its receipt is unavailable"
                ) from error
            self._read_finalize_receipt(existing_receipt, key=key, input_hash=input_hash)
        except (MemoryTransientError, MemoryPermanentError) as error:
            raise MemoryPermanentError(
                "xmemory finalization completed but its receipt could not be persisted"
            ) from error

    def close(self) -> None:
        """Close an owned facade; borrowed facades remain caller-owned."""

        if self._closed:
            return
        self._closed = True
        if self._ownership == "owned":
            close = getattr(self._facade, "close", None)
            if not callable(close):
                raise MemoryPermanentError("owned xmemory facade lost its close() method")
            close()

    def __enter__(self) -> XMemoryModule:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _validate_transition(transition: object) -> ExperienceTransition:
        if not isinstance(transition, ExperienceTransition):
            raise MemoryValidationError("xmemory record requires ExperienceTransition")
        try:
            owned = ExperienceTransition.model_validate(
                transition.model_dump(mode="python", round_trip=True, warnings="error")
            )
            sanitize_json(owned.model_dump(mode="json"))
        except (PydanticSerializationError, TypeError, ValueError) as error:
            raise MemoryValidationError("xmemory transition contains invalid data") from error
        if owned.occurred_at.utcoffset() is None:
            raise MemoryValidationError("xmemory transition timestamp must include a timezone")
        return owned.model_copy(update={"occurred_at": owned.occurred_at.astimezone(UTC)})

    @staticmethod
    def _message_for_transition(payload: dict[str, Any], occurred_at: datetime) -> dict[str, Any]:
        return {
            "role": "user",
            "content": canonical_json({"experience_transition": payload}),
            "timestamp": occurred_at.astimezone(UTC).isoformat(),
            "metadata": {
                "source": XMEMORY_MODULE_ID,
                "transition_id": payload["transition_id"],
                "run_id": payload["run_id"],
                "iteration": payload["iteration"],
            },
        }

    @staticmethod
    def _journal_id(
        kind: Literal["intent", "receipt"], key: str, *, scope: Literal["write", "finalize"]
    ) -> str:
        digest = sha256_json({"kind": kind, "scope": scope, "idempotency_key": key})
        return f"xmemory-{scope}-{kind}-{digest}"

    @staticmethod
    def _check_intent(
        record: StoredRecord,
        *,
        key: str,
        input_hash: str,
        owner_token: str | None = None,
        record_type: str = _INTENT_RECORD_TYPE,
    ) -> None:
        if record.record_type != record_type or not isinstance(record.payload, dict):
            raise MemoryPermanentError("xmemory journal intent record is invalid")
        if (
            record.payload.get("idempotency_key") != key
            or record.payload.get("input_hash") != input_hash
        ):
            raise MemoryConflictError("xmemory idempotency key was reused with different input")
        if record.payload.get("state") != "pending" or not isinstance(
            record.payload.get("owner_token"), str
        ):
            raise MemoryPermanentError("xmemory journal intent has an unknown state")
        if owner_token is not None and record.payload["owner_token"] != owner_token:
            raise MemoryConflictError("another caller owns the xmemory write intent")

    @classmethod
    def _read_finalize_receipt(cls, record: StoredRecord, *, key: str, input_hash: str) -> None:
        if record.record_type != _FINALIZE_RECEIPT_RECORD_TYPE or not isinstance(
            record.payload, dict
        ):
            raise MemoryPermanentError("xmemory finalization receipt record is invalid")
        if (
            record.payload.get("idempotency_key") != key
            or record.payload.get("input_hash") != input_hash
        ):
            raise MemoryConflictError("xmemory finalization key was reused with different input")
        if (
            record.payload.get("status") != "completed"
            or record.payload.get("observed_flush") is not True
            or record.payload.get("observed_semantic_completion") is not True
        ):
            raise MemoryPermanentError("xmemory finalization receipt payload is invalid")

    def _ensure_open(self) -> None:
        if self._closed:
            raise MemoryPermanentError("xmemory module is closed")

    @classmethod
    def _read_receipt(cls, record: StoredRecord, *, key: str, input_hash: str) -> CreatedMemoryItem:
        if record.record_type != _RECEIPT_RECORD_TYPE or not isinstance(record.payload, dict):
            raise MemoryPermanentError("xmemory journal receipt record is invalid")
        if (
            record.payload.get("idempotency_key") != key
            or record.payload.get("input_hash") != input_hash
        ):
            raise MemoryConflictError("xmemory idempotency key was reused with different input")
        item_id = record.payload.get("item_id")
        response = record.payload.get("response")
        if not isinstance(item_id, str) or not item_id or not isinstance(response, dict):
            raise MemoryPermanentError("xmemory journal receipt payload is invalid")
        return cls._created_item(item_id, response)

    @staticmethod
    def _created_item(item_id: str, response: dict[str, Any]) -> CreatedMemoryItem:
        safe_response = sanitize_json(response)
        if not isinstance(safe_response, dict):
            raise MemoryPermanentError("xmemory receipt response is invalid")
        content_hash = sha256_json(safe_response)
        return CreatedMemoryItem(
            item_id=item_id,
            artefact_type="xmemory-write-receipt",
            provenance=[ProvenanceRef(artefact_id=item_id, content_hash=content_hash)],
        )

    @classmethod
    def _normalize_write_response(cls, raw_response: Mapping[str, Any]) -> dict[str, Any]:
        """Keep documented primitive result fields and omit HU model objects."""

        normalized: dict[str, Any] = {
            "status": raw_response["status"],
            "messages_added": raw_response["messages_added"],
        }
        fields = (
            "episodes_created",
            "semantic_memories_created",
            "boundary_detections",
            "buffer_size_before",
            "buffer_size_after",
            "semantic_generation_scheduled",
            "semantic_tasks_scheduled",
        )
        for field in fields:
            if field not in raw_response:
                continue
            value = cls._safe_json_value(raw_response[field])
            if value is not _MISSING:
                normalized[field] = value
        return normalized

    @classmethod
    def _safe_json_value(cls, value: object) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, nested in value.items():
                if str(key).endswith("_object"):
                    continue
                safe = cls._safe_json_value(nested)
                if safe is not _MISSING:
                    result[str(key)] = safe
            return sanitize_json(result)
        if isinstance(value, list):
            result = []
            for nested in value:
                safe = cls._safe_json_value(nested)
                if safe is not _MISSING:
                    result.append(safe)
            return result
        try:
            return sanitize_json(value)
        except (TypeError, ValueError):
            return _MISSING

    def _context_item(self, kind: str, index: int, hit: dict[str, Any]) -> ContextItem:
        safe_hit = sanitize_json(hit)
        if not isinstance(safe_hit, dict):
            raise TypeError("search hit must be a JSON object")
        content_hash = sha256_json({"user_id": self._user_id, "kind": kind, "hit": safe_hit})
        item_id = f"xmemory-{kind}-{content_hash}"
        score_value = safe_hit.get("score")
        score = (
            float(score_value)
            if isinstance(score_value, (int, float))
            and not isinstance(score_value, bool)
            and score_value == score_value
            and score_value not in (float("inf"), float("-inf"))
            else 0.0
        )
        return ContextItem(
            envelope=UntrustedMemoryEnvelope(
                item_id=item_id,
                artefact_type=f"xmemory-{kind}",
                origin_module=XMEMORY_MODULE_ID,
                origin_version=self._module_version,
                trust_classification="external_untrusted",
                provenance=[ProvenanceRef(artefact_id=item_id, content_hash=content_hash)],
                item={"kind": kind, "rank": index + 1, "hit": safe_hit},
            ),
            score=score,
            selection_reason=f"xmemory {kind} result",
            estimated_tokens=0,
        )


class XMemoryRuntime:
    """Runner-facing ``AgentMemory`` facade over one xMemory orchestrator."""

    def __init__(self, orchestrator: MemoryOrchestrator, module: XMemoryModule) -> None:
        self._orchestrator = orchestrator
        self._module = module

    @property
    def orchestrator(self) -> MemoryOrchestrator:
        return self._orchestrator

    @property
    def context_diagnostics(self) -> dict[str, Any]:
        return self._orchestrator.last_context_diagnostics.model_dump(mode="json")

    async def build_context(self, request: MemoryContextRequest) -> DecisionMemoryContext:
        return await self._orchestrator.build_context(request)

    async def remember(self, entry: MemoryEntry) -> None:
        """Keep the runner compatibility hook without inventing xMemory writes."""

        del entry
        return None

    async def record_transition(self, transition: ExperienceTransition) -> None:
        await self._orchestrator.record_transition(transition)

    async def clear(self, run_id: str | None = None) -> None:
        del run_id
        raise MemoryPermanentError("xmemory integration cannot clear or delete external data")

    async def finalize_run(self, outcome: RunOutcome) -> None:
        await self._orchestrator.finalize_run(outcome)

    async def record_trace(self, write: AuditTraceWrite) -> AuditTraceEvent | None:
        return await self._orchestrator.record_trace(write)

    def close(self) -> None:
        self._module.close()


def build_xmemory_runtime(
    configuration: MemoryConfiguration,
    facade: XMemoryFacade,
    store: StructuredMemoryStore,
    *,
    namespace: str,
    user_id: str,
    ownership: Literal["owned", "borrowed"] = "borrowed",
    phase: Literal["training", "evaluation"] = "training",
    read_only: bool = False,
    top_k: int = 8,
    semantic_timeout: float = 30.0,
    audit_sink: AuditTraceSink | None = None,
) -> XMemoryRuntime:
    """Build the runner boundary through ``MemoryOrchestrator``."""

    module_config = configuration.xmemory
    if module_config is None:
        raise MemoryValidationError(
            "xmemory runtime requires the schema-1.3 xmemory module declaration"
        )
    if not module_config.enabled:
        raise MemoryValidationError("xmemory runtime requires an enabled xmemory module")
    module = XMemoryModule(
        facade,
        store,
        namespace=namespace,
        user_id=user_id,
        module_version=module_config.version,
        ownership=ownership,
        phase=phase,
        read_only=read_only,
        top_k=top_k,
        semantic_timeout=semantic_timeout,
    )
    orchestrator = MemoryOrchestrator(
        configuration,
        [MemoryModuleRegistration(XMEMORY_MODULE_ID, lambda _config: module)],
        audit_sink=audit_sink,
    )
    return XMemoryRuntime(orchestrator, module)


__all__ = [
    "XMEMORY_MODULE_ID",
    "XMEMORY_MODULE_VERSION",
    "XMemoryFacade",
    "XMemoryModule",
    "XMemoryRuntime",
    "build_xmemory_runtime",
]
