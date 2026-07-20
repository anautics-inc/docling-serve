"""Curated legacy Office conversion services."""

from docling_serve.legacy import _implementation
from docling_serve.legacy.source_identity import (
    SourceIdentityRestorer,
    restore_context_source_identity,
)

__all__ = [
    "APPROVED_SYSTEM_EXECUTABLE_ROOTS",
    "LEGACY_OFFICE_MIME_TYPES",
    "LEGACY_OFFICE_TARGETS",
    "LegacyHttpFetchResult",
    "LegacyOfficeCapabilityError",
    "LegacyOfficeConversionError",
    "LegacyOfficeConverter",
    "LegacyOfficeDoclingConverterManager",
    "LegacyOfficeError",
    "LegacyOfficeInputLimitError",
    "LegacyOfficeLimitError",
    "LegacyOfficeMissingOutputError",
    "LegacyOfficeOutputLimitError",
    "LegacyOfficeProcessSurvivedError",
    "LegacyOfficeScratchLimitError",
    "LegacyOfficeSourceFetchError",
    "LegacyOfficeSourcePolicyError",
    "LegacyOfficeTimeoutError",
    "LegacySourceIdentity",
    "LibreOfficeHeadlessConverter",
    "PinnedHttpResponse",
    "PreparedLegacySources",
    "ResolvedGlobalAddress",
    "SourceIdentityRestorer",
    "build_converter_manager",
    "build_legacy_office_public_task_error",
    "check_legacy_office_capability",
    "classify_legacy_office_failure",
    "fetch_legacy_http_source",
    "is_legacy_office_name",
    "original_content_type",
    "preconvert_legacy_office_sources",
    "ray_converter_run_with_retry",
    "restore_context_source_identity",
    "terminate_worker_fatally",
]
globals().update(
    {
        name: getattr(_implementation, name)
        for name in __all__
        if name not in {"SourceIdentityRestorer", "restore_context_source_identity"}
    }
)
