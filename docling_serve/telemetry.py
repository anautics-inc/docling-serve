"""Bounded OpenTelemetry error reporting without raw exception data."""

from __future__ import annotations

import re
from typing import Any

from opentelemetry.trace import Span, Status, StatusCode

from docling_serve.upload_staging import (
    UploadStagingError,
    contains_bearer_syntax,
    redact_sensitive_text,
)

_IDENTIFIER = re.compile(
    r"(?i)(?:\b[0-9a-f]{32,64}\b|\b[0-9a-f]{8}-[0-9a-f-]{27,36}\b)"
)
_SAFE_TYPE = re.compile(r"[^A-Za-z0-9_.-]")


def sanitize_telemetry_text(value: Any, *, limit: int = 256) -> str:
    text = redact_sensitive_text(str(value))
    if contains_bearer_syntax(text):
        return "[redacted]"
    text = _IDENTIFIER.sub("[identifier]", text)
    text = " ".join(text.split())
    return text[:limit]


def record_sanitized_exception(span: Span, exc: Exception) -> None:
    exception_type = _SAFE_TYPE.sub("", type(exc).__name__)[:96] or "Exception"
    if isinstance(exc, UploadStagingError):
        message = exc.public_message
    else:
        message = sanitize_telemetry_text(exc)
    span.add_event(
        "exception",
        {
            "exception.type": exception_type,
            "exception.message": message,
            "exception.escaped": True,
        },
    )
    span.set_status(Status(StatusCode.ERROR))
