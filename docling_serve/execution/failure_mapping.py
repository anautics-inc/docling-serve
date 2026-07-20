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


def map_task_failure(exc: BaseException) -> TaskFailureMapping:
    """Map an exception without exposing internal or credential-bearing details."""

    if isinstance(exc, UploadStagingError):
        return TaskFailureMapping(
            public_message=exc.public_message,
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

    return classify_legacy_office_failure(exc, task_id=task_id)
