from __future__ import annotations

import hashlib
import json

from uptick_agent.core.contracts import SGR, Reasoner
from uptick_agent.core.models import AgentContext, DecisionInputFingerprint, SGRDecision
from uptick_agent.core.sgr import normalized_output_schema


class AgentCore:
    """Portable decision core with no environment, memory, or persistence access."""

    def __init__(self, *, reasoner: Reasoner, sgr: SGR) -> None:
        self._reasoner = reasoner
        self._sgr = sgr

    def fingerprint(self, context: AgentContext) -> DecisionInputFingerprint:
        request = self._sgr.build_request(context)
        context_json = context.model_dump_json(indent=2)
        normalized_schema = normalized_output_schema(request.output_schema)
        schema_json = json.dumps(
            normalized_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return DecisionInputFingerprint(
            system_prompt_sha256=_sha256(request.system_prompt),
            context_sha256=_sha256(context_json),
            schema_sha256=_sha256(schema_json),
        )

    async def decide(self, context: AgentContext) -> SGRDecision:
        request = self._sgr.build_request(context)
        result = await self._reasoner.reason(request)
        return self._sgr.parse_result(context, result)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
