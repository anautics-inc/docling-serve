"""Engine-neutral mapping from internal failures to public task failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from docling_serve.legacy_office import (
    LegacyOfficeError,
    LegacyOfficeSourceFetchError,
    build_legacy_office_public_task_error,
    classify_legacy_office_failure,
)
from docling_serve.upload_staging import UploadStagingError


@dataclass(frozen=True)
class TaskFailureMapping:
    """Public message plus whether docling-serve owns the failure contract."""

    public_message: str
    service_owned: bool


def _is_safe_service_failure(exc: BaseException) -> bool:
    safe_types = {
        ("docling_serve.access.extract", "AccessToolsUnavailableError"),
        ("docling_serve.execution.subprocesses", "ExternalCommandError"),
        ("docling_serve.form.extract", "XfaToolsUnavailableError"),
        ("docling_serve.ingestion.admission", "UploadAdmissionError"),
        ("docling_serve.schematic.kicad_sch", "KicadConversionError"),
    }
    cls = type(exc)
    return (cls.__module__, cls.__name__) in safe_types


def map_task_failure(exc: BaseException) -> TaskFailureMapping:
    """Map an exception without exposing internal or credential-bearing details."""

    if isinstance(exc, UploadStagingError):
        return TaskFailureMapping(
            public_message=exc.public_message,
            service_owned=True,
        )
    if _is_safe_service_failure(exc):
        return TaskFailureMapping(
            public_message=f"{type(exc).__name__} prevented document processing.",
            service_owned=True,
        )
    return TaskFailureMapping(
        public_message=build_legacy_office_public_task_error(exc),
        service_owned=isinstance(
            exc,
            (LegacyOfficeError, LegacyOfficeSourceFetchError),
        ),
    )


def map_public_failure(exc: BaseException, *, task_id: str) -> Any:
    """Build Jobkit's structured failure model with service-specific policy."""

    mapping = map_task_failure(exc)
    safe_exc = (
        RuntimeError(mapping.public_message)
        if mapping.service_owned
        and not isinstance(exc, (LegacyOfficeError, LegacyOfficeSourceFetchError))
        else exc
    )
    return classify_legacy_office_failure(safe_exc, task_id=task_id)
