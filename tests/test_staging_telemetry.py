import json

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from docling_serve.telemetry import (
    record_sanitized_exception,
    sanitize_telemetry_text,
)
from docling_serve.upload_staging import StagedUploadTamperedError


def test_capture_exporter_never_receives_raw_staging_exception_data():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    raw = (
        "https://objects.example.test/item?X-Amz-Signature=bearer "
        "docling-staging/v1/"
        f"{'a' * 64}/{'1' * 32} "
        "Authorization: Bearer credential "
        "12345678-1234-1234-1234-123456789abc"
    )
    with tracer.start_as_current_span("test") as span:
        record_sanitized_exception(span, StagedUploadTamperedError(raw))
        span.set_attribute("safe.input", sanitize_telemetry_text(raw))

    serialized = json.dumps(
        [
            {
                "attributes": dict(item.attributes or {}),
                "events": [
                    {"name": event.name, "attributes": dict(event.attributes or {})}
                    for event in item.events
                ],
                "status": item.status.status_code.name,
            }
            for item in exporter.get_finished_spans()
        ],
        default=str,
    )
    for forbidden in (
        "X-Amz-Signature",
        "bearer",
        "docling-staging/v1/",
        "Authorization",
        "credential",
        "12345678-1234-1234-1234-123456789abc",
    ):
        assert forbidden.lower() not in serialized.lower()
    assert "StagedUploadTamperedError" in serialized
    assert "ERROR" in serialized


def test_telemetry_allows_noncredential_policy_words():
    value = "secretary credentialing password-policy token-economics"
    assert sanitize_telemetry_text(value) == value
