import asyncio
import json
from datetime import UTC, datetime

import pytest

from uptick_agent.memory.audit import AuditTraceWrite, StructuredAuditTraceSink, audit_event_id
from uptick_agent.memory.config import AuditConfiguration
from uptick_agent.memory.stores import InMemoryStructuredStore
from uptick_agent.redaction import redact_text, sanitize_json


def test_sanitize_json_redacts_quoted_secrets_in_embedded_json() -> None:
    secret = "synthetic-demo-credential"
    nested = json.dumps(
        {
            "data": {
                "api_key": secret,
                "password": "synthetic-demo-password",
                "message": "keep this useful context",
            }
        }
    )

    sanitized = sanitize_json({"content": f"runtime context: {nested}"})

    assert isinstance(sanitized, dict)
    content = sanitized["content"]
    assert isinstance(content, str)
    assert secret not in content
    assert "synthetic-demo-password" not in content
    assert "keep this useful context" in content
    assert json.loads(content.removeprefix("runtime context: ")) == {
        "data": {
            "api_key": "<redacted>",
            "password": "<redacted>",
            "message": "keep this useful context",
        }
    }


def test_sanitize_json_handles_spaces_escaped_quotes_and_nested_serialization() -> None:
    secret = 'value with spaces and "escaped" quotes'
    inner = json.dumps({"data": {"access_token": secret}})
    outer = json.dumps({"payload": inner})

    sanitized = sanitize_json(outer)
    assert isinstance(sanitized, str)
    assert secret not in sanitized
    assert "value with spaces" not in sanitized
    assert sanitize_json(sanitized) == sanitized
    assert json.loads(json.loads(sanitized)["payload"]) == {
        "data": {"access_token": "<redacted>"}
    }


def test_redaction_preserves_non_secret_text() -> None:
    text = "runtime context: {\"message\": \"service healthy\", \"count\": 2}"

    assert redact_text(text) == text


@pytest.mark.parametrize(
    "secret",
    [
        'synthetic-secret with spaces and "quoted } value"',
        "synthetic-secret\\",
        "synthetic-secret\\\\",
        "synthetic-secret\nwith a newline",
    ],
)
@pytest.mark.parametrize("encoding", ["json-string", "quote-only"])
def test_redaction_handles_escaped_json_backslash_variants(
    secret: str, encoding: str
) -> None:
    inner = json.dumps({"api_key": secret, "message": "keep"})
    fragment = (
        json.dumps(inner)[1:-1]
        if encoding == "json-string"
        else inner.replace('"', '\\"')
    )

    sanitized = redact_text(f"memo: {fragment} suffix")

    assert "synthetic-secret" not in sanitized
    assert "keep" in sanitized
    assert sanitized.endswith(" suffix")
    assert redact_text(sanitized) == sanitized


def test_redaction_handles_quoted_assignment_without_json_container() -> None:
    text = '"password": "value with spaces and \\"quotes\\""'

    sanitized = redact_text(text)

    assert sanitized == '"password": "<redacted>"'
    assert redact_text(sanitized) == sanitized


def test_audit_store_scrubs_double_encoded_json_inside_prose() -> None:
    async def scenario() -> None:
        secret = "synthetic-secret-123"
        inner = json.dumps({"api_key": secret})
        prose = "memo: " + json.dumps(inner)
        store = InMemoryStructuredStore()
        configuration = AuditConfiguration(enabled=True)
        sink = StructuredAuditTraceSink(
            store,
            namespace="audit-redaction-regression",
            configuration=configuration,
            runtime_configuration_fingerprint="a" * 64,
        )

        event = await sink.record(
            AuditTraceWrite(
                event_id=audit_event_id("run.outcome", "redaction-regression"),
                event_type="run.outcome",
                run_id="redaction-regression",
                sequence=1,
                occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
                outcome_correlation_id="outcome-redaction-regression",
                producer_id="test",
                producer_version="1.0",
                raw_bodies={"prompts": {"content": prose}},
            )
        )

        serialized_event = json.dumps(event.model_dump(mode="json"))
        serialized_store = json.dumps(
            (await store.list(namespace="audit-redaction-regression"))[0].model_dump(mode="json")
        )
        assert secret not in serialized_event
        assert secret not in serialized_store
        captured = event.captures[0]
        assert captured.body == {"content": "memo: \"{\\\"api_key\\\":\\\"<redacted>\\\"}\""}

    asyncio.run(scenario())


def test_audit_store_scrubs_escaped_json_fragment_inside_prose() -> None:
    async def scenario() -> None:
        secret = 'synthetic-secret with spaces and "quoted } value"'
        inner = json.dumps({"api_key": secret, "message": "keep"})
        escaped_fragment = json.dumps(inner)[1:-1]
        prose = "memo: " + escaped_fragment
        quote_only = "memo: " + inner.replace('"', '\\"')
        assert secret not in redact_text(quote_only)
        assert redact_text(redact_text(quote_only)) == redact_text(quote_only)
        store = InMemoryStructuredStore()
        configuration = AuditConfiguration(enabled=True)
        sink = StructuredAuditTraceSink(
            store,
            namespace="audit-redaction-escaped-fragment",
            configuration=configuration,
            runtime_configuration_fingerprint="a" * 64,
        )

        event = await sink.record(
            AuditTraceWrite(
                event_id=audit_event_id("run.outcome", "escaped-fragment-regression"),
                event_type="run.outcome",
                run_id="escaped-fragment-regression",
                sequence=1,
                occurred_at=datetime(2026, 9, 5, tzinfo=UTC),
                outcome_correlation_id="outcome-escaped-fragment-regression",
                producer_id="test",
                producer_version="1.0",
                raw_bodies={"prompts": {"content": prose}},
            )
        )

        serialized_event = json.dumps(event.model_dump(mode="json"))
        serialized_store = json.dumps(
            (await store.list(namespace="audit-redaction-escaped-fragment"))[0].model_dump(
                mode="json"
            )
        )
        assert secret not in serialized_event
        assert secret not in serialized_store
        assert "<redacted>" in json.dumps(event.captures[0].body)
        assert sanitize_json(event.captures[0].body) == event.captures[0].body

    asyncio.run(scenario())
